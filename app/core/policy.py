from __future__ import annotations

from typing import Any, Dict

from .schemas import StrategyPlan


DEFAULT_AUTO_KEYWORDS = "地铁站|公交站|停车场|商场"


def build_default_strategy() -> StrategyPlan:
    return {
        "strategy_type": "balanced",
        "reason": "Default balanced strategy for first attempt.",
        "constraint_adjustments": {},
        "auto_pickup_adjustments": {},
    }


def build_fallback_strategy(last_error: str, retry_count: int) -> StrategyPlan:
    auto_expand = {
        "keywords": DEFAULT_AUTO_KEYWORDS,
        "radius_m": 1500 + retry_count * 300,
        "limit": 25 + retry_count * 5,
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
            "max_wait_min": 10,
            "driver_detour_max_min": 15,
            "passenger_travel_max_min": 15,
        },
        "auto_pickup_adjustments": auto_expand,
    }


def apply_strategy_to_intent(intent: Dict[str, Any], strategy: StrategyPlan) -> Dict[str, Any]:
    patched = dict(intent)
    constraints = dict(patched.get("constraints", {}))
    for key, delta in strategy.get("constraint_adjustments", {}).items():
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

