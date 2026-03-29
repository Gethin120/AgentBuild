from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Any, Dict, TypedDict

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
    user_request: str
    intent: Dict[str, Any]
    plan_result: Dict[str, Any]
    error: str
    retry_count: int
    max_retries: int
    show_diagnostics: bool
    amap_key: str
    lmstudio_base_url: str
    model: str
    llm_timeout_sec: int
    llm_max_retries: int


def parse_intent_node(state: AgentState) -> AgentState:
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
    return state


def plan_node(state: AgentState) -> AgentState:
    try:
        result = run_plan(
            intent=state["intent"],
            amap_key=state["amap_key"],
            show_diagnostics=state["show_diagnostics"],
        )
        state["plan_result"] = result
        state["error"] = ""
        return state
    except Exception as exc:
        state["error"] = str(exc)
        return state


def assess_node(state: AgentState) -> AgentState:
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
        return state

    options = state.get("plan_result", {}).get("options", [])
    if options:
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
    return state


def route_after_plan(state: AgentState) -> str:
    if state.get("error"):
        if state.get("retry_count", 0) < state.get("max_retries", 1):
            return "assess"
        return "end"
    return "assess"


def route_after_assess(state: AgentState) -> str:
    if state.get("error"):
        return "end"

    options = state.get("plan_result", {}).get("options", [])
    if options:
        return "end"

    if state.get("retry_count", 0) < state.get("max_retries", 1):
        return "plan"
    return "end"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("parse_intent", parse_intent_node)
    graph.add_node("plan", plan_node)
    graph.add_node("assess", assess_node)

    graph.set_entry_point("parse_intent")
    graph.add_edge("parse_intent", "plan")
    graph.add_conditional_edges("plan", route_after_plan, {"assess": "assess", "end": END})
    graph.add_conditional_edges("assess", route_after_assess, {"plan": "plan", "end": END})
    return graph.compile()


def main() -> int:
    parser = argparse.ArgumentParser(description="结伴而行 Agent (LangGraph + LM Studio)")
    parser.add_argument("--user-request", required=True)
    parser.add_argument("--amap-key", default=os.getenv("AMAP_WEB_SERVICE_KEY"))
    parser.add_argument("--lmstudio-base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--model", default="qwen/qwen3.5-9b")
    parser.add_argument("--show-diagnostics", action="store_true")
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--print-intent", action="store_true")
    parser.add_argument("--llm-timeout-sec", type=int, default=180)
    parser.add_argument("--llm-max-retries", type=int, default=2)
    args = parser.parse_args()

    if not args.amap_key:
        raise ValueError("AMap key is required: pass --amap-key or set AMAP_WEB_SERVICE_KEY.")

    app = build_graph()
    init_state: AgentState = {
        "user_request": args.user_request,
        "intent": {},
        "plan_result": {},
        "error": "",
        "retry_count": 0,
        "max_retries": args.max_retries,
        "show_diagnostics": args.show_diagnostics,
        "amap_key": args.amap_key,
        "lmstudio_base_url": args.lmstudio_base_url,
        "model": args.model,
        "llm_timeout_sec": args.llm_timeout_sec,
        "llm_max_retries": args.llm_max_retries,
    }
    final_state = app.invoke(init_state)

    if final_state.get("intent") and args.print_intent:
        print("Parsed intent:")
        print(json.dumps(final_state["intent"], ensure_ascii=False, indent=2))

    if final_state.get("error"):
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": final_state["error"],
                    "retry_count": final_state.get("retry_count", 0),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "status": "ok",
                "retry_count": final_state.get("retry_count", 0),
                "result": final_state.get("plan_result", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
