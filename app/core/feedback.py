from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Tuple


DEFAULT_NEXT_ACTIONS = ["share_plan", "replan_if_delay", "open_navigation"]


def normalize_feedback_event(event: Dict[str, Any]) -> Dict[str, Any]:
    feedback_type = str(event.get("type", "") or "").strip()
    target_option = str(event.get("target_option", "") or "").strip()
    reason = str(event.get("reason", "") or "").strip()
    raw_signals = event.get("signals", []) or []

    signals: List[Dict[str, str]] = []
    for signal in raw_signals:
        if not isinstance(signal, dict):
            continue
        kind = str(signal.get("kind", "") or "").strip()
        value = str(signal.get("value", "") or "").strip()
        strength = str(signal.get("strength", "soft") or "soft").strip()
        if kind and value:
            signals.append({"kind": kind, "value": value, "strength": strength})

    if not signals and reason:
        signals = _infer_signals_from_reason(reason)
        if not feedback_type and signals:
            inferred = _infer_feedback_type_from_signals(signals)
            if inferred:
                feedback_type = inferred

    if not feedback_type:
        feedback_type = _infer_feedback_type_from_signals(signals)

    return {
        "type": feedback_type,
        "target_option": target_option,
        "signals": signals,
        "reason": reason,
    }


def should_use_llm_feedback_parser(event: Dict[str, Any]) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("signals"):
        return False
    reason = str(event.get("reason", "") or "").strip()
    return bool(reason)


def feedback_parser_system_prompt() -> str:
    return """
You are a feedback parser for a rendezvous planning agent.
Convert the user's follow-up feedback into strict JSON only.

Output schema:
{
  "type": "preference_update | plan_feedback | option_selection",
  "target_option": "string",
  "signals": [
    {
      "kind": "wait | detour | passenger_experience | pickup_point | selection",
      "value": "min_wait | can_depart_later | max_wait_min:<int> | avoid_long_detour | can_drive_more | driver_detour_max_min:<int> | low_transfer | low_walking | avoid_too_much_hassle | parking_unfriendly | prefer_metro | avoid_mall | low_landmark_confidence | exclude_pickup_point:<name> | select_option",
      "strength": "hard | soft"
    }
  ],
  "reason": "string"
}

Rules:
- Use only the allowed enum values above.
- If the user is choosing an option, set type=option_selection.
- If the user mentions a pickup point name from context, put it into target_option.
- If the feedback is about waiting, detour, transfers, walking, parking, metro preference, or avoiding malls, map it into the nearest allowed signal.
- Keep reason as a short paraphrase or the original user text.
- Return JSON only.
""".strip()


def feedback_parser_user_prompt(reason: str, previous_response_payload: Dict[str, Any] | None = None) -> str:
    previous_response_payload = previous_response_payload or {}
    recommended = previous_response_payload.get("recommended_option") or {}
    alternatives = previous_response_payload.get("alternative_options") or []
    option_names = []
    if recommended.get("pickup_point"):
        option_names.append(f"recommended:{recommended.get('pickup_point')}")
    for idx, item in enumerate(alternatives, start=1):
        pickup_point = item.get("pickup_point")
        if pickup_point:
            option_names.append(f"alternative_{idx}:{pickup_point}")
    return (
        f"user_feedback={reason}\n"
        f"available_options={option_names}\n"
        "Return strict JSON only."
    )


def validate_feedback_event(event: Dict[str, Any]) -> None:
    normalized = normalize_feedback_event(event)
    feedback_type = normalized.get("type", "")
    if feedback_type not in {"preference_update", "plan_feedback", "option_selection"}:
        raise ValueError("无法理解这条反馈，请用户重述为等待/绕路/换乘/点位/选择之一。")
    if feedback_type == "option_selection" and not (
        normalized.get("target_option") or _has_selection_signal(normalized.get("signals", []))
    ):
        raise ValueError("方案选择反馈必须指定 target_option。")
    if not normalized.get("signals") and feedback_type != "option_selection":
        raise ValueError("无法理解这条反馈，请用户重述为等待/绕路/换乘/点位/选择之一。")


def resolve_option_reference(previous_response_payload: Dict[str, Any], target_option: str) -> Dict[str, Any]:
    target = str(target_option or "").strip()
    if not target:
        return {}

    recommended = dict(previous_response_payload.get("recommended_option") or {})
    alternatives = [dict(item) for item in (previous_response_payload.get("alternative_options") or [])]
    lower_target = target.lower()

    if lower_target == "recommended":
        return recommended
    if lower_target.startswith("alternative_"):
        try:
            index = int(lower_target.split("_", 1)[1]) - 1
        except (IndexError, ValueError):
            index = -1
        if 0 <= index < len(alternatives):
            return alternatives[index]

    all_options = [recommended, *alternatives]
    for option in all_options:
        if str(option.get("pickup_point", "")).strip() == target:
            return option
    return {}


def apply_feedback_event(
    intent: Dict[str, Any],
    feedback_event: Dict[str, Any],
    *,
    previous_response_payload: Dict[str, Any] | None = None,
    selected_option_ref: str = "",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    normalized = normalize_feedback_event(feedback_event)
    validate_feedback_event(normalized)

    updated = copy.deepcopy(intent)
    updated.setdefault("constraints", {})
    updated.setdefault("preference_overrides", [])
    updated.setdefault("feedback_state", {"events": []})
    updated.setdefault("prefer_pickup_tags", [])
    updated.setdefault("avoid_pickup_tags", [])
    updated.setdefault("exclude_pickup_points", [])
    updated.setdefault("max_departure_shift_min", 60)
    feedback_state = dict(updated.get("feedback_state", {}) or {})
    feedback_state.setdefault("events", [])
    updated["feedback_state"] = feedback_state

    control: Dict[str, Any] = {
        "feedback_event": normalized,
        "selection_only": normalized.get("type") == "option_selection",
        "selected_option": {},
    }

    selection_target = selected_option_ref or normalized.get("target_option", "")
    if previous_response_payload and not selection_target:
        selection_target = _infer_target_option_from_reason(
            previous_response_payload,
            normalized.get("reason", ""),
        )
    if previous_response_payload and selection_target:
        control["selected_option"] = resolve_option_reference(previous_response_payload, selection_target)

    if normalized.get("type") == "option_selection":
        updated["selected_option_ref"] = selection_target or "recommended"
        feedback_state["events"].append(
            {
                "type": "option_selection",
                "target_option": updated["selected_option_ref"],
                "reason": normalized.get("reason", ""),
            }
        )
        return updated, control

    feedback_state["events"].append(normalized)
    feedback_state["latest_type"] = normalized.get("type")
    feedback_state["latest_reason"] = normalized.get("reason", "")

    constraints = dict(updated.get("constraints", {}) or {})
    overrides = list(updated.get("preference_overrides", []) or [])
    prefer_tags = list(updated.get("prefer_pickup_tags", []) or [])
    avoid_tags = list(updated.get("avoid_pickup_tags", []) or [])
    excluded_points = list(updated.get("exclude_pickup_points", []) or [])

    for signal in normalized.get("signals", []):
        kind = str(signal.get("kind", "") or "")
        value = str(signal.get("value", "") or "")

        if kind == "wait":
            if value == "min_wait":
                updated["preference_profile"] = "min_wait"
            elif value == "can_depart_later":
                updated["max_departure_shift_min"] = max(
                    int(updated.get("max_departure_shift_min", 60) or 60),
                    90,
                )
            elif value.startswith("max_wait_min:"):
                constraints["max_wait_min"] = _extract_numeric_value(value)
        elif kind == "detour":
            if value == "avoid_long_detour":
                updated["preference_profile"] = "min_detour"
            elif value == "can_drive_more":
                feedback_state["detour_penalty_relaxed"] = True
            elif value.startswith("driver_detour_max_min:"):
                constraints["driver_detour_max_min"] = _extract_numeric_value(value)
        elif kind == "passenger_experience":
            if value == "low_transfer":
                overrides = _append_unique(overrides, "low_transfer")
            elif value == "low_walking":
                overrides = _append_unique(overrides, "low_walking_signal")
            elif value == "avoid_too_much_hassle":
                overrides = _append_unique(overrides, "low_transfer")
                overrides = _append_unique(overrides, "balanced_fairness")
        elif kind == "pickup_point":
            if value == "prefer_metro":
                prefer_tags = _append_unique(prefer_tags, "metro")
            elif value == "avoid_mall":
                avoid_tags = _append_unique(avoid_tags, "mall")
            elif value == "parking_unfriendly":
                avoid_tags = _append_unique(avoid_tags, "parking_unfriendly")
            elif value == "low_landmark_confidence":
                avoid_tags = _append_unique(avoid_tags, "low_landmark_confidence")
            elif value.startswith("exclude_pickup_point:"):
                excluded_points = _append_unique(
                    excluded_points,
                    value.split(":", 1)[1].strip(),
                )

    updated["constraints"] = {k: v for k, v in constraints.items() if int(v or 0) > 0}
    updated["preference_overrides"] = overrides
    updated["prefer_pickup_tags"] = prefer_tags
    updated["avoid_pickup_tags"] = avoid_tags
    updated["exclude_pickup_points"] = excluded_points
    return updated, control


def build_selection_payload(
    *,
    previous_response_payload: Dict[str, Any],
    selected_option: Dict[str, Any],
    selected_option_ref: str,
    feedback_event: Dict[str, Any],
) -> Dict[str, Any]:
    if not selected_option:
        raise ValueError("无法定位要选择的方案。")

    payload = copy.deepcopy(previous_response_payload)
    summary = dict(payload.get("summary", {}) or {})
    summary["selection_state"] = "selected"
    summary["selected_option_ref"] = selected_option_ref
    payload["summary"] = summary
    payload["status"] = "selected"
    payload["selected_option"] = selected_option
    payload["selection_summary"] = {
        "selected_option_ref": selected_option_ref,
        "pickup_point": selected_option.get("pickup_point"),
        "reason": feedback_event.get("reason", "") or "用户确认采用该方案。",
    }
    payload["execution_ready_share_text"] = (
        f"我们确认按「{selected_option.get('pickup_point', '待确认会合点')}」会合。"
        f"司机预计 {selected_option.get('eta_driver_to_pickup')} 到达，"
        f"朋友预计 {selected_option.get('eta_passenger_to_pickup')} 到达，"
        f"现场等待约 {selected_option.get('optimized_wait_time_min', selected_option.get('pickup_wait_time_min', 0))} 分钟。"
    )
    payload["execution_ready_share_card"] = {
        "title": f"已确认会合点：{selected_option.get('pickup_point', '待确认会合点')}",
        "subtitle": "双方可按该方案出发",
        "highlights": [
            f"优化后等待约 {selected_option.get('optimized_wait_time_min', selected_option.get('pickup_wait_time_min', 0))} 分钟",
            f"司机绕路约 {selected_option.get('driver_detour_time_min', 0)} 分钟",
            f"预计到达目的地时间 {selected_option.get('total_arrival_time', '')}",
        ],
    }
    payload["next_actions"] = list(DEFAULT_NEXT_ACTIONS)
    return payload


def _append_unique(items: List[str], value: str) -> List[str]:
    if value and value not in items:
        items.append(value)
    return items


def _has_selection_signal(signals: List[Dict[str, str]]) -> bool:
    return any(str(item.get("value", "") or "") == "select_option" for item in signals)


def _infer_feedback_type_from_signals(signals: List[Dict[str, str]]) -> str:
    for signal in signals:
        if str(signal.get("kind", "")) == "selection":
            return "option_selection"
    if signals:
        return "plan_feedback"
    return ""


def _extract_numeric_value(text: str) -> int:
    match = re.search(r"(\d+)", str(text))
    if not match:
        return 0
    return int(match.group(1))


def _infer_signals_from_reason(reason: str) -> List[Dict[str, str]]:
    text = str(reason or "").strip()
    signals: List[Dict[str, str]] = []

    if re.search(r"选第?\s*\d+\s*个|就用.+(方案|点)|按这个方案|这个方案可以", text):
        signals.append({"kind": "selection", "value": "select_option", "strength": "hard"})
        return signals

    if "等太久" in text or "少等" in text:
        signals.append({"kind": "wait", "value": "min_wait", "strength": "soft"})
    if "晚点出发" in text or "晚出发" in text:
        signals.append({"kind": "wait", "value": "can_depart_later", "strength": "soft"})
    if "不想绕太远" in text or "少绕路" in text:
        signals.append({"kind": "detour", "value": "avoid_long_detour", "strength": "soft"})
    if "多开一点" in text:
        signals.append({"kind": "detour", "value": "can_drive_more", "strength": "soft"})
    if any(phrase in text for phrase in ("少换乘", "不要换乘", "尽量不要换乘", "别换乘", "尽量别换乘")):
        signals.append({"kind": "passenger_experience", "value": "low_transfer", "strength": "soft"})
    if any(phrase in text for phrase in ("少步行", "不要走太多路", "尽量少走路", "别走太多路")):
        signals.append({"kind": "passenger_experience", "value": "low_walking", "strength": "soft"})
    if "别太折腾" in text:
        signals.append(
            {"kind": "passenger_experience", "value": "avoid_too_much_hassle", "strength": "soft"}
        )
    if "不好停车" in text:
        signals.append({"kind": "pickup_point", "value": "parking_unfriendly", "strength": "soft"})
    if "优先地铁站" in text:
        signals.append({"kind": "pickup_point", "value": "prefer_metro", "strength": "soft"})
    if "不要商圈" in text:
        signals.append({"kind": "pickup_point", "value": "avoid_mall", "strength": "soft"})
    if "不好认" in text:
        signals.append(
            {"kind": "pickup_point", "value": "low_landmark_confidence", "strength": "soft"}
        )

    wait_limit = re.search(r"等待不超过\s*(\d+)\s*分钟", text)
    if wait_limit:
        signals.append(
            {
                "kind": "wait",
                "value": f"max_wait_min:{wait_limit.group(1)}",
                "strength": "hard",
            }
        )
    detour_limit = re.search(r"最多绕路\s*(\d+)\s*分钟", text)
    if detour_limit:
        signals.append(
            {
                "kind": "detour",
                "value": f"driver_detour_max_min:{detour_limit.group(1)}",
                "strength": "hard",
            }
        )
    return signals


def _infer_target_option_from_reason(
    previous_response_payload: Dict[str, Any],
    reason: str,
) -> str:
    text = str(reason or "").strip()
    if not text:
        return ""
    recommended = dict(previous_response_payload.get("recommended_option") or {})
    alternatives = [dict(item) for item in (previous_response_payload.get("alternative_options") or [])]
    all_options = [recommended, *alternatives]
    for index, option in enumerate(all_options):
        pickup_point = str(option.get("pickup_point", "") or "").strip()
        if pickup_point and pickup_point in text:
            if index == 0:
                return "recommended"
            return f"alternative_{index}"
    return ""
