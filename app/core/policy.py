from __future__ import annotations

from typing import Any, Dict

from .schemas import JudgePolicy, StrategyPlan, StrategyPolicy


DEFAULT_AUTO_KEYWORDS = "地铁站|公交站|停车场|商场"


def default_strategy_policy() -> StrategyPolicy:
    return {
        "step_wait_min": 10,
        "step_detour_min": 15,
        "step_passenger_travel_min": 15,
        "base_auto_radius_m": 1500,
        "auto_radius_step_m": 300,
        "base_auto_limit": 25,
        "auto_limit_step": 5,
        "default_keywords": DEFAULT_AUTO_KEYWORDS,
    }


def default_judge_policy() -> JudgePolicy:
    return {
        "max_avg_wait_min": 45,
        "max_avg_detour_min": 90,
        "min_options_required": 1,
    }


def build_default_strategy(policy: StrategyPolicy) -> StrategyPlan:
    return {
        "strategy_type": "balanced",
        "reason": "Default balanced strategy for first attempt.",
        "constraint_adjustments": {},
        "auto_pickup_adjustments": {
            "keywords": policy["default_keywords"],
            "radius_m": policy["base_auto_radius_m"],
            "limit": policy["base_auto_limit"],
        },
    }


def build_fallback_strategy(
    last_error: str,
    retry_count: int,
    policy: StrategyPolicy,
) -> StrategyPlan:
    auto_expand = {
        "keywords": policy["default_keywords"],
        "radius_m": policy["base_auto_radius_m"] + retry_count * policy["auto_radius_step_m"],
        "limit": policy["base_auto_limit"] + retry_count * policy["auto_limit_step"],
    }
    if "No pickup candidates generated automatically" in last_error:
        return {
            "strategy_type": "balanced",
            "reason": "Expand POI search when auto candidates are insufficient.",
            "constraint_adjustments": {},
            "auto_pickup_adjustments": auto_expand,
        }
    return {
        "strategy_type": "balanced",
        "reason": "Relax constraints progressively when no feasible option is found.",
        "constraint_adjustments": {
            "max_wait_min": policy["step_wait_min"],
            "driver_detour_max_min": policy["step_detour_min"],
            "passenger_travel_max_min": policy["step_passenger_travel_min"],
        },
        "auto_pickup_adjustments": auto_expand,
    }


def evaluate_plan_quality(
    *,
    plan_result: Dict[str, Any],
    error: str,
    judge_policy: JudgePolicy,
) -> Dict[str, Any]:
    if error:
        return {
            "pass_": False,
            "reason": "Planner error occurred.",
            "score": 0.0,
            "risks": [error],
        }

    options = plan_result.get("options", [])
    if len(options) < int(judge_policy["min_options_required"]):
        return {
            "pass_": False,
            "reason": "Insufficient feasible options.",
            "score": 0.0,
            "risks": ["insufficient_options"],
        }

    wait_values = [
        int(x.get("optimized_wait_time_min", x.get("pickup_wait_time_min", 0))) for x in options
    ]
    detour_values = [int(x.get("driver_detour_time_min", 0)) for x in options]
    avg_wait = sum(wait_values) / max(len(wait_values), 1)
    avg_detour = sum(detour_values) / max(len(detour_values), 1)
    risks = []
    if avg_wait > int(judge_policy["max_avg_wait_min"]):
        risks.append("avg_wait_too_high")
    if avg_detour > int(judge_policy["max_avg_detour_min"]):
        risks.append("avg_detour_too_high")

    pass_ = True
    score = 1.0 / (1.0 + avg_wait + avg_detour)
    return {
        "pass_": pass_,
        "reason": "Feasible options found." if not risks else "Feasible options found, but average wait or detour is relatively high.",
        "score": round(score, 4),
        "risks": risks,
    }


def apply_strategy_to_intent(intent: Dict[str, Any], strategy: StrategyPlan) -> Dict[str, Any]:
    patched = dict(intent)
    constraints = dict(patched.get("constraints", {}))
    for key, delta in strategy.get("constraint_adjustments", {}).items():
        if key in constraints:
            constraints[key] = int(constraints.get(key, 0)) + int(delta)
    patched["constraints"] = constraints

    auto = dict(patched.get("auto_pickup", {}))
    for key, value in strategy.get("auto_pickup_adjustments", {}).items():
        if key in {"radius_m", "limit"}:
            auto[key] = max(int(auto.get(key, 0)), int(value))
        else:
            auto[key] = value
    if not str(auto.get("keywords", "")).strip():
        auto["keywords"] = DEFAULT_AUTO_KEYWORDS
    patched["auto_pickup"] = auto
    return patched
