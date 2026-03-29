from __future__ import annotations

from typing import Any, Dict, List


def compact_plan_result(result: Dict[str, Any]) -> Dict[str, Any]:
    resolved = result.get("resolved_locations", {})
    compact_resolved: Dict[str, Any] = {}
    for key in ("driver_origin", "passenger_origin", "destination"):
        item = resolved.get(key, {})
        compact_resolved[key] = {
            "name": item.get("name"),
            "lat": item.get("lat"),
            "lon": item.get("lon"),
        }

    compact_options: List[Dict[str, Any]] = []
    for option in result.get("options", []):
        compact_options.append(
            {
                "pickup_point": option.get("pickup_point"),
                "score": option.get("score"),
                "eta_driver_to_pickup": option.get("eta_driver_to_pickup"),
                "eta_passenger_to_pickup": option.get("eta_passenger_to_pickup"),
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
    if "diagnostics" in result:
        compact["diagnostics"] = result["diagnostics"]
    return compact


def build_natural_language_output(
    *,
    status: str,
    intent: Dict[str, Any],
    result: Dict[str, Any],
    retry_count: int,
    error: str = "",
) -> str:
    if status != "ok":
        return f"本次规划失败：{error}"

    resolved = result.get("resolved_locations", {})
    driver_name = ((resolved.get("driver_origin") or {}).get("name")) or intent.get(
        "driver_origin_address", "司机出发地"
    )
    passenger_name = ((resolved.get("passenger_origin") or {}).get("name")) or intent.get(
        "passenger_origin_address", "朋友出发地"
    )
    destination_name = ((resolved.get("destination") or {}).get("name")) or intent.get(
        "destination_address", "目的地"
    )

    options = result.get("options", [])
    if not options:
        return (
            f"已完成从{driver_name}与{passenger_name}前往{destination_name}的规划，"
            f"但在当前约束下没有可行会合方案。系统已重试 {retry_count} 次。"
        )

    lines = [
        f"已完成结伴规划：你从{driver_name}出发，朋友从{passenger_name}出发，目的地为{destination_name}。",
        f"共找到 {len(options)} 个可行会合点（系统重试 {retry_count} 次）。",
    ]
    for idx, option in enumerate(options, start=1):
        lines.append(
            f"{idx}. 会合点「{option.get('pickup_point')}」，"
            f"你预计 {option.get('eta_driver_to_pickup')} 到达，"
            f"朋友预计 {option.get('eta_passenger_to_pickup')} 到达，"
            f"等待约 {option.get('pickup_wait_time_min')} 分钟，"
            f"司机绕路约 {option.get('driver_detour_time_min')} 分钟，"
            f"预计到达目的地时间 {option.get('total_arrival_time')}。"
        )
    return "\n".join(lines)

