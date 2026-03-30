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
from uuid import uuid4

try:
    from langgraph.graph import END, StateGraph
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency: langgraph.\n"
        "Install with: python3 -m pip install --user langgraph"
    ) from exc

from app.intent_and_planner import (
    apply_request_constraint_overrides,
    call_lmstudio_chat,
    extract_json_object,
    intent_prompt_template,
    run_plan,
    sanitize_intent,
    validate_intent,
)
from app.core.memory import append_jsonl
from app.core.metrics import build_run_metrics
from app.core.policy import (
    apply_strategy_to_intent,
    build_default_strategy,
    build_fallback_strategy,
    default_judge_policy,
    default_strategy_policy,
    evaluate_plan_quality,
)
from app.core.runtime_env import load_project_env
from app.core.replan import apply_replan_event
from app.core.response import (
    build_natural_language_output,
    build_response_payload,
    classify_failure,
    compact_plan_result,
    infer_response_status,
)
from app.core.schemas import (
    JudgePolicy,
    JudgeResult,
    RetryPolicy,
    SessionMemory,
    StrategyPlan,
    StrategyPolicy,
    UserMemory,
)


class AgentState(TypedDict):
    request_id: str
    user_request: str
    intent: Dict[str, Any]
    strategy_plan: Dict[str, Any]
    plan_result: Dict[str, Any]
    judge_result: Dict[str, Any]
    response_payload: Dict[str, Any]
    metrics_summary: Dict[str, Any]
    response_text: str
    error: str
    retry_count: int
    retry_policy: RetryPolicy
    strategy_policy: StrategyPolicy
    judge_policy: JudgePolicy
    show_diagnostics: bool
    lmstudio_base_url: str
    model: str
    llm_timeout_sec: int
    llm_max_retries: int
    enable_thinking: bool
    enable_llm_strategy: bool
    enable_llm_judge: bool
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
    existing_intent = state.get("intent", {}) or {}
    if existing_intent:
        try:
            validate_intent(existing_intent)
            intent = sanitize_intent(existing_intent)
            state["intent"] = intent
            state["error"] = ""
            session = state["session_memory"]
            session.setdefault("intents", []).append(intent)
            state["session_memory"] = session
            record_event(state, "graph.parse_intent", "done", "Intent loaded from input")
            return state
        except Exception as exc:
            state["error"] = f"parse_intent_failed: {exc}"
            record_event(
                state,
                "graph.parse_intent",
                "error",
                "Provided intent is invalid",
                {"error": state["error"]},
            )
            return state
    try:
        system_prompt = intent_prompt_template(datetime.now().isoformat(timespec="minutes"))
        model_output = call_lmstudio_chat(
            base_url=state["lmstudio_base_url"],
            model=state["model"],
            system_prompt=system_prompt,
            user_prompt=state["user_request"],
            timeout_sec=state.get("llm_timeout_sec", 180),
            max_retries=state.get("llm_max_retries", 2),
            enable_thinking=state.get("enable_thinking", False),
        )
        intent = extract_json_object(model_output)
        intent = apply_request_constraint_overrides(intent, state.get("user_request", ""))
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
        if state.get("enable_llm_strategy", True):
            prompt = (
                "You are a retry-policy strategist.\n"
                "Return JSON only with keys: strategy_type, reason, constraint_adjustments, auto_pickup_adjustments.\n"
                "Rules:\n"
                "- Keep adjustments conservative and practical.\n"
                "- On first round (retry_count=0), usually keep constraints unchanged.\n"
                "- If last_error indicates no candidates, prioritize expanding auto pickup radius/limit.\n"
                "- If no feasible options, relax constraints gradually.\n"
                f"intent={json.dumps(state['intent'], ensure_ascii=False)}\n"
                f"retry_count={state.get('retry_count', 0)}\n"
                f"last_error={state.get('error', '')}\n"
                f"last_judge={json.dumps(state.get('judge_result', {}), ensure_ascii=False)}\n"
            )
            output = call_lmstudio_chat(
                base_url=state["lmstudio_base_url"],
                model=state["model"],
                system_prompt="Output strict JSON only. Do not output thinking or analysis.",
                user_prompt=prompt,
                timeout_sec=state.get("llm_timeout_sec", 180),
                max_retries=1,
                enable_thinking=state.get("enable_thinking", False),
            )
            strategy = extract_json_object(output)
        elif state.get("retry_count", 0) == 0:
            strategy = build_default_strategy(state["strategy_policy"])
        else:
            strategy = build_fallback_strategy(
                state.get("error", ""),
                state.get("retry_count", 0),
                state["strategy_policy"],
            )

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
    rule_judge: JudgeResult = evaluate_plan_quality(
        plan_result=state.get("plan_result", {}),
        error=state.get("error", ""),
        judge_policy=state["judge_policy"],
    )

    judge = rule_judge
    llm_judge_enabled = bool(state.get("enable_llm_judge", False))
    if llm_judge_enabled:
        try:
            options = (state.get("plan_result", {}) or {}).get("options", [])
            llm_prompt = (
                "You are a route-plan judge. Return JSON only with keys: pass_, reason, score, risks.\n"
                "Judge from practicality/user experience perspective.\n"
                f"intent={json.dumps(state.get('intent', {}), ensure_ascii=False)}\n"
                f"rule_judge={json.dumps(rule_judge, ensure_ascii=False)}\n"
                f"options={json.dumps(options, ensure_ascii=False)}\n"
            )
            llm_output = call_lmstudio_chat(
                base_url=state["lmstudio_base_url"],
                model=state["model"],
                system_prompt="Output strict JSON only. Do not output thinking or analysis.",
                user_prompt=llm_prompt,
                timeout_sec=state.get("llm_timeout_sec", 180),
                max_retries=1,
                enable_thinking=state.get("enable_thinking", False),
            )
            llm_judge_raw = extract_json_object(llm_output)
            llm_pass = bool(llm_judge_raw.get("pass_", False))
            llm_reason = str(llm_judge_raw.get("reason", ""))
            llm_score = float(llm_judge_raw.get("score", 0.0))
            llm_risks = [str(x) for x in llm_judge_raw.get("risks", [])]

            merged_risks = sorted(set(rule_judge.get("risks", []) + llm_risks))
            merged_pass = bool(rule_judge.get("pass_", False)) and llm_pass
            merged_score = round((float(rule_judge.get("score", 0.0)) + llm_score) / 2.0, 4)
            merged_reason = (
                f"rule: {rule_judge.get('reason', '')}; "
                f"llm: {llm_reason or 'no extra comment'}"
            )
            judge = {
                "pass_": merged_pass,
                "reason": merged_reason,
                "score": merged_score,
                "risks": merged_risks,
            }
        except Exception as exc:
            record_event(
                state,
                "graph.judge",
                "retry",
                "LLM judge failed, fallback to rule judge",
                {"error": str(exc)},
            )

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
    status = infer_response_status(error=state.get("error", ""), result=state.get("plan_result", {}))
    payload = build_response_payload(
        intent=state.get("intent", {}),
        result=state.get("plan_result", {}),
        retry_count=state.get("retry_count", 0),
        error=state.get("error", ""),
    )
    text = build_natural_language_output(
        status=status,
        intent=state.get("intent", {}),
        result=state.get("plan_result", {}),
        retry_count=state.get("retry_count", 0),
        error=state.get("error", ""),
    )
    state["response_payload"] = payload
    state["response_text"] = text
    state["metrics_summary"] = build_run_metrics(
        request_id=state.get("request_id", ""),
        user_request=state.get("user_request", ""),
        intent=state.get("intent", {}),
        plan_result=state.get("plan_result", {}),
        response_payload=payload,
        judge_result=state.get("judge_result", {}),
        retry_count=state.get("retry_count", 0),
        failure_category=classify_failure(
            error=state.get("error", ""),
            result=state.get("plan_result", {}),
        ),
    )
    record_event(
        state,
        "graph.compose",
        "done",
        "Response composed",
        {
            "status": status,
            "options": len((state.get("plan_result", {}) or {}).get("options", [])),
            "recommendation_basis": (payload.get("recommended_option") or {}).get(
                "recommendation_basis"
            ),
        },
    )
    return state


def persist_memory_node(state: AgentState) -> AgentState:
    record_event(state, "graph.persist", "start", "Persisting run trace")
    trace_path = Path(state.get("trace_path", ".runs/trace.jsonl"))
    append_jsonl(
        trace_path,
        {
            "request_id": state.get("request_id", ""),
            "time": datetime.now().isoformat(timespec="seconds"),
            "user_request": state.get("user_request"),
            "retry_count": state.get("retry_count", 0),
            "judge_result": state.get("judge_result", {}),
            "error": state.get("error", ""),
            "final_status": infer_response_status(
                error=state.get("error", ""),
                result=state.get("plan_result", {}),
            ),
            "failure_category": classify_failure(
                error=state.get("error", ""),
                result=state.get("plan_result", {}),
            ),
            "intent": state.get("intent", {}),
            "plan_result": state.get("plan_result", {}),
            "response_payload": state.get("response_payload", {}),
            "metrics_summary": state.get("metrics_summary", {}),
            "trace_events": state.get("trace_events", []),
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
    project_root = Path(__file__).resolve().parents[1]
    load_project_env(project_root, override=False)

    parser = argparse.ArgumentParser(description="结伴而行 Agent (LangGraph 主干 + Specialist 架构)")
    parser.add_argument("--user-request", required=True)
    parser.add_argument("--intent-json-path")
    parser.add_argument("--replan-event-json-path")
    parser.add_argument("--previous-response-json-path")
    parser.add_argument("--output-json-path")
    parser.add_argument("--intent-output-json-path")
    parser.add_argument("--lmstudio-base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--model", default="qwen/qwen3.5-9b")
    parser.add_argument("--show-diagnostics", action="store_true")
    parser.add_argument("--print-intent", action="store_true")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--json-stdout", action="store_true")
    parser.add_argument("--llm-timeout-sec", type=int, default=30)
    parser.add_argument("--llm-max-retries", type=int, default=1)
    parser.add_argument("--enable-thinking", action="store_true")
    parser.add_argument("--disable-thinking", action="store_false", dest="enable_thinking")
    parser.add_argument("--retry-max-attempts", type=int, default=2)
    parser.add_argument("--planner-timeout-sec", type=int, default=120)
    parser.add_argument("--planner-max-retries", type=int, default=2)
    parser.add_argument("--retry-backoff-sec", type=float, default=0.6)
    parser.add_argument("--strategy-step-wait-min", type=int, default=10)
    parser.add_argument("--strategy-step-detour-min", type=int, default=15)
    parser.add_argument("--strategy-step-passenger-travel-min", type=int, default=15)
    parser.add_argument("--strategy-base-auto-radius-m", type=int, default=1500)
    parser.add_argument("--strategy-auto-radius-step-m", type=int, default=300)
    parser.add_argument("--strategy-base-auto-limit", type=int, default=25)
    parser.add_argument("--strategy-auto-limit-step", type=int, default=5)
    parser.add_argument("--judge-max-avg-wait-min", type=int, default=45)
    parser.add_argument("--judge-max-avg-detour-min", type=int, default=90)
    parser.add_argument("--judge-min-options-required", type=int, default=1)
    parser.add_argument("--enable-llm-strategy", action="store_true")
    parser.add_argument("--disable-llm-strategy", action="store_false", dest="enable_llm_strategy")
    parser.add_argument("--enable-llm-judge", action="store_true")
    parser.add_argument("--disable-llm-judge", action="store_false", dest="enable_llm_judge")
    parser.add_argument("--trace-path", default=".runs/trace.jsonl")
    parser.set_defaults(enable_llm_strategy=False, enable_llm_judge=False, enable_thinking=False)
    args = parser.parse_args()

    amap_key = os.getenv("AMAP_WEB_SERVICE_KEY", "").strip()
    if not amap_key:
        raise ValueError("AMAP_WEB_SERVICE_KEY is required in server environment.")

    strategy_policy = default_strategy_policy()
    strategy_policy["step_wait_min"] = args.strategy_step_wait_min
    strategy_policy["step_detour_min"] = args.strategy_step_detour_min
    strategy_policy["step_passenger_travel_min"] = args.strategy_step_passenger_travel_min
    strategy_policy["base_auto_radius_m"] = args.strategy_base_auto_radius_m
    strategy_policy["auto_radius_step_m"] = args.strategy_auto_radius_step_m
    strategy_policy["base_auto_limit"] = args.strategy_base_auto_limit
    strategy_policy["auto_limit_step"] = args.strategy_auto_limit_step

    judge_policy = default_judge_policy()
    judge_policy["max_avg_wait_min"] = args.judge_max_avg_wait_min
    judge_policy["max_avg_detour_min"] = args.judge_max_avg_detour_min
    judge_policy["min_options_required"] = args.judge_min_options_required

    provided_intent: Dict[str, Any] = {}
    if args.intent_json_path:
        with open(args.intent_json_path, "r", encoding="utf-8") as f:
            provided_intent = json.load(f)
    if args.previous_response_json_path and provided_intent:
        with open(args.previous_response_json_path, "r", encoding="utf-8") as f:
            previous_payload = json.load(f)
        previous_response_payload = previous_payload.get("response_payload", previous_payload)
        previous_recommended = (previous_response_payload.get("recommended_option") or {})
        if previous_recommended:
            provided_intent["previous_recommendation"] = previous_recommended
    if args.replan_event_json_path:
        with open(args.replan_event_json_path, "r", encoding="utf-8") as f:
            replan_event = json.load(f)
        if provided_intent:
            provided_intent = apply_replan_event(provided_intent, replan_event)

    app = build_graph(amap_key=amap_key)
    init_state: AgentState = {
        "request_id": uuid4().hex,
        "user_request": args.user_request,
        "intent": provided_intent,
        "strategy_plan": {},
        "plan_result": {},
        "judge_result": {},
        "response_payload": {},
        "metrics_summary": {},
        "response_text": "",
        "error": "",
        "retry_count": 0,
        "retry_policy": {
            "max_attempts": args.retry_max_attempts,
            "backoff_sec": args.retry_backoff_sec,
            "planner_timeout_sec": args.planner_timeout_sec,
            "planner_max_retries": args.planner_max_retries,
        },
        "strategy_policy": strategy_policy,
        "judge_policy": judge_policy,
        "show_diagnostics": args.show_diagnostics,
        "lmstudio_base_url": args.lmstudio_base_url,
        "model": args.model,
        "llm_timeout_sec": args.llm_timeout_sec,
        "llm_max_retries": args.llm_max_retries,
        "enable_thinking": args.enable_thinking,
        "enable_llm_strategy": args.enable_llm_strategy,
        "enable_llm_judge": args.enable_llm_judge,
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

    status = infer_response_status(
        error=final_state.get("error", ""),
        result=final_state.get("plan_result", {}),
    )
    payload = {
        "request_id": final_state.get("request_id", ""),
        "status": status,
        "error": final_state.get("error", ""),
        "retry_count": final_state.get("retry_count", 0),
        "strategy_plan": final_state.get("strategy_plan", {}),
        "judge_result": final_state.get("judge_result", {}),
        "response_payload": final_state.get("response_payload", {}),
        "metrics_summary": final_state.get("metrics_summary", {}),
        "natural_language_output": final_state.get("response_text", ""),
        "result": final_state.get("plan_result", {}),
    }
    if args.intent_output_json_path and final_state.get("intent"):
        intent_output_path = Path(args.intent_output_json_path)
        intent_output_path.parent.mkdir(parents=True, exist_ok=True)
        intent_output_path.write_text(
            json.dumps(final_state.get("intent", {}), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.output_json_path:
        output_path = Path(args.output_json_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.json_stdout:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(final_state.get("response_text", ""))
    return 1 if status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
