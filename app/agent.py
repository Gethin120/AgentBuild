from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

try:
    from langgraph.graph import END, StateGraph
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency: langgraph.\n"
        "Install with: python3 -m pip install --user langgraph"
    ) from exc

from app.intent_and_planner import (
    call_lmstudio_chat,
    extract_json_object,
    intent_prompt_template,
    run_plan,
    sanitize_intent,
    validate_intent,
)
from app.core.memory import append_jsonl
from app.core.policy import apply_strategy_to_intent, build_default_strategy, build_fallback_strategy
from app.core.response import build_natural_language_output, compact_plan_result
from app.core.schemas import JudgeResult, RetryPolicy, SessionMemory, StrategyPlan, UserMemory


class AgentState(TypedDict):
    user_request: str
    intent: Dict[str, Any]
    strategy_plan: Dict[str, Any]
    plan_result: Dict[str, Any]
    judge_result: Dict[str, Any]
    response_text: str
    error: str
    retry_count: int
    retry_policy: RetryPolicy
    show_diagnostics: bool
    lmstudio_base_url: str
    model: str
    llm_timeout_sec: int
    llm_max_retries: int
    enable_llm_strategy: bool
    enable_llm_narration: bool
    user_memory: Dict[str, Any]
    session_memory: Dict[str, Any]
    trace_events: List[Dict[str, Any]]
    progress: bool
    trace_path: str


def emit_progress(
    enabled: bool, stage: str, status: str, message: str, extra: Optional[Dict[str, Any]] = None
) -> None:
    if not enabled:
        return
    event = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "stage": stage,
        "status": status,
        "message": message,
    }
    if extra:
        event["extra"] = extra
    print(f"[PROGRESS] {json.dumps(event, ensure_ascii=False)}", file=sys.stderr, flush=True)


def record_event(
    state: AgentState,
    stage: str,
    status: str,
    message: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    event = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "stage": stage,
        "status": status,
        "message": message,
        "epoch": time.time(),
    }
    if extra:
        event["extra"] = extra
    state.setdefault("trace_events", []).append(event)
    emit_progress(state.get("progress", False), stage, status, message, extra)


def _run_with_timeout(fn, timeout_sec: int):
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_sec)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _is_retryable_error(error_text: str) -> bool:
    lowered = error_text.lower()
    retryable_signals = [
        "timeout",
        "temporarily",
        "connection",
        "network",
        "10021",
        "qps",
        "rate",
    ]
    return any(signal in lowered for signal in retryable_signals)


def parse_intent_node(state: AgentState) -> AgentState:
    record_event(state, "graph.parse_intent", "start", "Parsing intent with LLM")
    try:
        system_prompt = intent_prompt_template(datetime.now().isoformat(timespec="minutes"))
        model_output = call_lmstudio_chat(
            base_url=state["lmstudio_base_url"],
            model=state["model"],
            system_prompt=system_prompt,
            user_prompt=state["user_request"],
            timeout_sec=state.get("llm_timeout_sec", 180),
            max_retries=state.get("llm_max_retries", 2),
        )
        intent = extract_json_object(model_output)
        validate_intent(intent)
        intent = sanitize_intent(intent)
        state["intent"] = intent
        state["error"] = ""
        session = state["session_memory"]
        session.setdefault("intents", []).append(intent)
        state["session_memory"] = session
        record_event(state, "graph.parse_intent", "done", "Intent parsed")
    except Exception as exc:
        state["error"] = f"parse_intent_failed: {exc}"
        record_event(
            state,
            "graph.parse_intent",
            "error",
            "Intent parse failed",
            {"error": state["error"]},
        )
    return state


def strategy_node(state: AgentState) -> AgentState:
    record_event(state, "graph.strategy", "start", "Building strategy plan")
    if not state.get("intent"):
        return state

    try:
        if state.get("retry_count", 0) == 0:
            strategy = build_default_strategy()
        elif state.get("enable_llm_strategy", False):
            prompt = (
                "You are a retry-policy strategist.\n"
                "Return JSON only with keys: strategy_type, reason, constraint_adjustments, auto_pickup_adjustments.\n"
                "Given this intent and last error, propose a conservative next retry strategy.\n"
                f"intent={json.dumps(state['intent'], ensure_ascii=False)}\n"
                f"last_error={state.get('error', '')}\n"
            )
            output = call_lmstudio_chat(
                base_url=state["lmstudio_base_url"],
                model=state["model"],
                system_prompt="Output strict JSON only.",
                user_prompt=prompt,
                timeout_sec=state.get("llm_timeout_sec", 180),
                max_retries=1,
            )
            strategy = extract_json_object(output)
        else:
            strategy = build_fallback_strategy(state.get("error", ""), state.get("retry_count", 0))

        state["strategy_plan"] = strategy
        patched_intent = apply_strategy_to_intent(state["intent"], strategy)
        state["intent"] = patched_intent
        session = state["session_memory"]
        session.setdefault("strategies", []).append(strategy)
        state["session_memory"] = session
        state["error"] = ""
        record_event(
            state,
            "graph.strategy",
            "done",
            "Strategy prepared",
            {"strategy_type": strategy.get("strategy_type")},
        )
    except Exception as exc:
        state["error"] = f"strategy_failed: {exc}"
        record_event(state, "graph.strategy", "error", "Strategy failed", {"error": state["error"]})
    return state


def planning_node(state: AgentState, amap_key: str) -> AgentState:
    record_event(state, "graph.plan", "start", "Running deterministic planner")
    if not state.get("intent"):
        state["error"] = "missing_intent"
        return state

    policy = state.get("retry_policy", {})
    timeout_sec = int(policy.get("planner_timeout_sec", 120))
    call_retries = int(policy.get("planner_max_retries", 1))
    last_error = ""

    for attempt in range(1, call_retries + 2):
        try:
            result = _run_with_timeout(
                lambda: run_plan(
                    intent=state["intent"],
                    amap_key=amap_key,
                    show_diagnostics=state["show_diagnostics"],
                ),
                timeout_sec=timeout_sec,
            )
            state["plan_result"] = compact_plan_result(result)
            state["error"] = ""
            record_event(
                state,
                "graph.plan",
                "done",
                "Planner completed",
                {"attempt": attempt, "options": len(state["plan_result"].get("options", []))},
            )
            return state
        except FuturesTimeoutError:
            last_error = f"planner_timeout: exceeded {timeout_sec}s"
        except Exception as exc:
            last_error = str(exc)

        if attempt >= call_retries + 1 or not _is_retryable_error(last_error):
            state["error"] = last_error
            record_event(
                state,
                "graph.plan",
                "error",
                "Planner failed",
                {"error": state["error"], "attempt": attempt},
            )
            return state

        record_event(
            state,
            "graph.plan",
            "retry",
            "Planner transient failure, retrying",
            {"attempt": attempt, "error": last_error},
        )
        time.sleep(float(policy.get("backoff_sec", 0.6)) * attempt)

    return state


def judge_node(state: AgentState) -> AgentState:
    record_event(state, "graph.judge", "start", "Judging current plan quality")
    options = state.get("plan_result", {}).get("options", [])

    if state.get("error"):
        judge: JudgeResult = {
            "pass_": False,
            "reason": "Planner error occurred.",
            "score": 0.0,
            "risks": [state["error"]],
        }
    elif options:
        wait_values = [int(x.get("pickup_wait_time_min", 0)) for x in options]
        detour_values = [int(x.get("driver_detour_time_min", 0)) for x in options]
        score = 1.0 / (1.0 + sum(wait_values) + sum(detour_values))
        judge = {
            "pass_": True,
            "reason": "Feasible options found.",
            "score": round(score, 4),
            "risks": [],
        }
    else:
        judge = {
            "pass_": False,
            "reason": "No feasible options under current constraints.",
            "score": 0.0,
            "risks": ["no_feasible_option"],
        }

    state["judge_result"] = judge
    session = state["session_memory"]
    session.setdefault("judge_results", []).append(judge)
    state["session_memory"] = session
    record_event(
        state,
        "graph.judge",
        "done",
        "Judge evaluated plan",
        {"pass": judge["pass_"], "score": judge["score"]},
    )
    return state


def retry_controller_node(state: AgentState) -> AgentState:
    record_event(state, "graph.retry", "start", "Evaluating retry policy")
    max_attempts = int(state.get("retry_policy", {}).get("max_attempts", 2))
    judge_pass = bool((state.get("judge_result") or {}).get("pass_", False))

    if judge_pass:
        record_event(state, "graph.retry", "done", "No retry needed")
        return state

    if state.get("retry_count", 0) >= max_attempts:
        record_event(state, "graph.retry", "done", "Retry budget exhausted")
        return state

    state["retry_count"] = state.get("retry_count", 0) + 1
    record_event(
        state,
        "graph.retry",
        "retry",
        "Will retry with updated strategy",
        {"retry_count": state["retry_count"], "max_attempts": max_attempts},
    )
    return state


def compose_response_node(state: AgentState) -> AgentState:
    record_event(state, "graph.compose", "start", "Composing final response")
    status = "error" if state.get("error") else "ok"
    text = build_natural_language_output(
        status=status,
        intent=state.get("intent", {}),
        result=state.get("plan_result", {}),
        retry_count=state.get("retry_count", 0),
        error=state.get("error", ""),
    )
    state["response_text"] = text
    record_event(state, "graph.compose", "done", "Response composed")
    return state


def persist_memory_node(state: AgentState) -> AgentState:
    record_event(state, "graph.persist", "start", "Persisting run trace")
    trace_path = Path(state.get("trace_path", ".runs/trace.jsonl"))
    append_jsonl(
        trace_path,
        {
            "time": datetime.now().isoformat(timespec="seconds"),
            "user_request": state.get("user_request"),
            "retry_count": state.get("retry_count", 0),
            "judge_result": state.get("judge_result", {}),
            "error": state.get("error", ""),
            "intent": state.get("intent", {}),
        },
    )
    record_event(state, "graph.persist", "done", "Trace persisted", {"path": str(trace_path)})
    return state


def route_after_parse(state: AgentState) -> str:
    return "end" if state.get("error") else "strategy"


def route_after_judge(state: AgentState) -> str:
    judge_pass = bool((state.get("judge_result") or {}).get("pass_", False))
    if judge_pass:
        return "compose"
    max_attempts = int(state.get("retry_policy", {}).get("max_attempts", 2))
    if state.get("retry_count", 0) < max_attempts:
        return "retry"
    return "compose"


def route_after_retry(state: AgentState) -> str:
    max_attempts = int(state.get("retry_policy", {}).get("max_attempts", 2))
    if state.get("retry_count", 0) <= max_attempts:
        return "strategy"
    return "compose"


def build_graph(amap_key: str):
    graph = StateGraph(AgentState)
    graph.add_node("parse_intent", parse_intent_node)
    graph.add_node("strategy", strategy_node)
    graph.add_node("plan", lambda s: planning_node(s, amap_key=amap_key))
    graph.add_node("judge", judge_node)
    graph.add_node("retry", retry_controller_node)
    graph.add_node("compose", compose_response_node)
    graph.add_node("persist", persist_memory_node)

    graph.set_entry_point("parse_intent")
    graph.add_conditional_edges("parse_intent", route_after_parse, {"strategy": "strategy", "end": END})
    graph.add_edge("strategy", "plan")
    graph.add_edge("plan", "judge")
    graph.add_conditional_edges(
        "judge", route_after_judge, {"retry": "retry", "compose": "compose"}
    )
    graph.add_conditional_edges(
        "retry", route_after_retry, {"strategy": "strategy", "compose": "compose"}
    )
    graph.add_edge("compose", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


def main() -> int:
    parser = argparse.ArgumentParser(description="结伴而行 Agent (LangGraph 主干 + Specialist 架构)")
    parser.add_argument("--user-request", required=True)
    parser.add_argument("--lmstudio-base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--model", default="qwen/qwen3.5-9b")
    parser.add_argument("--show-diagnostics", action="store_true")
    parser.add_argument("--print-intent", action="store_true")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--llm-timeout-sec", type=int, default=180)
    parser.add_argument("--llm-max-retries", type=int, default=2)
    parser.add_argument("--retry-max-attempts", type=int, default=2)
    parser.add_argument("--planner-timeout-sec", type=int, default=120)
    parser.add_argument("--planner-max-retries", type=int, default=2)
    parser.add_argument("--retry-backoff-sec", type=float, default=0.6)
    parser.add_argument("--enable-llm-strategy", action="store_true")
    parser.add_argument("--enable-llm-narration", action="store_true")
    parser.add_argument("--trace-path", default=".runs/trace.jsonl")
    args = parser.parse_args()

    amap_key = os.getenv("AMAP_WEB_SERVICE_KEY", "").strip()
    if not amap_key:
        raise ValueError("AMAP_WEB_SERVICE_KEY is required in server environment.")

    app = build_graph(amap_key=amap_key)
    init_state: AgentState = {
        "user_request": args.user_request,
        "intent": {},
        "strategy_plan": {},
        "plan_result": {},
        "judge_result": {},
        "response_text": "",
        "error": "",
        "retry_count": 0,
        "retry_policy": {
            "max_attempts": args.retry_max_attempts,
            "backoff_sec": args.retry_backoff_sec,
            "planner_timeout_sec": args.planner_timeout_sec,
            "planner_max_retries": args.planner_max_retries,
        },
        "show_diagnostics": args.show_diagnostics,
        "lmstudio_base_url": args.lmstudio_base_url,
        "model": args.model,
        "llm_timeout_sec": args.llm_timeout_sec,
        "llm_max_retries": args.llm_max_retries,
        "enable_llm_strategy": args.enable_llm_strategy,
        "enable_llm_narration": args.enable_llm_narration,
        "user_memory": UserMemory().__dict__,
        "session_memory": SessionMemory().__dict__,
        "trace_events": [],
        "progress": args.progress,
        "trace_path": args.trace_path,
    }
    final_state = app.invoke(init_state)

    if args.print_intent and final_state.get("intent"):
        print("Parsed intent:")
        print(json.dumps(final_state.get("intent", {}), ensure_ascii=False, indent=2))

    status = "error" if final_state.get("error") else "ok"
    payload = {
        "status": status,
        "error": final_state.get("error", ""),
        "retry_count": final_state.get("retry_count", 0),
        "strategy_plan": final_state.get("strategy_plan", {}),
        "judge_result": final_state.get("judge_result", {}),
        "natural_language_output": final_state.get("response_text", ""),
        "result": final_state.get("plan_result", {}),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
