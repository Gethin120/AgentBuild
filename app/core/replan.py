from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def apply_replan_event(intent: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    patched = deepcopy(intent)
    event_type = str(event.get("type", "") or "").strip()
    delay_min = _safe_int(event.get("delay_min", 0), 0)
    delta_min = _safe_int(event.get("delta_min", 0), 0)

    constraints = dict(patched.get("constraints", {}) or {})
    auto_pickup = dict(patched.get("auto_pickup", {}) or {})
    changes = []

    if event_type == "passenger_delay":
        before = _safe_int(patched.get("passenger_departure_delay_min", 0), 0)
        patched["passenger_departure_delay_min"] = _safe_int(
            patched.get("passenger_departure_delay_min", 0),
            0,
        ) + max(delay_min, 0)
        changes.append(
            {
                "field": "passenger_departure_delay_min",
                "before": before,
                "after": patched["passenger_departure_delay_min"],
            }
        )
    elif event_type == "driver_delay":
        before = _safe_int(patched.get("driver_departure_delay_min", 0), 0)
        patched["driver_departure_delay_min"] = _safe_int(
            patched.get("driver_departure_delay_min", 0),
            0,
        ) + max(delay_min, 0)
        changes.append(
            {
                "field": "driver_departure_delay_min",
                "before": before,
                "after": patched["driver_departure_delay_min"],
            }
        )
    elif event_type == "expand_wait":
        before = _safe_int(constraints.get("max_wait_min", 45), 45)
        constraints["max_wait_min"] = before + max(delta_min, 0)
        changes.append({"field": "max_wait_min", "before": before, "after": constraints["max_wait_min"]})
    elif event_type == "expand_detour":
        before = _safe_int(constraints.get("driver_detour_max_min", 90), 90)
        constraints["driver_detour_max_min"] = before + max(delta_min, 0)
        changes.append(
            {
                "field": "driver_detour_max_min",
                "before": before,
                "after": constraints["driver_detour_max_min"],
            }
        )
    elif event_type == "expand_passenger_travel":
        before = _safe_int(constraints.get("passenger_travel_max_min", 120), 120)
        constraints["passenger_travel_max_min"] = before + max(delta_min, 0)
        changes.append(
            {
                "field": "passenger_travel_max_min",
                "before": before,
                "after": constraints["passenger_travel_max_min"],
            }
        )
    elif event_type == "expand_search_radius":
        before = _safe_int(auto_pickup.get("radius_m", 1000), 1000)
        auto_pickup["radius_m"] = before + max(delta_min, 0)
        changes.append({"field": "auto_pickup.radius_m", "before": before, "after": auto_pickup["radius_m"]})
    elif event_type == "expand_pickup_limit":
        before = _safe_int(auto_pickup.get("limit", 20), 20)
        auto_pickup["limit"] = before + max(delta_min, 0)
        changes.append({"field": "auto_pickup.limit", "before": before, "after": auto_pickup["limit"]})

    patched["constraints"] = constraints
    patched["auto_pickup"] = auto_pickup
    patched["replan_context"] = {
        "type": event_type,
        "delay_min": delay_min,
        "delta_min": delta_min,
        "reason": str(event.get("reason", "") or "").strip(),
        "changes": changes,
    }
    return patched
