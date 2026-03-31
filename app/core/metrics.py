from __future__ import annotations

from typing import Any, Dict, List


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _constraint_hit_rate(intent: Dict[str, Any], options: List[Dict[str, Any]]) -> float:
    if not options:
        return 0.0

    constraints = intent.get("constraints", {})
    max_wait = _safe_int(constraints.get("max_wait_min", 0))
    max_detour = _safe_int(constraints.get("driver_detour_max_min", 0))

    hits = 0
    for option in options:
        wait_ok = _safe_int(option.get("pickup_wait_time_min")) <= max_wait if max_wait > 0 else True
        detour_ok = (
            _safe_int(option.get("driver_detour_time_min")) <= max_detour if max_detour > 0 else True
        )
        if wait_ok and detour_ok:
            hits += 1
    return round(hits / len(options), 4)


def _candidate_utilization(result: Dict[str, Any]) -> float:
    candidate_count = _safe_int(result.get("pickup_candidates_count", 0))
    options_count = len(result.get("options", []) or [])
    if candidate_count <= 0:
        return 0.0
    return round(options_count / candidate_count, 4)


def build_run_metrics(
    *,
    request_id: str,
    user_request: str,
    intent: Dict[str, Any],
    plan_result: Dict[str, Any],
    response_payload: Dict[str, Any],
    judge_result: Dict[str, Any],
    retry_count: int,
    failure_category: str,
) -> Dict[str, Any]:
    options = plan_result.get("options", []) or []
    status = str(response_payload.get("status", "error"))
    recommended = response_payload.get("recommended_option") or {}
    constraint_diagnostics = response_payload.get("constraint_diagnostics") or {}
    replan_delta = ((response_payload.get("summary") or {}).get("replan_delta") or {})

    return {
        "request_id": request_id,
        "user_request": user_request,
        "status": status,
        "preference_profile": str(intent.get("preference_profile", "balanced")),
        "active_preferences": list(((response_payload.get("summary") or {}).get("active_preferences", []) or [])),
        "is_replan": bool(intent.get("replan_context")),
        "replan_type": str(((intent.get("replan_context") or {}).get("type", ""))),
        "selected_option_ref": str(((response_payload.get("selection_summary") or {}).get("selected_option_ref", ""))),
        "pickup_changed_on_replan": bool(replan_delta.get("pickup_changed", False)),
        "wait_delta_min": _safe_int(replan_delta.get("wait_delta_min", 0)),
        "detour_delta_min": _safe_int(replan_delta.get("detour_delta_min", 0)),
        "arrival_delta_min": _safe_int(replan_delta.get("arrival_delta_min", 0)),
        "raw_wait_min": _safe_int(((response_payload.get("summary") or {}).get("raw_wait_min", 0))),
        "optimized_wait_min": _safe_int(((response_payload.get("summary") or {}).get("optimized_wait_min", 0))),
        "departure_shift_min": _safe_int(((response_payload.get("summary") or {}).get("departure_shift_min", 0))),
        "departure_shift_role": str(((response_payload.get("summary") or {}).get("departure_shift_role", ""))),
        "success_flag": 1 if status == "ok" else 0,
        "selected_flag": 1 if status == "selected" else 0,
        "no_solution_flag": 1 if status == "no_solution" else 0,
        "error_flag": 1 if status == "error" else 0,
        "feasible_option_count": len(options),
        "candidate_count": _safe_int(plan_result.get("pickup_candidates_count", 0)),
        "constraint_hit_rate": _constraint_hit_rate(intent, options),
        "candidate_utilization": _candidate_utilization(plan_result),
        "retry_count": retry_count,
        "judge_pass": bool(judge_result.get("pass_", False)),
        "judge_score": float(judge_result.get("score", 0.0) or 0.0),
        "failure_category": failure_category,
        "primary_bottleneck": response_payload.get("primary_bottleneck"),
        "filtered_candidate_count": int(constraint_diagnostics.get("filtered_candidate_count", 0) or 0),
        "reason_counts": dict(constraint_diagnostics.get("reason_counts", {}) or {}),
        "avg_exceed_by_reason": dict(constraint_diagnostics.get("avg_exceed_by_reason", {}) or {}),
        "recommendation_basis": recommended.get("recommendation_basis"),
        "recommendation_tags": list(recommended.get("tradeoff_tags", []) or []),
        "preference_alignment": bool(recommended.get("preference_alignment", False)),
        "relaxation_fields": [
            item.get("field")
            for item in (response_payload.get("relaxation_suggestions", []) or [])
            if isinstance(item, dict) and item.get("field")
        ],
    }
