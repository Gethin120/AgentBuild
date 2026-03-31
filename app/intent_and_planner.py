from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from app.engine import (
    PlanningConstraints,
    RendezvousPlanner,
    ScoringWeights,
    build_provider,
    demo_request,
    resolve_request_from_addresses,
    resolve_request_with_auto_pickups,
)


def _build_lmstudio_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = (
        os.getenv("LM_STUDIO_API_KEY", "").strip()
        or os.getenv("LMSTUDIO_API_KEY", "").strip()
    )
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _extract_json_snippet_from_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""

    fenced = re.findall(r"```json\s*(\{.*?\})\s*```", raw, flags=re.S)
    if fenced:
        return fenced[0].strip()

    # Find the first balanced JSON object.
    start = raw.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(raw)):
        ch = raw[idx]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : idx + 1].strip()
    return ""


def call_lmstudio_chat(
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    timeout_sec: int = 180,
    max_retries: int = 2,
    enable_thinking: bool = False,
) -> str:
    endpoint = urljoin(base_url.rstrip("/") + "/", "chat/completions")
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    # Best-effort switch for Qwen-like thinking mode in OpenAI-compatible servers.
    payload["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}
    data = json.dumps(payload).encode("utf-8")
    req = Request(endpoint, data=data, headers=_build_lmstudio_headers())
    attempt = 0
    while True:
        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
            response = json.loads(raw)
            message = ((response.get("choices") or [{}])[0]).get("message") or {}
            content = message.get("content", "")
            if isinstance(content, list):
                content = "".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
            content = str(content or "").strip()
            if content:
                return content

            # Some local models place output in reasoning_content when thinking is enabled.
            reasoning = str(message.get("reasoning_content", "") or "").strip()
            if reasoning:
                snippet = _extract_json_snippet_from_text(reasoning)
                if snippet:
                    return snippet
                return reasoning

            raise ValueError("LM response has empty content and reasoning_content.")
        except Exception:
            if attempt >= max_retries:
                raise
            attempt += 1


def extract_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.findall(r"```json\s*(\{.*?\})\s*```", text, flags=re.S)
    if fenced:
        return json.loads(fenced[0])

    brace = re.search(r"(\{.*\})", text, flags=re.S)
    if brace:
        return json.loads(brace.group(1))

    raise ValueError("Model did not return valid JSON.")


def intent_prompt_template(now_iso: str) -> str:
    return f"""
You are an intent parser for a ride-sharing rendezvous planner.
Current time: {now_iso}

Return ONLY a JSON object with this schema:
{{
  "driver_origin_address": "string",
  "passenger_origin_address": "string",
  "destination_address": "string",
  "geocode_city": "string (optional, empty if uncertain)",
  "candidate_mode": "auto" or "manual",
  "pickup_addresses": ["string"],
  "constraints": {{
    "passenger_travel_max_min": integer,
    "driver_detour_max_min": integer,
    "max_wait_min": integer
  }},
  "weights": {{
    "arrival_weight": number,
    "wait_weight": number,
    "detour_weight": number
  }},
  "top_n": integer,
  "auto_pickup": {{
    "limit": integer,
    "radius_m": integer,
    "sample_km": number,
    "keywords": "string"
  }}
}}

Rules:
- Default candidate_mode to "auto" if user did not provide manual pickup addresses.
- If candidate_mode is "manual", pickup_addresses must not be empty.
- Do NOT default geocode_city to any city (for example Shanghai).
- Set geocode_city only when user explicitly specifies a single clear city for this trip.
- If city is ambiguous or cross-city, set geocode_city to "".
- Weights should sum to 1.0.
- If user did not explicitly provide a hard limit, keep the corresponding constraints field absent or empty.
- Do NOT invent default hard constraints for passenger travel, driver detour, or waiting time.
- Use practical defaults only for non-constraint fields:
  top_n=3, auto limit=20, radius_m=1000, sample_km=5
- Output JSON only.
""".strip()


def validate_intent(intent: Dict[str, Any]) -> None:
    required = [
        "driver_origin_address",
        "passenger_origin_address",
        "destination_address",
        "candidate_mode",
        "constraints",
        "weights",
        "top_n",
        "auto_pickup",
    ]
    missing = [k for k in required if k not in intent]
    if missing:
        raise ValueError(f"Intent missing fields: {missing}")

    if intent["candidate_mode"] not in {"auto", "manual"}:
        raise ValueError("candidate_mode must be 'auto' or 'manual'")

    if intent["candidate_mode"] == "manual" and not intent.get("pickup_addresses"):
        raise ValueError("candidate_mode=manual requires non-empty pickup_addresses")


def _normalize_constraints(constraints: Dict[str, Any]) -> Dict[str, int]:
    normalized: Dict[str, int] = {}
    for key in ("passenger_travel_max_min", "driver_detour_max_min", "max_wait_min"):
        value = constraints.get(key)
        if value in (None, "", 0):
            continue
        numeric = int(value)
        if numeric > 0:
            normalized[key] = numeric
    return normalized


def _normalize_city_token(city: str) -> str:
    value = city.strip()
    if value.endswith("市"):
        value = value[:-1]
    return value.lower()


def _extract_city_from_address(address: str) -> str:
    text = (address or "").strip()
    # Examples matched: 上海市, 杭州市, 北京市
    match = re.search(r"([^\s,，]{2,12}?市)", text)
    if not match:
        return ""
    return _normalize_city_token(match.group(1))


def _city_hint_matches_addresses(city_hint: str, addresses: List[str]) -> bool:
    hint = _normalize_city_token(city_hint)
    if not hint:
        return True
    lowered_addrs = [a.lower() for a in addresses if a]
    return any(hint in addr for addr in lowered_addrs)


def _normalize_weights(weights: Dict[str, Any]) -> Dict[str, float]:
    arrival = max(float(weights.get("arrival_weight", 0.55) or 0.0), 0.0)
    wait = max(float(weights.get("wait_weight", 0.25) or 0.0), 0.0)
    detour = max(float(weights.get("detour_weight", 0.20) or 0.0), 0.0)
    total = arrival + wait + detour
    if total <= 0:
        return {
            "arrival_weight": 0.55,
            "wait_weight": 0.25,
            "detour_weight": 0.20,
        }
    return {
        "arrival_weight": round(arrival / total, 4),
        "wait_weight": round(wait / total, 4),
        "detour_weight": round(detour / total, 4),
    }


def _derive_preference_profile(weights: Dict[str, float]) -> str:
    ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    top_key, top_value = ranked[0]
    second_value = ranked[1][1]

    if top_value - second_value < 0.08:
        return "balanced"
    if top_key == "arrival_weight":
        return "fast_arrival"
    if top_key == "wait_weight":
        return "min_wait"
    if top_key == "detour_weight":
        return "min_detour"
    return "balanced"


def _extract_numeric_constraint(user_request: str, patterns: List[str]) -> int | None:
    text = str(user_request or "")
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                continue
    return None


def apply_request_constraint_overrides(intent: Dict[str, Any], user_request: str) -> Dict[str, Any]:
    constraints = dict(intent.get("constraints", {}) or {})
    explicit_passenger_max = _extract_numeric_constraint(
        user_request,
        [
            r"(?:朋友|乘客)[^，。,；;\n]{0,20}?(?:公交地铁|公交|地铁|通勤)[^0-9]{0,8}?不超过\s*(\d+)\s*分钟",
            r"(?:朋友|乘客)[^，。,；;\n]{0,20}?不超过\s*(\d+)\s*分钟",
        ],
    )
    explicit_driver_detour_max = _extract_numeric_constraint(
        user_request,
        [
            r"我[^，。,；;\n]{0,20}?最多绕路\s*(\d+)\s*分钟",
            r"司机[^，。,；;\n]{0,20}?最多绕路\s*(\d+)\s*分钟",
        ],
    )
    explicit_wait_max = _extract_numeric_constraint(
        user_request,
        [
            r"(?:最多等待|最大等待|等待不超过)\s*(\d+)\s*分钟",
        ],
    )

    if explicit_passenger_max is not None:
        constraints["passenger_travel_max_min"] = explicit_passenger_max
    if explicit_driver_detour_max is not None:
        constraints["driver_detour_max_min"] = explicit_driver_detour_max
    if explicit_wait_max is not None:
        constraints["max_wait_min"] = explicit_wait_max

    if constraints:
        intent["constraints"] = constraints
    return intent


def sanitize_intent(intent: Dict[str, Any]) -> Dict[str, Any]:
    intent["constraints"] = _normalize_constraints(intent.get("constraints", {}) or {})
    auto = intent.get("auto_pickup", {})
    keywords = str(auto.get("keywords", "")).strip()
    if not keywords:
        auto["keywords"] = "地铁站|公交站|停车场|商场"
    intent["auto_pickup"] = auto

    addresses = [
        str(intent.get("driver_origin_address", "")).strip(),
        str(intent.get("passenger_origin_address", "")).strip(),
        str(intent.get("destination_address", "")).strip(),
    ]
    city_hint = str(intent.get("geocode_city", "") or "").strip()

    # If LLM gives a city hint that clearly mismatches all addresses, drop it.
    if city_hint and not _city_hint_matches_addresses(city_hint, addresses):
        city_hint = ""

    # If city hint is empty but all core addresses contain the same city token, infer it.
    if not city_hint:
        inferred = [_extract_city_from_address(a) for a in addresses if a]
        if len(inferred) == 3 and inferred[0] and inferred[0] == inferred[1] == inferred[2]:
            city_hint = inferred[0]

    normalized_weights = _normalize_weights(intent.get("weights", {}))
    intent["weights"] = normalized_weights
    intent["preference_profile"] = str(
        intent.get("preference_profile") or _derive_preference_profile(normalized_weights)
    )
    overrides = []
    for item in (intent.get("preference_overrides", []) or []):
        value = str(item or "").strip()
        if value and value not in overrides:
            overrides.append(value)
    intent["preference_overrides"] = overrides
    intent["feedback_state"] = dict(intent.get("feedback_state", {}) or {})
    intent["prefer_pickup_tags"] = [
        str(item).strip()
        for item in (intent.get("prefer_pickup_tags", []) or [])
        if str(item).strip()
    ]
    intent["avoid_pickup_tags"] = [
        str(item).strip()
        for item in (intent.get("avoid_pickup_tags", []) or [])
        if str(item).strip()
    ]
    intent["exclude_pickup_points"] = [
        str(item).strip()
        for item in (intent.get("exclude_pickup_points", []) or [])
        if str(item).strip()
    ]
    intent["max_departure_shift_min"] = max(
        int(intent.get("max_departure_shift_min", 60) or 60),
        0,
    )
    intent["driver_departure_delay_min"] = max(
        int(intent.get("driver_departure_delay_min", 0) or 0),
        0,
    )
    intent["passenger_departure_delay_min"] = max(
        int(intent.get("passenger_departure_delay_min", 0) or 0),
        0,
    )
    intent["geocode_city"] = city_hint
    return intent


def run_plan(intent: Dict[str, Any], amap_key: str, show_diagnostics: bool) -> Dict[str, Any]:
    request = demo_request()
    now = datetime.now()

    constraints = intent.get("constraints", {})
    weights = intent.get("weights", {})

    request = replace(
        request,
        departure_time=now + timedelta(minutes=int(intent.get("driver_departure_delay_min", 0) or 0)),
        passenger_departure_time=(
            now + timedelta(minutes=int(intent.get("passenger_departure_delay_min", 0) or 0))
        ),
        constraints=PlanningConstraints(
            passenger_travel_max_min=(
                int(constraints["passenger_travel_max_min"])
                if constraints.get("passenger_travel_max_min") is not None
                else None
            ),
            driver_detour_max_min=(
                int(constraints["driver_detour_max_min"])
                if constraints.get("driver_detour_max_min") is not None
                else None
            ),
            max_wait_min=(
                int(constraints["max_wait_min"])
                if constraints.get("max_wait_min") is not None
                else None
            ),
        ),
        weights=ScoringWeights(
            arrival_weight=float(weights.get("arrival_weight", 0.55)),
            wait_weight=float(weights.get("wait_weight", 0.25)),
            detour_weight=float(weights.get("detour_weight", 0.20)),
        ),
        top_n=int(intent.get("top_n", 3)),
        preference_profile=str(intent.get("preference_profile", "balanced")),
        preference_overrides=tuple(intent.get("preference_overrides", []) or []),
        max_departure_shift_min=int(intent.get("max_departure_shift_min", 60) or 60),
        prefer_pickup_tags=tuple(intent.get("prefer_pickup_tags", []) or []),
        avoid_pickup_tags=tuple(intent.get("avoid_pickup_tags", []) or []),
        exclude_pickup_points=tuple(intent.get("exclude_pickup_points", []) or []),
    )

    mode = intent["candidate_mode"]
    geocode_city = intent.get("geocode_city")
    driver_origin_address = intent["driver_origin_address"]
    passenger_origin_address = intent["passenger_origin_address"]
    destination_address = intent["destination_address"]

    if mode == "auto":
        auto = intent.get("auto_pickup", {})
        request = resolve_request_with_auto_pickups(
            base_request=request,
            amap_key=amap_key,
            driver_origin_address=driver_origin_address,
            passenger_origin_address=passenger_origin_address,
            destination_address=destination_address,
            geocode_city=geocode_city,
            auto_pickup_limit=int(auto.get("limit", 20)),
            auto_pickup_radius_m=int(auto.get("radius_m", 1000)),
            auto_pickup_sample_km=float(auto.get("sample_km", 5.0)),
            auto_pickup_keywords=str(auto.get("keywords", "地铁站|公交站|停车场|商场")),
        )
    else:
        request = resolve_request_from_addresses(
            base_request=request,
            amap_key=amap_key,
            driver_origin_address=driver_origin_address,
            passenger_origin_address=passenger_origin_address,
            destination_address=destination_address,
            pickup_candidate_addresses=[str(x) for x in intent.get("pickup_addresses", [])],
            geocode_city=geocode_city,
        )

    planner = RendezvousPlanner(provider=build_provider("amap", amap_key))
    options, diagnostics = planner.plan_with_diagnostics(request)

    result = {
        "resolved_locations": {
            "driver_origin": {
                "name": request.driver_origin.name,
                "lat": request.driver_origin.lat,
                "lon": request.driver_origin.lon,
            },
            "passenger_origin": {
                "name": request.passenger_origin.name,
                "lat": request.passenger_origin.lat,
                "lon": request.passenger_origin.lon,
            },
            "destination": {
                "name": request.destination.name,
                "lat": request.destination.lat,
                "lon": request.destination.lon,
            },
        },
        "pickup_candidates_count": len(request.pickup_candidates),
        "options": [
            {
                "pickup_point": x.pickup_point.name,
                "score": x.score,
                "eta_driver_to_pickup": x.eta_driver_to_pickup.isoformat(timespec="minutes"),
                "eta_passenger_to_pickup": x.eta_passenger_to_pickup.isoformat(timespec="minutes"),
                "pickup_wait_time_min": x.pickup_wait_time,
                "raw_wait_time_min": x.raw_wait_time,
                "optimized_wait_time_min": x.optimized_wait_time,
                "departure_shift_role": x.departure_shift_role,
                "departure_shift_min": x.departure_shift_min,
                "driver_detour_time_min": x.driver_detour_time,
                "fairness_gap_time_min": x.fairness_gap_time,
                "passenger_transfer_count": x.passenger_transfer_count,
                "pickup_tags": list(x.pickup_tags),
                "total_arrival_time": x.total_arrival_time.isoformat(timespec="minutes"),
            }
            for x in options
        ],
    }
    if show_diagnostics:
        result["diagnostics"] = [
            {"pickup_point": d.pickup_point.name, "reasons": d.reasons}
            for d in diagnostics.filtered_candidates
        ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="结伴而行 V2 (LM Studio + 本地规划引擎)")
    parser.add_argument("--user-request", required=True, help="自然语言请求")
    parser.add_argument("--amap-key", default=os.getenv("AMAP_WEB_SERVICE_KEY"))
    parser.add_argument("--lmstudio-base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--model", default="qwen/qwen3.5-9b")
    parser.add_argument("--show-diagnostics", action="store_true")
    parser.add_argument("--print-intent", action="store_true")
    parser.add_argument("--llm-timeout-sec", type=int, default=30)
    parser.add_argument("--llm-max-retries", type=int, default=1)
    args = parser.parse_args()

    if not args.amap_key:
        raise ValueError("AMap key is required: pass --amap-key or set AMAP_WEB_SERVICE_KEY.")

    system_prompt = intent_prompt_template(datetime.now().isoformat(timespec="minutes"))
    model_output = call_lmstudio_chat(
        base_url=args.lmstudio_base_url,
        model=args.model,
        system_prompt=system_prompt,
        user_prompt=args.user_request,
        timeout_sec=args.llm_timeout_sec,
        max_retries=args.llm_max_retries,
    )
    intent = extract_json_object(model_output)
    intent = apply_request_constraint_overrides(intent, args.user_request)
    validate_intent(intent)
    intent = sanitize_intent(intent)

    if args.print_intent:
        print("Parsed intent:")
        print(json.dumps(intent, ensure_ascii=False, indent=2))

    result = run_plan(intent=intent, amap_key=args.amap_key, show_diagnostics=args.show_diagnostics)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
