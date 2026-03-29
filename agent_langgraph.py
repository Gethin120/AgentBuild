from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, TypedDict

try:
    from langgraph.graph import END, StateGraph
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency: langgraph.\n"
        "Install with: python3 -m pip install --user langgraph"
    ) from exc

from agent_local import (
    call_lmstudio_chat,
    extract_json_object,
    intent_prompt_template,
    run_plan,
    sanitize_intent,
    validate_intent,
)


class AgentState(TypedDict):
    intent: Dict[str, Any]
    plan_result: Dict[str, Any]
    error: str
    retry_count: int
    max_retries: int
    show_diagnostics: bool
    lmstudio_base_url: str
    model: str
    llm_timeout_sec: int
    llm_max_retries: int
    planner_timeout_sec: int
    planner_max_retries: int
    progress: bool
    trace_events: list[Dict[str, Any]]


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
    emit_progress(state.get("progress", False), stage, status, message, extra)
    state.setdefault("trace_events", []).append(event)


def assess_node(state: AgentState) -> AgentState:
    record_event(state, "graph.assess", "start", "Assessing result / retry policy")
    if state.get("error"):
        error_text = state.get("error", "")
        # Recovery for bad auto-pickup settings (often from imperfect model extraction).
        if (
            "No pickup candidates generated automatically" in error_text
            and state.get("retry_count", 0) < state.get("max_retries", 1)
        ):
            intent = state["intent"]
            auto = intent.get("auto_pickup", {})
            auto["keywords"] = "地铁站|公交站|停车场|商场"
            auto["radius_m"] = max(int(auto.get("radius_m", 1000)), 1500)
            auto["limit"] = max(int(auto.get("limit", 20)), 25)
            intent["auto_pickup"] = auto
            state["intent"] = intent
            state["retry_count"] = state.get("retry_count", 0) + 1
            state["error"] = ""
            record_event(
                state,
                "graph.assess",
                "retry",
                "Recovered from candidate-generation error and will retry",
                {"retry_count": state["retry_count"]},
            )
        return state

    options = state.get("plan_result", {}).get("options", [])
    if options:
        record_event(
            state,
            "graph.assess",
            "done",
            "Feasible options found",
            {"options": len(options)},
        )
        return state

    # No feasible option: relax constraints once/twice as an agent decision.
    intent = state["intent"]
    constraints = intent.get("constraints", {})
    constraints["max_wait_min"] = int(constraints.get("max_wait_min", 45)) + 15
    constraints["driver_detour_max_min"] = int(constraints.get("driver_detour_max_min", 90)) + 20
    constraints["passenger_travel_max_min"] = int(
        constraints.get("passenger_travel_max_min", 120)
    ) + 20
    intent["constraints"] = constraints
    state["intent"] = intent
    state["retry_count"] = state.get("retry_count", 0) + 1
    record_event(
        state,
        "graph.assess",
        "retry",
        "No feasible option; relaxed constraints for retry",
        {"retry_count": state["retry_count"], "constraints": constraints},
    )
    return state


def _short_stage(stage: str) -> str:
    mapping = {
        "graph": "Graph",
        "graph.parse_intent": "ParseIntent",
        "graph.plan": "PlanNode",
        "plan": "PreparePlan",
        "candidates": "Candidates",
        "routing": "Routing",
        "graph.assess": "Assess",
    }
    return mapping.get(stage, stage)


def build_flow_report_markdown(state: AgentState, started_perf_counter: float) -> str:
    events = state.get("trace_events", [])
    if not events:
        return "# Flow Report\n\nNo events captured."

    important = []
    for e in events:
        st = e.get("stage")
        status = e.get("status")
        if status in {"start", "done", "retry", "error"} and st in {
            "graph",
            "graph.parse_intent",
            "graph.plan",
            "plan",
            "candidates",
            "routing",
            "graph.assess",
        }:
            important.append(e)

    lines = []
    lines.append("# Flow Report")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- elapsed_sec: {round(time.perf_counter() - started_perf_counter, 2)}")
    lines.append(f"- retry_count: {state.get('retry_count', 0)}")
    lines.append(f"- status: {'error' if state.get('error') else 'ok'}")
    lines.append("")
    lines.append("## Key Steps")
    for idx, e in enumerate(important, start=1):
        extra = e.get("extra")
        suffix = f" | extra={extra}" if extra else ""
        lines.append(
            f"{idx}. [{e.get('time')}] {_short_stage(str(e.get('stage')))}::{e.get('status')} - {e.get('message')}{suffix}"
        )

    lines.append("")
    lines.append("## Mermaid")
    lines.append("```mermaid")
    lines.append("flowchart TD")
    for idx, e in enumerate(important, start=1):
        node_id = f"N{idx}"
        label = f"{_short_stage(str(e.get('stage')))} | {e.get('status')}"
        lines.append(f'    {node_id}["{label}"]')
        if idx > 1:
            prev = f"N{idx-1}"
            lines.append(f"    {prev} --> {node_id}")
    lines.append("```")
    return "\n".join(lines)


def route_after_plan(state: AgentState) -> str:
    if state.get("error"):
        if state.get("retry_count", 0) < state.get("max_retries", 1):
            return "assess"
        return "end"
    return "assess"


def route_after_parse(state: AgentState) -> str:
    if state.get("error"):
        return "end"
    return "plan"


def route_after_assess(state: AgentState) -> str:
    if state.get("error"):
        return "end"

    options = state.get("plan_result", {}).get("options", [])
    if options:
        return "end"

    if state.get("retry_count", 0) < state.get("max_retries", 1):
        return "plan"
    return "end"


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


def _compact_plan_result(result: Dict[str, Any]) -> Dict[str, Any]:
    resolved = result.get("resolved_locations", {})
    compact_resolved: Dict[str, Any] = {}
    for key in ("driver_origin", "passenger_origin", "destination"):
        item = resolved.get(key, {})
        compact_resolved[key] = {
            "name": item.get("name"),
            "lat": item.get("lat"),
            "lon": item.get("lon"),
        }

    compact_options = []
    for option in result.get("options", []):
        poi = option.get("pickup_poi") or {"name": option.get("pickup_point")}
        to_pickup = option.get("to_pickup_plan") or {}
        compact_options.append(
            {
                "pickup_poi": {
                    "name": poi.get("name"),
                    "lat": poi.get("lat"),
                    "lon": poi.get("lon"),
                },
                "to_pickup_plan": {
                    "driver": {
                        "mode": ((to_pickup.get("driver") or {}).get("mode")),
                        "travel_time_min": ((to_pickup.get("driver") or {}).get("travel_time_min")),
                        "eta_to_pickup": ((to_pickup.get("driver") or {}).get("eta_to_pickup")),
                    },
                    "passenger": {
                        "mode": ((to_pickup.get("passenger") or {}).get("mode")),
                        "travel_time_min": ((to_pickup.get("passenger") or {}).get("travel_time_min")),
                        "eta_to_pickup": ((to_pickup.get("passenger") or {}).get("eta_to_pickup")),
                    },
                },
                "score": option.get("score"),
                "pickup_wait_time_min": option.get("pickup_wait_time_min"),
                "driver_detour_time_min": option.get("driver_detour_time_min"),
                "total_arrival_time": option.get("total_arrival_time"),
            }
        )

    compact: Dict[str, Any] = {
        "resolved_locations": compact_resolved,
        "pickup_candidates_count": result.get("pickup_candidates_count", 0),
        "options": compact_options,
    }
    diagnostics = result.get("diagnostics")
    if diagnostics is not None:
        compact["diagnostics"] = diagnostics
    return compact


def _build_natural_language_summary(state: AgentState) -> str:
    if state.get("error"):
        return f"本次规划失败，原因是：{state.get('error')}"

    result = state.get("plan_result", {}) or {}
    resolved = result.get("resolved_locations", {}) or {}
    driver_name = ((resolved.get("driver_origin") or {}).get("name")) or "司机出发点"
    passenger_name = ((resolved.get("passenger_origin") or {}).get("name")) or "乘客出发点"
    destination_name = ((resolved.get("destination") or {}).get("name")) or "目的地"
    options = result.get("options") or []

    if not options:
        return (
            f"已完成从{driver_name}与{passenger_name}前往{destination_name}的会合规划，"
            "但当前约束下没有可行方案。建议放宽乘客通勤时长、司机绕路时长或等待时间后重试。"
        )

    lines = [
        f"已为你完成结伴出行规划：司机从{driver_name}出发，乘客从{passenger_name}出发，最终前往{destination_name}。",
        f"共找到 {len(options)} 个可行会合方案，按推荐优先级如下：",
    ]
    for idx, option in enumerate(options, start=1):
        poi = option.get("pickup_poi") or {}
        poi_name = poi.get("name") or "未命名会合点"
        wait_min = option.get("pickup_wait_time_min")
        detour_min = option.get("driver_detour_time_min")
        eta = option.get("total_arrival_time")
        score = option.get("score")
        to_pickup = option.get("to_pickup_plan") or {}
        driver_plan = to_pickup.get("driver") or {}
        passenger_plan = to_pickup.get("passenger") or {}
        driver_mode = driver_plan.get("mode") or "driving"
        passenger_mode = passenger_plan.get("mode") or "transit"
        driver_travel = driver_plan.get("travel_time_min")
        passenger_travel = passenger_plan.get("travel_time_min")
        driver_eta = driver_plan.get("eta_to_pickup")
        passenger_eta = passenger_plan.get("eta_to_pickup")
        lines.append(
            f"{idx}. 会合点「{poi_name}」，预计等待 {wait_min} 分钟，司机绕路 {detour_min} 分钟，"
            f"预计到达目的地时间 {eta}，综合评分 {score}。"
        )
        lines.append(
            f"   前往会合点：你使用 {driver_mode} 约 {driver_travel} 分钟（预计 {driver_eta} 到达）；"
            f"朋友使用 {passenger_mode} 约 {passenger_travel} 分钟（预计 {passenger_eta} 到达）。"
        )

    diagnostics = result.get("diagnostics") or []
    if diagnostics:
        lines.append(f"另外有 {len(diagnostics)} 个候选会合点因约束不满足被过滤。")
    return "\n".join(lines)


def build_graph(user_request: str, amap_key: str):
    def parse_intent_node(state: AgentState) -> AgentState:
        record_event(state, "graph.parse_intent", "start", "Parsing intent")
        try:
            system_prompt = intent_prompt_template(datetime.now().isoformat(timespec="minutes"))
            model_output = call_lmstudio_chat(
                base_url=state["lmstudio_base_url"],
                model=state["model"],
                system_prompt=system_prompt,
                user_prompt=user_request,
                timeout_sec=state.get("llm_timeout_sec", 180),
                max_retries=state.get("llm_max_retries", 2),
            )
            intent = extract_json_object(model_output)
            validate_intent(intent)
            intent = sanitize_intent(intent)

            state["intent"] = intent
            state["error"] = ""
            record_event(state, "graph.parse_intent", "done", "Intent parsed")
            return state
        except Exception as exc:
            state["error"] = f"parse_intent_failed: {exc}"
            record_event(
                state,
                "graph.parse_intent",
                "error",
                "Intent parsing failed",
                {"error": state["error"]},
            )
            return state

    def plan_node(state: AgentState) -> AgentState:
        record_event(state, "graph.plan", "start", "Running deterministic planner")
        planner_timeout_sec = max(10, int(state.get("planner_timeout_sec", 120)))
        planner_attempts = max(1, int(state.get("planner_max_retries", 1)) + 1)
        last_error = ""
        for attempt in range(1, planner_attempts + 1):
            try:
                result = _run_with_timeout(
                    lambda: run_plan(
                        intent=state["intent"],
                        amap_key=amap_key,
                        show_diagnostics=state["show_diagnostics"],
                        progress_enabled=state.get("progress", False),
                        progress_hook=lambda e: state.setdefault("trace_events", []).append(
                            {**e, "epoch": time.time()}
                        ),
                    ),
                    timeout_sec=planner_timeout_sec,
                )
                compact_result = _compact_plan_result(result)
                state["plan_result"] = compact_result
                state["error"] = ""
                record_event(
                    state,
                    "graph.plan",
                    "done",
                    "Planner returned result",
                    {
                        "options": len((compact_result or {}).get("options", [])),
                        "attempt": attempt,
                    },
                )
                return state
            except FuturesTimeoutError:
                last_error = f"planner_timeout: exceeded {planner_timeout_sec}s"
            except Exception as exc:
                last_error = str(exc)

            is_last_attempt = attempt >= planner_attempts
            if is_last_attempt or not _is_retryable_error(last_error):
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
                "Planner call failed, retrying",
                {"attempt": attempt, "error": last_error},
            )
            time.sleep(0.6 * attempt)

        state["error"] = last_error or "planner_failed_unknown"
        record_event(
            state,
            "graph.plan",
            "error",
            "Planner failed",
            {"error": state["error"]},
        )
        return state

    graph = StateGraph(AgentState)
    graph.add_node("parse_intent", parse_intent_node)
    graph.add_node("plan", plan_node)
    graph.add_node("assess", assess_node)

    graph.set_entry_point("parse_intent")
    graph.add_conditional_edges("parse_intent", route_after_parse, {"plan": "plan", "end": END})
    graph.add_conditional_edges("plan", route_after_plan, {"assess": "assess", "end": END})
    graph.add_conditional_edges("assess", route_after_assess, {"plan": "plan", "end": END})
    return graph.compile()


def main() -> int:
    parser = argparse.ArgumentParser(description="结伴而行 Agent (LangGraph + LM Studio)")
    parser.add_argument("--user-request", required=True)
    parser.add_argument("--lmstudio-base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--model", default="qwen/qwen3.5-9b")
    parser.add_argument("--show-diagnostics", action="store_true")
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--print-intent", action="store_true")
    parser.add_argument("--llm-timeout-sec", type=int, default=180)
    parser.add_argument("--llm-max-retries", type=int, default=2)
    parser.add_argument("--planner-timeout-sec", type=int, default=120)
    parser.add_argument("--planner-max-retries", type=int, default=2)
    parser.add_argument("--progress", action="store_true", help="Print graph stage progress to stderr")
    parser.add_argument(
        "--flow-report-dir",
        default=".runs",
        help="Directory to write per-run flow report markdown",
    )
    args = parser.parse_args()

    amap_key = os.getenv("AMAP_WEB_SERVICE_KEY", "").strip()
    if not amap_key:
        raise ValueError("AMAP_WEB_SERVICE_KEY is required in server environment.")

    overall_started = time.perf_counter()
    emit_progress(args.progress, "graph", "start", "LangGraph agent run started")
    app = build_graph(user_request=args.user_request, amap_key=amap_key)
    init_state: AgentState = {
        "intent": {},
        "plan_result": {},
        "error": "",
        "retry_count": 0,
        "max_retries": args.max_retries,
        "show_diagnostics": args.show_diagnostics,
        "lmstudio_base_url": args.lmstudio_base_url,
        "model": args.model,
        "llm_timeout_sec": args.llm_timeout_sec,
        "llm_max_retries": args.llm_max_retries,
        "planner_timeout_sec": args.planner_timeout_sec,
        "planner_max_retries": args.planner_max_retries,
        "progress": args.progress,
        "trace_events": [],
    }
    final_state = app.invoke(init_state)

    if final_state.get("intent") and args.print_intent:
        print("Parsed intent:")
        print(json.dumps(final_state["intent"], ensure_ascii=False, indent=2))

    if final_state.get("error"):
        emit_progress(
            args.progress,
            "graph",
            "error",
            "LangGraph agent run failed",
            {
                "retry_count": final_state.get("retry_count", 0),
                "elapsed_sec": round(time.perf_counter() - overall_started, 2),
            },
        )
        result_obj = {
            "status": "error",
            "error": final_state["error"],
            "retry_count": final_state.get("retry_count", 0),
            "natural_language_output": _build_natural_language_summary(final_state),
        }
        print(
            json.dumps(
                result_obj,
                ensure_ascii=False,
                indent=2,
            )
        )
        report_dir = Path(args.flow_report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = report_dir / f"flow_{ts}.md"
        report_path.write_text(
            build_flow_report_markdown(final_state, overall_started), encoding="utf-8"
        )
        emit_progress(
            args.progress,
            "graph",
            "report",
            "Flow report generated",
            {"path": str(report_path)},
        )
        return 1

    report_dir = Path(args.flow_report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"flow_{ts}.md"
    report_path.write_text(
        build_flow_report_markdown(final_state, overall_started), encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "status": "ok",
                "retry_count": final_state.get("retry_count", 0),
                "flow_report_path": str(report_path),
                "natural_language_output": _build_natural_language_summary(final_state),
                "result": final_state.get("plan_result", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    emit_progress(
        args.progress,
        "graph",
        "done",
        "LangGraph agent run completed",
        {
            "retry_count": final_state.get("retry_count", 0),
            "elapsed_sec": round(time.perf_counter() - overall_started, 2),
            "flow_report_path": str(report_path),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
