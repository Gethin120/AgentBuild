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
    from langgraph.types import Command, interrupt
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

from app.core.checkpointing import FileCheckpointSaver
from app.core.memory import append_jsonl
from app.core.feedback import (
    build_selection_payload,
    feedback_parser_system_prompt,
    feedback_parser_user_prompt,
    normalize_feedback_event,
    apply_feedback_event,
    resolve_option_reference,
    should_use_llm_feedback_parser,
)
from app.core.metrics import build_run_metrics
from app.core.session_store import persist_turn_state
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
    FeedbackEvent,
    RetryPolicy,
    SessionMemory,
    StrategyPlan,
    StrategyPolicy,
    UserMemory,
)


LANGGRAPH_CHECKPOINTER = FileCheckpointSaver(
    Path(__file__).resolve().parents[1] / ".runs" / "langgraph_checkpoints"
)


class AgentState(TypedDict):
    request_id: str
    session_id: str
    thread_id: str
    turn_type: str
    user_request: str
    intent: Dict[str, Any]
    strategy_plan: Dict[str, Any]
    feedback_event: Dict[str, Any]
    feedback_control: Dict[str, Any]
    followup_payload: Dict[str, Any]
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


def parse_feedback_event_with_fallback(
    *,
    raw_feedback_event: Dict[str, Any],
    previous_response_payload: Dict[str, Any],
    lmstudio_base_url: str,
    model: str,
    llm_timeout_sec: int,
    llm_max_retries: int,
    enable_thinking: bool,
) -> FeedbackEvent:
    if should_use_llm_feedback_parser(raw_feedback_event):
        try:
            llm_feedback_output = call_lmstudio_chat(
                base_url=lmstudio_base_url,
                model=model,
                system_prompt=feedback_parser_system_prompt(),
                user_prompt=feedback_parser_user_prompt(
                    str(raw_feedback_event.get("reason", "") or ""),
                    previous_response_payload=previous_response_payload,
                ),
                timeout_sec=llm_timeout_sec,
                max_retries=llm_max_retries,
                enable_thinking=enable_thinking,
            )
            return normalize_feedback_event(extract_json_object(llm_feedback_output))
        except Exception:
            return normalize_feedback_event(raw_feedback_event)
    return normalize_feedback_event(raw_feedback_event)


def serialize_interrupts(items: Any) -> List[Dict[str, Any]]:
    serialized: List[Dict[str, Any]] = []
    for item in list(items or []):
        serialized.append(
            {
                "id": getattr(item, "id", ""),
                "value": getattr(item, "value", item),
            }
        )
    return serialized


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
            "session_id": state.get("session_id", ""),
            "thread_id": state.get("thread_id", ""),
            "turn_type": state.get("turn_type", ""),
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
            "feedback_event": state.get("feedback_event", {}),
            "plan_result": state.get("plan_result", {}),
            "response_payload": state.get("response_payload", {}),
            "metrics_summary": state.get("metrics_summary", {}),
            "trace_events": state.get("trace_events", []),
        },
    )
    session_id = str(state.get("session_id", "") or "").strip()
    if session_id:
        metrics = dict(state.get("metrics_summary", {}) or {})
        metrics["feedback_event"] = dict(state.get("feedback_event", {}) or {})
        persist_turn_state(
            Path(__file__).resolve().parents[1],
            session_id=session_id,
            turn_type=str(state.get("turn_type", "request") or "request"),
            user_input=str(state.get("user_request", "") or ""),
            intent=dict(state.get("intent", {}) or {}),
            response_payload=dict(state.get("response_payload", {}) or {}),
            metrics_summary=metrics,
        )
    record_event(state, "graph.persist", "done", "Trace persisted", {"path": str(trace_path)})
    return state


def await_feedback_node(state: AgentState) -> AgentState:
    if infer_response_status(error=state.get("error", ""), result=state.get("plan_result", {})) == "error":
        return state
    if str(state.get("turn_type", "") or "") == "selection":
        return state

    payload = interrupt(
        {
            "type": "await_feedback",
            "session_id": state.get("session_id", ""),
            "thread_id": state.get("thread_id", ""),
            "summary": (state.get("response_payload", {}) or {}).get("summary", {}),
            "recommended_option": (state.get("response_payload", {}) or {}).get(
                "recommended_option", {}
            ),
            "alternative_options": (state.get("response_payload", {}) or {}).get(
                "alternative_options", []
            ),
        }
    )
    state["followup_payload"] = dict(payload or {})
    return state


def apply_followup_feedback_node(state: AgentState) -> AgentState:
    followup = dict(state.get("followup_payload", {}) or {})
    raw_feedback_event = dict(followup.get("feedback_event", {}) or {})
    previous_response_payload = dict(state.get("response_payload", {}) or {})

    if not raw_feedback_event:
        state["error"] = "missing_feedback_event"
        return state

    try:
        feedback_event = parse_feedback_event_with_fallback(
            raw_feedback_event=raw_feedback_event,
            previous_response_payload=previous_response_payload,
            lmstudio_base_url=state["lmstudio_base_url"],
            model=state["model"],
            llm_timeout_sec=state.get("llm_timeout_sec", 30),
            llm_max_retries=state.get("llm_max_retries", 1),
            enable_thinking=state.get("enable_thinking", False),
        )
        updated_intent, feedback_control = apply_feedback_event(
            state.get("intent", {}) or {},
            feedback_event,
            previous_response_payload=previous_response_payload,
            selected_option_ref=str(followup.get("selected_option_ref", "") or ""),
        )
        state["intent"] = updated_intent
        state["feedback_event"] = feedback_event
        state["feedback_control"] = feedback_control
        state["user_request"] = str(feedback_event.get("reason", "") or state.get("user_request", ""))
        state["turn_type"] = "feedback"
        state["retry_count"] = 0
        state["strategy_plan"] = {}
        state["plan_result"] = {}
        state["judge_result"] = {}
        state["response_payload"] = {}
        state["response_text"] = ""
        state["metrics_summary"] = {}
        state["error"] = ""
        session = state["session_memory"]
        session.setdefault("feedback_events", []).append(feedback_event)
        state["session_memory"] = session
        record_event(
            state,
            "graph.followup_feedback",
            "done",
            "Follow-up feedback applied",
            {"type": feedback_event.get("type", ""), "reason": feedback_event.get("reason", "")},
        )
    except Exception as exc:
        state["error"] = str(exc)
        record_event(
            state,
            "graph.followup_feedback",
            "error",
            "Follow-up feedback failed",
            {"error": state["error"]},
        )
    return state


def apply_followup_selection_node(state: AgentState) -> AgentState:
    followup = dict(state.get("followup_payload", {}) or {})
    previous_response_payload = dict(state.get("response_payload", {}) or {})
    selected_option_ref = str(followup.get("selected_option_ref", "") or "")
    raw_feedback_event = dict(followup.get("feedback_event", {}) or {})
    try:
        feedback_event = normalize_feedback_event(
            raw_feedback_event
            or {
                "type": "option_selection",
                "target_option": selected_option_ref,
                "signals": [{"kind": "selection", "value": "select_option", "strength": "hard"}],
                "reason": "用户确认采用该方案。",
            }
        )
        selected_option = resolve_option_reference(previous_response_payload, selected_option_ref)
        payload = build_selection_payload(
            previous_response_payload=previous_response_payload,
            selected_option=selected_option,
            selected_option_ref=selected_option_ref,
            feedback_event=feedback_event,
        )
        state["feedback_event"] = feedback_event
        state["feedback_control"] = {"selection_only": True, "selected_option": selected_option}
        state["turn_type"] = "selection"
        state["user_request"] = str(feedback_event.get("reason", "") or "选择方案")
        state["response_payload"] = payload
        state["response_text"] = payload.get("execution_ready_share_text", "")
        state["metrics_summary"] = build_run_metrics(
            request_id=state.get("request_id", ""),
            user_request=state.get("user_request", ""),
            intent=state.get("intent", {}),
            plan_result={"options": [selected_option] if selected_option else []},
            response_payload=payload,
            judge_result={},
            retry_count=0,
            failure_category="",
        )
        state["error"] = ""
        record_event(
            state,
            "graph.followup_selection",
            "done",
            "Follow-up selection applied",
            {"selected_option_ref": selected_option_ref},
        )
    except Exception as exc:
        state["error"] = str(exc)
        record_event(
            state,
            "graph.followup_selection",
            "error",
            "Follow-up selection failed",
            {"error": state["error"]},
        )
    return state


def apply_followup_replan_node(state: AgentState) -> AgentState:
    followup = dict(state.get("followup_payload", {}) or {})
    replan_event = dict(followup.get("replan_event", {}) or {})
    if not replan_event:
        state["error"] = "missing_replan_event"
        return state
    try:
        state["intent"] = apply_replan_event(state.get("intent", {}) or {}, replan_event)
        state["turn_type"] = "replan"
        state["user_request"] = str(followup.get("reason", "") or "重规划")
        state["retry_count"] = 0
        state["strategy_plan"] = {}
        state["plan_result"] = {}
        state["judge_result"] = {}
        state["response_payload"] = {}
        state["response_text"] = ""
        state["metrics_summary"] = {}
        state["error"] = ""
        record_event(
            state,
            "graph.followup_replan",
            "done",
            "Follow-up replan event applied",
            {"replan_type": replan_event.get("type", "")},
        )
    except Exception as exc:
        state["error"] = str(exc)
        record_event(
            state,
            "graph.followup_replan",
            "error",
            "Follow-up replan failed",
            {"error": state["error"]},
        )
    return state


def route_after_parse(state: AgentState) -> str:
    return "compose" if state.get("error") else "strategy"


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


def route_after_persist(state: AgentState) -> str:
    status = str((state.get("response_payload", {}) or {}).get("status", "") or "")
    if status in {"error", "selected"} or str(state.get("turn_type", "") or "") == "selection":
        return "end"
    return "await_feedback"


def route_after_followup(state: AgentState) -> str:
    if state.get("error"):
        return "compose"
    action = str((state.get("followup_payload", {}) or {}).get("action", "") or "")
    if action == "selection":
        return "persist"
    if action == "replan":
        return "strategy"
    if action == "feedback":
        control = dict(state.get("feedback_control", {}) or {})
        if control.get("selection_only"):
            return "persist"
        return "strategy"
    return "end"


def build_graph(amap_key: str):
    graph = StateGraph(AgentState)
    graph.add_node("parse_intent", parse_intent_node)
    graph.add_node("strategy", strategy_node)
    graph.add_node("plan", lambda s: planning_node(s, amap_key=amap_key))
    graph.add_node("judge", judge_node)
    graph.add_node("retry", retry_controller_node)
    graph.add_node("compose", compose_response_node)
    graph.add_node("persist", persist_memory_node)
    graph.add_node("await_feedback", await_feedback_node)
    graph.add_node("apply_followup_feedback", apply_followup_feedback_node)
    graph.add_node("apply_followup_selection", apply_followup_selection_node)
    graph.add_node("apply_followup_replan", apply_followup_replan_node)

    graph.set_entry_point("parse_intent")
    graph.add_conditional_edges("parse_intent", route_after_parse, {"strategy": "strategy", "compose": "compose"})
    graph.add_edge("strategy", "plan")
    graph.add_edge("plan", "judge")
    graph.add_conditional_edges(
        "judge", route_after_judge, {"retry": "retry", "compose": "compose"}
    )
    graph.add_conditional_edges(
        "retry", route_after_retry, {"strategy": "strategy", "compose": "compose"}
    )
    graph.add_edge("compose", "persist")
    graph.add_conditional_edges("persist", route_after_persist, {"await_feedback": "await_feedback", "end": END})
    graph.add_conditional_edges(
        "await_feedback",
        route_after_followup,
        {
            "strategy": "strategy",
            "persist": "persist",
            "compose": "compose",
            "end": END,
        },
    )
    return graph.compile(checkpointer=LANGGRAPH_CHECKPOINTER)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    load_project_env(project_root, override=False)

    parser = argparse.ArgumentParser(description="结伴而行 Agent (LangGraph 主干 + Specialist 架构)")
    parser.add_argument("--user-request", required=True)
    parser.add_argument("--intent-json-path")
    parser.add_argument("--replan-event-json-path")
    parser.add_argument("--feedback-json-path")
    parser.add_argument("--previous-response-json-path")
    parser.add_argument("--selected-option-ref")
    parser.add_argument("--session-id")
    parser.add_argument("--turn-type", default="request")
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
    previous_payload: Dict[str, Any] = {}
    previous_response_payload: Dict[str, Any] = {}
    resume_payload: Dict[str, Any] = {}
    if args.intent_json_path:
        with open(args.intent_json_path, "r", encoding="utf-8") as f:
            provided_intent = json.load(f)
    if args.previous_response_json_path:
        with open(args.previous_response_json_path, "r", encoding="utf-8") as f:
            previous_payload = json.load(f)
        previous_response_payload = previous_payload.get("response_payload", previous_payload)
        if not provided_intent:
            provided_intent = dict(previous_payload.get("intent", {}) or {})
    if args.previous_response_json_path and provided_intent:
        previous_recommended = (previous_response_payload.get("recommended_option") or {})
        if previous_recommended:
            provided_intent["previous_recommendation"] = previous_recommended
    feedback_event: Dict[str, Any] = {}
    feedback_control: Dict[str, Any] = {}
    feedback_parse_error = ""
    is_resume_turn = bool(args.session_id and (args.feedback_json_path or args.replan_event_json_path or args.turn_type == "selection"))
    if is_resume_turn:
        if args.feedback_json_path:
            with open(args.feedback_json_path, "r", encoding="utf-8") as f:
                raw_feedback_event = json.load(f)
            resume_payload = {
                "action": "selection" if args.turn_type == "selection" else "feedback",
                "feedback_event": raw_feedback_event,
                "selected_option_ref": args.selected_option_ref or str(raw_feedback_event.get("target_option", "") or ""),
            }
        elif args.replan_event_json_path:
            with open(args.replan_event_json_path, "r", encoding="utf-8") as f:
                replan_event = json.load(f)
            resume_payload = {
                "action": "replan",
                "replan_event": replan_event,
                "reason": args.user_request,
            }
    else:
        if args.feedback_json_path:
            try:
                with open(args.feedback_json_path, "r", encoding="utf-8") as f:
                    raw_feedback_event = json.load(f)
                feedback_event = parse_feedback_event_with_fallback(
                    raw_feedback_event=raw_feedback_event,
                    previous_response_payload=previous_response_payload,
                    lmstudio_base_url=args.lmstudio_base_url,
                    model=args.model,
                    llm_timeout_sec=args.llm_timeout_sec,
                    llm_max_retries=args.llm_max_retries,
                    enable_thinking=args.enable_thinking,
                )
                if not provided_intent:
                    raise ValueError("--feedback-json-path requires --previous-response-json-path or --intent-json-path.")
                provided_intent, feedback_control = apply_feedback_event(
                    provided_intent,
                    feedback_event,
                    previous_response_payload=previous_response_payload,
                    selected_option_ref=args.selected_option_ref or "",
                )
            except Exception as exc:
                feedback_parse_error = str(exc)
        if args.replan_event_json_path:
            with open(args.replan_event_json_path, "r", encoding="utf-8") as f:
                replan_event = json.load(f)
            if provided_intent:
                provided_intent = apply_replan_event(provided_intent, replan_event)

    if feedback_parse_error:
        request_id = uuid4().hex
        response_payload = {
            "status": "error",
            "summary": {
                "selection_state": "planning",
                "active_preferences": [],
            },
            "recommended_option": None,
            "alternative_options": [],
            "selected_option": None,
            "selection_summary": {},
            "execution_ready_share_text": "",
            "execution_ready_share_card": {},
            "next_actions": [],
            "suggestions": [
                "请把反馈表达成等待、绕路、换乘、点位偏好，或者直接选择一个方案。"
            ],
            "relaxation_suggestions": [],
            "primary_bottleneck": None,
            "constraint_diagnostics": {},
            "share_text": "",
            "share_card": {},
            "error": {
                "code": "feedback_parse_failed",
                "message": feedback_parse_error,
            },
        }
        payload = {
            "request_id": request_id,
            "session_id": args.session_id or "",
            "thread_id": args.session_id or request_id,
            "turn_type": args.turn_type,
            "status": "error",
            "error": feedback_parse_error,
            "retry_count": 0,
            "strategy_plan": {},
            "feedback_event": feedback_event,
            "judge_result": {},
            "response_payload": response_payload,
            "metrics_summary": build_run_metrics(
                request_id=request_id,
                user_request=args.user_request,
                intent=provided_intent,
                plan_result={},
                response_payload=response_payload,
                judge_result={},
                retry_count=0,
                failure_category="feedback_parse_failed",
            ),
            "natural_language_output": f"本次规划失败：{feedback_parse_error}",
            "intent": provided_intent,
            "result": {},
        }
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
            print(payload["natural_language_output"])
        if args.session_id:
            metrics = dict(payload.get("metrics_summary", {}) or {})
            metrics["feedback_event"] = dict(feedback_event or {})
            persist_turn_state(
                project_root,
                session_id=args.session_id,
                turn_type=args.turn_type,
                user_input=args.user_request,
                intent=provided_intent,
                response_payload=response_payload,
                metrics_summary=metrics,
            )
        return 1

    if feedback_control.get("selection_only") and not is_resume_turn:
        selected_option = feedback_control.get("selected_option") or resolve_option_reference(
            previous_response_payload,
            args.selected_option_ref or feedback_event.get("target_option", ""),
        )
        payload = build_selection_payload(
            previous_response_payload=previous_response_payload,
            selected_option=selected_option,
            selected_option_ref=args.selected_option_ref or feedback_event.get("target_option", ""),
            feedback_event=feedback_event,
        )
        final_payload = {
            "request_id": uuid4().hex,
            "session_id": args.session_id or "",
            "thread_id": args.session_id or "",
            "turn_type": "selection",
            "status": "selected",
            "error": "",
            "retry_count": 0,
            "strategy_plan": {},
            "feedback_event": feedback_event,
            "judge_result": {},
            "response_payload": payload,
            "metrics_summary": build_run_metrics(
                request_id=uuid4().hex,
                user_request=args.user_request,
                intent=provided_intent,
                plan_result={"options": [selected_option] if selected_option else []},
                response_payload=payload,
                judge_result={},
                retry_count=0,
                failure_category="",
            ),
            "natural_language_output": payload.get("execution_ready_share_text", ""),
            "intent": provided_intent,
            "result": previous_payload.get("result", {}),
        }
        if args.output_json_path:
            output_path = Path(args.output_json_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(final_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if args.intent_output_json_path:
            intent_output_path = Path(args.intent_output_json_path)
            intent_output_path.parent.mkdir(parents=True, exist_ok=True)
            intent_output_path.write_text(
                json.dumps(provided_intent, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if args.json_stdout:
            print(json.dumps(final_payload, ensure_ascii=False, indent=2))
        else:
            print(final_payload["natural_language_output"])
        if args.session_id:
            metrics = dict(final_payload.get("metrics_summary", {}) or {})
            metrics["feedback_event"] = dict(feedback_event or {})
            persist_turn_state(
                project_root,
                session_id=args.session_id,
                turn_type="selection",
                user_input=args.user_request,
                intent=provided_intent,
                response_payload=payload,
                metrics_summary=metrics,
            )
        return 0

    app = build_graph(amap_key=amap_key)
    init_state: AgentState = {
        "request_id": uuid4().hex,
        "session_id": args.session_id or "",
        "thread_id": args.session_id or "",
        "turn_type": args.turn_type,
        "user_request": args.user_request,
        "intent": provided_intent,
        "strategy_plan": {},
        "feedback_event": feedback_event,
        "feedback_control": feedback_control,
        "followup_payload": {},
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
    invoke_config = {
        "configurable": {
            "thread_id": args.session_id or init_state["request_id"],
        }
    }
    if is_resume_turn:
        final_state = app.invoke(Command(resume=resume_payload), config=invoke_config)
    else:
        final_state = app.invoke(init_state, config=invoke_config)

    if args.print_intent and final_state.get("intent"):
        print("Parsed intent:")
        print(json.dumps(final_state.get("intent", {}), ensure_ascii=False, indent=2))

    status = infer_response_status(
        error=final_state.get("error", ""),
        result=final_state.get("plan_result", {}),
    )
    payload = {
        "request_id": final_state.get("request_id", ""),
        "session_id": final_state.get("session_id", ""),
        "thread_id": final_state.get("thread_id", ""),
        "turn_type": final_state.get("turn_type", ""),
        "status": status,
        "error": final_state.get("error", ""),
        "retry_count": final_state.get("retry_count", 0),
        "strategy_plan": final_state.get("strategy_plan", {}),
        "feedback_event": final_state.get("feedback_event", {}),
        "judge_result": final_state.get("judge_result", {}),
        "response_payload": final_state.get("response_payload", {}),
        "metrics_summary": final_state.get("metrics_summary", {}),
        "natural_language_output": final_state.get("response_text", ""),
        "intent": final_state.get("intent", {}),
        "result": final_state.get("plan_result", {}),
        "awaiting_feedback": "__interrupt__" in final_state,
        "interrupt_payload": serialize_interrupts(final_state.get("__interrupt__", [])),
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
