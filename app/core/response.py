from __future__ import annotations

from collections import Counter
from datetime import datetime
import re
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
    if status == "error":
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
    if status == "no_solution" or not options:
        if intent.get("constraints"):
            return (
                f"已完成从{driver_name}与{passenger_name}前往{destination_name}的规划，"
                f"但在当前约束下没有可行会合方案。系统已重试 {retry_count} 次。"
            )
        return (
            f"已完成从{driver_name}与{passenger_name}前往{destination_name}的规划，"
            f"但在当前自动候选点范围内没有找到可行会合方案。系统已重试 {retry_count} 次。"
        )

    recommended = dict(options[0])
    wait_warning = _build_wait_warning(recommended)
    departure_advice = _build_departure_advice(recommended)
    replan_summary = _build_replan_summary(intent)
    replan_delta = _build_replan_delta(intent, recommended)

    lines = [
        f"已完成结伴规划：你从{driver_name}出发，朋友从{passenger_name}出发，目的地为{destination_name}。",
        f"共找到 {len(options)} 个可行会合点（系统重试 {retry_count} 次）。",
    ]
    if wait_warning:
        lines.append(wait_warning)
    if departure_advice:
        lines.append(departure_advice)
    if replan_summary:
        title = replan_summary.get("title", "动态重规划")
        reason = str(replan_summary.get("reason", "") or "").strip()
        line = f"本次为重规划：{title}。"
        if reason:
            line += f"触发原因：{reason}。"
        lines.append(line)
    if replan_delta:
        delta_parts: List[str] = []
        if replan_delta.get("pickup_changed"):
            delta_parts.append(
                f"会合点由「{replan_delta.get('previous_pickup_point')}」调整为「{replan_delta.get('current_pickup_point')}」"
            )
        wait_delta = _safe_int(replan_delta.get("wait_delta_min", 0))
        detour_delta = _safe_int(replan_delta.get("detour_delta_min", 0))
        arrival_delta = _safe_int(replan_delta.get("arrival_delta_min", 0))
        if wait_delta:
            delta_parts.append(
                f"等待时间{'增加' if wait_delta > 0 else '减少'}了 {abs(wait_delta)} 分钟"
            )
        if detour_delta:
            delta_parts.append(
                f"司机绕路{'增加' if detour_delta > 0 else '减少'}了 {abs(detour_delta)} 分钟"
            )
        if arrival_delta:
            delta_parts.append(
                f"整体到达时间{'推后' if arrival_delta > 0 else '提前'}了 {abs(arrival_delta)} 分钟"
            )
        if delta_parts:
            lines.append("与上一次方案相比：" + "，".join(delta_parts) + "。")
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


def infer_response_status(*, error: str, result: Dict[str, Any]) -> str:
    if error:
        return "error"
    options = (result or {}).get("options", [])
    if options:
        return "ok"
    return "no_solution"


def classify_failure(*, error: str, result: Dict[str, Any]) -> str:
    if not error:
        options = (result or {}).get("options", [])
        return "constraints_too_strict" if not options else ""

    lowered = error.lower()
    if "parse_intent" in lowered:
        return "intent_parse_failed"
    if "address not found" in lowered or "geocode" in lowered:
        return "address_resolution_failed"
    if "timeout" in lowered:
        return "planner_timeout"
    if "api error" in lowered or "network" in lowered or "connection" in lowered:
        return "upstream_api_error"
    return "unknown_error"


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.max
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.max


def _dominant_constraint_reason(result: Dict[str, Any]) -> str:
    summary = _summarize_diagnostics(result)
    counts = summary.get("reason_counts", {})
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)[0][0]


def _parse_reason_prefix(reason: str) -> str:
    return str(reason).split(" ", 1)[0].strip()


def _parse_reason_gap(reason: str) -> int:
    match = re.search(r"\(([-]?\d+)\s*>\s*([-]?\d+)\)", str(reason))
    if not match:
        return 0
    actual = _safe_int(match.group(1))
    limit = _safe_int(match.group(2))
    return max(actual - limit, 0)


def _summarize_diagnostics(result: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = result.get("diagnostics", []) or []
    reason_counter: Counter[str] = Counter()
    exceed_totals: Dict[str, int] = {}
    exceed_counts: Dict[str, int] = {}

    for item in diagnostics:
        for raw_reason in item.get("reasons", []):
            prefix = _parse_reason_prefix(str(raw_reason))
            if not prefix:
                continue
            reason_counter[prefix] += 1
            gap = _parse_reason_gap(str(raw_reason))
            if gap > 0:
                exceed_totals[prefix] = exceed_totals.get(prefix, 0) + gap
                exceed_counts[prefix] = exceed_counts.get(prefix, 0) + 1

    avg_exceed_by_reason = {
        key: round(exceed_totals[key] / exceed_counts[key], 2)
        for key in exceed_totals
        if exceed_counts.get(key, 0) > 0
    }
    return {
        "filtered_candidate_count": len(diagnostics),
        "reason_counts": dict(reason_counter),
        "avg_exceed_by_reason": avg_exceed_by_reason,
    }


def _build_no_solution_suggestions(intent: Dict[str, Any], result: Dict[str, Any]) -> List[str]:
    dominant_reason = _dominant_constraint_reason(result)
    constraints = intent.get("constraints", {})
    diagnostics_summary = _summarize_diagnostics(result)
    avg_exceed = (diagnostics_summary.get("avg_exceed_by_reason", {}) or {}).get(dominant_reason)

    suggestions = []
    if dominant_reason == "passenger_travel_exceeded":
        suggestions.append(
            f"当前主要卡在朋友到会合点耗时过长，可以优先把乘客通勤上限从 {constraints.get('passenger_travel_max_min', 120)} 分钟适度放宽。"
            + (f"当前被过滤点平均超出约 {avg_exceed} 分钟。" if avg_exceed else "")
        )
    elif dominant_reason == "driver_detour_exceeded":
        suggestions.append(
            f"当前主要卡在司机绕路过多，可以优先把司机绕路上限从 {constraints.get('driver_detour_max_min', 90)} 分钟适度放宽。"
            + (f"当前被过滤点平均超出约 {avg_exceed} 分钟。" if avg_exceed else "")
        )
    elif dominant_reason == "wait_time_exceeded":
        suggestions.append(
            f"当前主要卡在双方等待时间过长，可以优先把最大等待时间从 {constraints.get('max_wait_min', 45)} 分钟适度放宽。"
            + (f"当前被过滤点平均超出约 {avg_exceed} 分钟。" if avg_exceed else "")
        )

    if constraints:
        suggestions.append(
            "如果目的地较远或跨城，建议扩大自动候选点搜索范围，或手动指定更容易会合的地铁站、商场、停车场。"
        )
    else:
        suggestions.append(
            "当前没有用户明确设置的硬约束，建议优先扩大自动候选点搜索范围，或手动指定更容易会合的地铁站、商场、停车场。"
        )
    return suggestions


def _build_relaxation_suggestions(intent: Dict[str, Any], result: Dict[str, Any]) -> List[Dict[str, Any]]:
    dominant_reason = _dominant_constraint_reason(result)
    constraints = intent.get("constraints", {})
    diagnostics_summary = _summarize_diagnostics(result)
    avg_exceed = (diagnostics_summary.get("avg_exceed_by_reason", {}) or {}).get(dominant_reason)

    suggestions: List[Dict[str, Any]] = []
    if dominant_reason == "passenger_travel_exceeded" and "passenger_travel_max_min" in constraints:
        current = _safe_int(constraints.get("passenger_travel_max_min", 0))
        suggested = current + max(15, int(avg_exceed or 0))
        suggestions.append(
            {
                "field": "passenger_travel_max_min",
                "current_value": current,
                "suggested_value": suggested,
                "reason": "多数候选点被乘客通勤时间约束过滤，优先放宽这一项更可能恢复可行方案。",
            }
        )
    elif dominant_reason == "driver_detour_exceeded" and "driver_detour_max_min" in constraints:
        current = _safe_int(constraints.get("driver_detour_max_min", 0))
        suggested = current + max(15, int(avg_exceed or 0))
        suggestions.append(
            {
                "field": "driver_detour_max_min",
                "current_value": current,
                "suggested_value": suggested,
                "reason": "多数候选点被司机绕路约束过滤，优先放宽这一项更可能恢复可行方案。",
            }
        )
    elif dominant_reason == "wait_time_exceeded" and "max_wait_min" in constraints:
        current = _safe_int(constraints.get("max_wait_min", 0))
        suggested = current + max(10, int(avg_exceed or 0))
        suggestions.append(
            {
                "field": "max_wait_min",
                "current_value": current,
                "suggested_value": suggested,
                "reason": "多数候选点被等待时间约束过滤，适度增加容忍等待时间可能更有效。",
            }
        )

    suggestions.append(
        {
            "field": "auto_pickup.radius_m",
            "current_value": _safe_int(((intent.get("auto_pickup") or {}).get("radius_m", 1000))),
            "suggested_value": _safe_int(((intent.get("auto_pickup") or {}).get("radius_m", 1000))) + 300,
            "reason": "扩大候选点搜索半径，有助于发现更适合会合的站点或商圈。",
        }
    )
    return suggestions


def _option_tradeoff_tags(option: Dict[str, Any], options: List[Dict[str, Any]]) -> List[str]:
    wait_min = _safe_int(option.get("pickup_wait_time_min"))
    detour_min = _safe_int(option.get("driver_detour_time_min"))
    arrival_time = _safe_datetime(option.get("total_arrival_time"))

    min_wait = min(_safe_int(x.get("pickup_wait_time_min")) for x in options)
    min_detour = min(_safe_int(x.get("driver_detour_time_min")) for x in options)
    earliest_arrival = min(_safe_datetime(x.get("total_arrival_time")) for x in options)

    tags: List[str] = []
    if wait_min == min_wait:
        tags.append("min_wait")
    if detour_min == min_detour:
        tags.append("min_detour")
    if arrival_time == earliest_arrival:
        tags.append("fast_arrival")
    if not tags:
        tags.append("balanced")
    return tags


def _option_recommendation_basis(option: Dict[str, Any], options: List[Dict[str, Any]]) -> str:
    tags = _option_tradeoff_tags(option, options)
    if "min_wait" in tags and "min_detour" in tags:
        return "best_wait_and_detour"
    if "fast_arrival" in tags and "min_wait" in tags:
        return "fast_arrival_with_low_wait"
    if "fast_arrival" in tags:
        return "fast_arrival"
    if "min_wait" in tags:
        return "min_wait"
    if "min_detour" in tags:
        return "min_detour"
    return "balanced"


def _preference_profile_label(profile: str) -> str:
    mapping = {
        "balanced": "均衡优先",
        "fast_arrival": "更快到达",
        "min_wait": "更少等待",
        "min_detour": "更少绕路",
    }
    return mapping.get(profile, "均衡优先")


def _recommended_reason(option: Dict[str, Any], options: List[Dict[str, Any]]) -> str:
    wait_min = _safe_int(option.get("pickup_wait_time_min"))
    detour_min = _safe_int(option.get("driver_detour_time_min"))
    arrival_time = _safe_datetime(option.get("total_arrival_time"))

    min_wait = min(_safe_int(x.get("pickup_wait_time_min")) for x in options)
    min_detour = min(_safe_int(x.get("driver_detour_time_min")) for x in options)
    earliest_arrival = min(_safe_datetime(x.get("total_arrival_time")) for x in options)

    strengths: List[str] = []
    if wait_min == min_wait:
        strengths.append("等待最少")
    if detour_min == min_detour:
        strengths.append("司机绕路最少")
    if arrival_time == earliest_arrival:
        strengths.append("整体到达最快")

    if not strengths:
        strengths.append("整体取舍最均衡")

    joined = "、".join(strengths[:2])
    return (
        f"推荐这个方案，因为它在当前候选中{joined}。"
        f"预计等待约 {wait_min} 分钟，司机绕路约 {detour_min} 分钟。"
    )


def _alternative_reason(option: Dict[str, Any], recommended: Dict[str, Any], options: List[Dict[str, Any]]) -> str:
    wait_min = _safe_int(option.get("pickup_wait_time_min"))
    detour_min = _safe_int(option.get("driver_detour_time_min"))
    arrival_time = _safe_datetime(option.get("total_arrival_time"))

    rec_wait = _safe_int(recommended.get("pickup_wait_time_min"))
    rec_detour = _safe_int(recommended.get("driver_detour_time_min"))
    rec_arrival = _safe_datetime(recommended.get("total_arrival_time"))

    min_wait = min(_safe_int(x.get("pickup_wait_time_min")) for x in options)
    min_detour = min(_safe_int(x.get("driver_detour_time_min")) for x in options)
    earliest_arrival = min(_safe_datetime(x.get("total_arrival_time")) for x in options)

    tags: List[str] = []
    if wait_min == min_wait:
        tags.append("更少等待")
    if detour_min == min_detour:
        tags.append("更少绕路")
    if arrival_time == earliest_arrival:
        tags.append("更快到达")

    if not tags:
        if wait_min < rec_wait:
            tags.append("等待更少")
        elif detour_min < rec_detour:
            tags.append("绕路更少")
        elif arrival_time < rec_arrival:
            tags.append("到达更早")
        else:
            tags.append("可作为稳妥备选")

    joined = "、".join(tags[:2])
    return (
        f"这个方案更偏向{joined}。"
        f"预计等待约 {wait_min} 分钟，司机绕路约 {detour_min} 分钟。"
    )


def _wait_experience_level(wait_min: int) -> str:
    if wait_min >= 60:
        return "high"
    if wait_min >= 30:
        return "medium"
    return "low"


def _build_wait_warning(option: Dict[str, Any]) -> str:
    wait_min = _safe_int(option.get("pickup_wait_time_min"))
    level = _wait_experience_level(wait_min)
    if level == "high":
        return f"当前推荐方案现场等待较长，预计约 {wait_min} 分钟，建议不要按双方同时出发直接执行。"
    if level == "medium":
        return f"当前推荐方案等待时间偏长，预计约 {wait_min} 分钟，建议结合出发时间一起调整。"
    return ""


def _build_departure_advice(option: Dict[str, Any]) -> str:
    driver_eta = _safe_datetime(option.get("eta_driver_to_pickup"))
    passenger_eta = _safe_datetime(option.get("eta_passenger_to_pickup"))
    if driver_eta == datetime.max or passenger_eta == datetime.max:
        return ""

    delta_min = abs(int((driver_eta - passenger_eta).total_seconds() // 60))
    if delta_min < 10:
        return ""
    if driver_eta < passenger_eta:
        return f"建议司机稍晚出发约 {delta_min} 分钟，可明显减少现场等待。"
    return f"建议朋友稍晚出发约 {delta_min} 分钟，可明显减少现场等待。"


def _build_share_text(
    *,
    driver_name: str,
    passenger_name: str,
    destination_name: str,
    recommended_option: Dict[str, Any],
) -> str:
    pickup_point = str(recommended_option.get("pickup_point", "待确认会合点"))
    wait_min = _safe_int(recommended_option.get("pickup_wait_time_min"))
    detour_min = _safe_int(recommended_option.get("driver_detour_time_min"))
    arrival_time = str(recommended_option.get("total_arrival_time", ""))
    return (
        f"结伴出行建议：我从{driver_name}出发，你从{passenger_name}出发，"
        f"我们先在「{pickup_point}」会合，再一起去{destination_name}。"
        f"当前推荐方案预计等待约{wait_min}分钟，司机绕路约{detour_min}分钟，"
        f"预计整体到达时间为{arrival_time}。"
    )


def _build_share_card(
    *,
    destination_name: str,
    recommended_option: Dict[str, Any],
) -> Dict[str, Any]:
    pickup_point = str(recommended_option.get("pickup_point", "待确认会合点"))
    wait_min = _safe_int(recommended_option.get("pickup_wait_time_min"))
    detour_min = _safe_int(recommended_option.get("driver_detour_time_min"))
    arrival_time = str(recommended_option.get("total_arrival_time", ""))
    return {
        "title": f"推荐会合点：{pickup_point}",
        "subtitle": f"会合后一起前往 {destination_name}",
        "highlights": [
            f"等待约 {wait_min} 分钟",
            f"司机绕路约 {detour_min} 分钟",
            f"整体到达时间 {arrival_time}",
        ],
        "pickup_point": pickup_point,
        "arrival_time": arrival_time,
    }


def _build_replan_summary(intent: Dict[str, Any]) -> Dict[str, Any]:
    context = dict(intent.get("replan_context", {}) or {})
    if not context:
        return {}

    type_label_map = {
        "passenger_delay": "朋友晚点",
        "driver_delay": "司机晚点",
        "expand_wait": "放宽等待时间",
        "expand_detour": "放宽司机绕路",
        "expand_passenger_travel": "放宽朋友通勤",
        "expand_search_radius": "扩大搜索半径",
        "expand_pickup_limit": "增加候选点数量",
    }
    change_texts = []
    for item in context.get("changes", []) or []:
        field = str(item.get("field", ""))
        before = item.get("before")
        after = item.get("after")
        if field:
            change_texts.append(f"{field}: {before} -> {after}")

    return {
        "title": type_label_map.get(str(context.get("type", "")), "动态重规划"),
        "reason": str(context.get("reason", "") or "").strip(),
        "changes": change_texts,
    }


def _build_replan_delta(intent: Dict[str, Any], recommended_option: Dict[str, Any]) -> Dict[str, Any]:
    previous = dict(intent.get("previous_recommendation", {}) or {})
    if not previous:
        return {}

    current_pickup = str(recommended_option.get("pickup_point", "") or "")
    previous_pickup = str(previous.get("pickup_point", "") or "")
    current_wait = _safe_int(recommended_option.get("pickup_wait_time_min"))
    previous_wait = _safe_int(previous.get("pickup_wait_time_min"))
    current_detour = _safe_int(recommended_option.get("driver_detour_time_min"))
    previous_detour = _safe_int(previous.get("driver_detour_time_min"))

    current_arrival = _safe_datetime(recommended_option.get("total_arrival_time"))
    previous_arrival = _safe_datetime(previous.get("total_arrival_time"))

    arrival_delta_min = 0
    if current_arrival != datetime.max and previous_arrival != datetime.max:
        arrival_delta_min = int((current_arrival - previous_arrival).total_seconds() // 60)

    return {
        "pickup_changed": current_pickup != previous_pickup,
        "previous_pickup_point": previous_pickup,
        "current_pickup_point": current_pickup,
        "wait_delta_min": current_wait - previous_wait,
        "detour_delta_min": current_detour - previous_detour,
        "arrival_delta_min": arrival_delta_min,
    }


def build_response_payload(
    *,
    intent: Dict[str, Any],
    result: Dict[str, Any],
    retry_count: int,
    error: str = "",
) -> Dict[str, Any]:
    status = infer_response_status(error=error, result=result)
    resolved = result.get("resolved_locations", {})
    options = result.get("options", [])

    driver_name = ((resolved.get("driver_origin") or {}).get("name")) or intent.get(
        "driver_origin_address", "司机出发地"
    )
    passenger_name = ((resolved.get("passenger_origin") or {}).get("name")) or intent.get(
        "passenger_origin_address", "朋友出发地"
    )
    destination_name = ((resolved.get("destination") or {}).get("name")) or intent.get(
        "destination_address", "目的地"
    )

    payload: Dict[str, Any] = {
        "status": status,
        "summary": {
            "driver_origin_name": driver_name,
            "passenger_origin_name": passenger_name,
            "destination_name": destination_name,
            "candidate_count": int(result.get("pickup_candidates_count", 0) or 0),
            "feasible_option_count": len(options),
            "retry_count": retry_count,
            "preference_profile": str(intent.get("preference_profile", "balanced")),
            "preference_label": _preference_profile_label(
                str(intent.get("preference_profile", "balanced"))
            ),
            "is_replan": bool(intent.get("replan_context")),
            "replan_context": dict(intent.get("replan_context", {}) or {}),
            "replan_summary": _build_replan_summary(intent),
            "replan_delta": {},
            "experience_warning": "",
            "departure_advice": "",
        },
        "recommended_option": None,
        "alternative_options": [],
        "suggestions": [],
        "relaxation_suggestions": [],
        "primary_bottleneck": None,
        "constraint_diagnostics": {},
        "share_text": "",
        "share_card": {},
        "error": None,
    }

    if status == "error":
        failure_category = classify_failure(error=error, result=result)
        payload["error"] = {
            "code": failure_category or "unknown_error",
            "message": "本次规划失败，请检查地址信息或稍后重试。",
        }
        return payload

    if status == "no_solution":
        constraints = intent.get("constraints", {})
        dominant_reason = _dominant_constraint_reason(result)
        dominant_reason_text = f"主要瓶颈是 {dominant_reason}。" if dominant_reason else ""
        diagnostics_summary = _summarize_diagnostics(result)
        payload["suggestions"] = _build_no_solution_suggestions(intent, result)
        payload["relaxation_suggestions"] = _build_relaxation_suggestions(intent, result)
        payload["primary_bottleneck"] = dominant_reason or None
        payload["constraint_diagnostics"] = diagnostics_summary
        if constraints:
            parts = []
            if "passenger_travel_max_min" in constraints:
                parts.append(f"乘客不超过 {constraints.get('passenger_travel_max_min')} 分钟")
            if "driver_detour_max_min" in constraints:
                parts.append(f"司机绕路不超过 {constraints.get('driver_detour_max_min')} 分钟")
            if "max_wait_min" in constraints:
                parts.append(f"等待不超过 {constraints.get('max_wait_min')} 分钟")
            no_solution_message = (
                "当前约束下未找到可行会合方案。"
                + dominant_reason_text
                + (
                    f"共有 {diagnostics_summary.get('filtered_candidate_count', 0)} 个候选点被约束过滤。"
                    if diagnostics_summary.get("filtered_candidate_count", 0)
                    else ""
                )
            )
            if parts:
                no_solution_message += "当前限制为：" + "、".join(parts) + "。"
        else:
            no_solution_message = (
                "当前自动候选点范围内未找到可行会合方案。"
                + dominant_reason_text
                + (
                    f"共有 {diagnostics_summary.get('filtered_candidate_count', 0)} 个候选点被约束过滤。"
                    if diagnostics_summary.get("filtered_candidate_count", 0)
                    else ""
                )
            )
        payload["error"] = {
            "code": classify_failure(error=error, result=result) or "no_feasible_option",
            "message": no_solution_message,
        }
        return payload

    recommended = dict(options[0])
    recommended["tradeoff_tags"] = _option_tradeoff_tags(recommended, options)
    recommended["recommendation_basis"] = _option_recommendation_basis(recommended, options)
    recommended["reason"] = _recommended_reason(recommended, options)
    recommended["wait_warning"] = _build_wait_warning(recommended)
    recommended["departure_advice"] = _build_departure_advice(recommended)
    recommended["preference_alignment"] = (
        recommended["recommendation_basis"] == payload["summary"]["preference_profile"]
        or payload["summary"]["preference_profile"] == "balanced"
    )
    payload["recommended_option"] = recommended
    payload["summary"]["replan_delta"] = _build_replan_delta(intent, recommended)
    payload["summary"]["experience_warning"] = recommended["wait_warning"]
    payload["summary"]["departure_advice"] = recommended["departure_advice"]
    payload["share_text"] = _build_share_text(
        driver_name=driver_name,
        passenger_name=passenger_name,
        destination_name=destination_name,
        recommended_option=recommended,
    )
    payload["share_card"] = _build_share_card(
        destination_name=destination_name,
        recommended_option=recommended,
    )

    alternatives: List[Dict[str, Any]] = []
    for option in options[1:]:
        alt = dict(option)
        alt["tradeoff_tags"] = _option_tradeoff_tags(alt, options)
        alt["recommendation_basis"] = _option_recommendation_basis(alt, options)
        alt["reason"] = _alternative_reason(alt, recommended, options)
        alt["wait_warning"] = _build_wait_warning(alt)
        alt["departure_advice"] = _build_departure_advice(alt)
        alt["preference_alignment"] = (
            alt["recommendation_basis"] == payload["summary"]["preference_profile"]
            or payload["summary"]["preference_profile"] == "balanced"
        )
        alternatives.append(alt)
    payload["alternative_options"] = alternatives
    return payload
