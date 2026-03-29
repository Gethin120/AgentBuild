from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from engine import (
    PlanningConstraints,
    RendezvousPlanner,
    ScoringWeights,
    build_provider,
    demo_request,
    resolve_request_from_addresses,
    resolve_request_with_auto_pickups,
)


def call_lmstudio_chat(
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    timeout_sec: int = 180,
    max_retries: int = 2,
    on_retry: Optional[Callable[[int, str], None]] = None,
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
    data = json.dumps(payload).encode("utf-8")
    req = Request(endpoint, data=data, headers={"Content-Type": "application/json"})
    attempt = 0
    while True:
        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
            response = json.loads(raw)
            return response["choices"][0]["message"]["content"]
        except Exception:
            if attempt >= max_retries:
                raise
            attempt += 1
            if on_retry:
                on_retry(attempt, f"LM Studio request retry {attempt}/{max_retries}")


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
  "geocode_city": "string",
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
- Weights should sum to 1.0.
- Be conservative and practical for defaults:
  passenger_travel_max_min=120, driver_detour_max_min=90, max_wait_min=45
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


def sanitize_intent(intent: Dict[str, Any]) -> Dict[str, Any]:
    auto = intent.get("auto_pickup", {})
    keywords = str(auto.get("keywords", "")).strip()
    if not keywords:
        auto["keywords"] = "地铁站|公交站|停车场|商场"
    intent["auto_pickup"] = auto
    return intent


def run_plan(
    intent: Dict[str, Any],
    amap_key: str,
    show_diagnostics: bool,
    progress_enabled: bool = False,
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    def push(stage: str, status: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
        event = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "stage": stage,
            "status": status,
            "message": message,
        }
        if extra:
            event["extra"] = extra
        emit_progress(progress_enabled, stage, status, message, extra)
        if progress_hook:
            progress_hook(event)

    run_started = time.perf_counter()
    push("plan", "start", "Preparing planning request")
    request = demo_request()

    constraints = intent.get("constraints", {})
    weights = intent.get("weights", {})

    request = replace(
        request,
        constraints=PlanningConstraints(
            passenger_travel_max_min=int(constraints.get("passenger_travel_max_min", 120)),
            driver_detour_max_min=int(constraints.get("driver_detour_max_min", 90)),
            max_wait_min=int(constraints.get("max_wait_min", 45)),
        ),
        weights=ScoringWeights(
            arrival_weight=float(weights.get("arrival_weight", 0.55)),
            wait_weight=float(weights.get("wait_weight", 0.25)),
            detour_weight=float(weights.get("detour_weight", 0.20)),
        ),
        top_n=int(intent.get("top_n", 3)),
    )

    mode = intent["candidate_mode"]
    geocode_city = intent.get("geocode_city")
    driver_origin_address = intent["driver_origin_address"]
    passenger_origin_address = intent["passenger_origin_address"]
    destination_address = intent["destination_address"]

    if mode == "auto":
        push("candidates", "start", "Generating auto pickup candidates")
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
        push(
            "candidates",
            "done",
            "Auto pickup candidates generated",
            {"count": len(request.pickup_candidates)},
        )
    else:
        push("candidates", "start", "Resolving manual pickup addresses")
        request = resolve_request_from_addresses(
            base_request=request,
            amap_key=amap_key,
            driver_origin_address=driver_origin_address,
            passenger_origin_address=passenger_origin_address,
            destination_address=destination_address,
            pickup_candidate_addresses=[str(x) for x in intent.get("pickup_addresses", [])],
            geocode_city=geocode_city,
        )
        push(
            "candidates",
            "done",
            "Manual pickup candidates resolved",
            {"count": len(request.pickup_candidates)},
        )

    push("routing", "start", "Evaluating candidate routes")
    planner = RendezvousPlanner(provider=build_provider("amap", amap_key))
    options, diagnostics = planner.plan_with_diagnostics(request)
    push(
        "routing",
        "done",
        "Route evaluation completed",
        {
            "feasible_options": len(options),
            "filtered_candidates": len(diagnostics.filtered_candidates),
            "elapsed_sec": round(time.perf_counter() - run_started, 2),
        },
    )

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
                "pickup_poi": {
                    "name": x.pickup_point.name,
                    "lat": x.pickup_point.lat,
                    "lon": x.pickup_point.lon,
                },
                "to_pickup_plan": {
                    "driver": {
                        "mode": request.driver_mode,
                        "travel_time_min": x.travel_time_driver_to_pickup,
                        "eta_to_pickup": x.eta_driver_to_pickup.isoformat(timespec="minutes"),
                    },
                    "passenger": {
                        "mode": request.passenger_mode,
                        "travel_time_min": x.travel_time_passenger_to_pickup,
                        "eta_to_pickup": x.eta_passenger_to_pickup.isoformat(timespec="minutes"),
                    },
                },
                "score": x.score,
                "eta_driver_to_pickup": x.eta_driver_to_pickup.isoformat(timespec="minutes"),
                "eta_passenger_to_pickup": x.eta_passenger_to_pickup.isoformat(timespec="minutes"),
                "pickup_wait_time_min": x.pickup_wait_time,
                "driver_detour_time_min": x.driver_detour_time,
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
    parser.add_argument("--llm-timeout-sec", type=int, default=180)
    parser.add_argument("--llm-max-retries", type=int, default=2)
    parser.add_argument("--progress", action="store_true", help="Print key stage progress to stderr")
    args = parser.parse_args()

    if not args.amap_key:
        raise ValueError("AMap key is required: pass --amap-key or set AMAP_WEB_SERVICE_KEY.")

    overall_started = time.perf_counter()
    emit_progress(args.progress, "intent", "start", "Parsing natural-language request")
    system_prompt = intent_prompt_template(datetime.now().isoformat(timespec="minutes"))
    model_output = call_lmstudio_chat(
        base_url=args.lmstudio_base_url,
        model=args.model,
        system_prompt=system_prompt,
        user_prompt=args.user_request,
        timeout_sec=args.llm_timeout_sec,
        max_retries=args.llm_max_retries,
        on_retry=(
            lambda attempt, msg: emit_progress(
                args.progress, "intent", "retry", msg, {"attempt": attempt}
            )
        ),
    )
    intent = extract_json_object(model_output)
    validate_intent(intent)
    intent = sanitize_intent(intent)
    emit_progress(args.progress, "intent", "done", "Intent parsed successfully")

    if args.print_intent:
        print("Parsed intent:")
        print(json.dumps(intent, ensure_ascii=False, indent=2))

    result = run_plan(
        intent=intent,
        amap_key=args.amap_key,
        show_diagnostics=args.show_diagnostics,
        progress_enabled=args.progress,
    )
    emit_progress(
        args.progress,
        "complete",
        "done",
        "Agent run completed",
        {"elapsed_sec": round(time.perf_counter() - overall_started, 2)},
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
