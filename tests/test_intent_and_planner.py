from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.intent_and_planner import (
    _build_lmstudio_headers,
    apply_request_constraint_overrides,
    sanitize_intent,
)


class SanitizeIntentTests(unittest.TestCase):
    def test_lmstudio_headers_include_authorization_when_key_present(self) -> None:
        with patch.dict(os.environ, {"LM_STUDIO_API_KEY": "sk-test-key"}, clear=False):
            headers = _build_lmstudio_headers()

        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Authorization"], "Bearer sk-test-key")

    def test_lmstudio_headers_without_key_only_keep_content_type(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            headers = _build_lmstudio_headers()

        self.assertEqual(headers, {"Content-Type": "application/json"})

    def test_infers_city_and_preference_profile_from_weights(self) -> None:
        intent = {
            "driver_origin_address": "上海市虹桥火车站",
            "passenger_origin_address": "上海市世纪大道地铁站",
            "destination_address": "上海市迪士尼乐园",
            "geocode_city": "",
            "candidate_mode": "auto",
            "pickup_addresses": [],
            "constraints": {
                "passenger_travel_max_min": 120,
                "driver_detour_max_min": 90,
                "max_wait_min": 45,
            },
            "weights": {
                "arrival_weight": 0.8,
                "wait_weight": 0.1,
                "detour_weight": 0.1,
            },
            "top_n": 3,
            "auto_pickup": {
                "limit": 20,
                "radius_m": 1000,
                "sample_km": 5.0,
                "keywords": "",
            },
        }

        sanitized = sanitize_intent(intent)

        self.assertEqual(sanitized["geocode_city"], "上海")
        self.assertEqual(sanitized["preference_profile"], "fast_arrival")
        self.assertEqual(sanitized["auto_pickup"]["keywords"], "地铁站|公交站|停车场|商场")
        self.assertAlmostEqual(sum(sanitized["weights"].values()), 1.0, places=3)

    def test_balanced_profile_when_weights_are_close(self) -> None:
        intent = {
            "driver_origin_address": "杭州东站",
            "passenger_origin_address": "杭州龙翔桥地铁站",
            "destination_address": "杭州西湖风景名胜区",
            "geocode_city": "杭州",
            "candidate_mode": "auto",
            "pickup_addresses": [],
            "constraints": {},
            "weights": {
                "arrival_weight": 0.4,
                "wait_weight": 0.34,
                "detour_weight": 0.26,
            },
            "top_n": 3,
            "auto_pickup": {},
        }

        sanitized = sanitize_intent(intent)

        self.assertEqual(sanitized["preference_profile"], "balanced")
        self.assertEqual(sanitized["driver_departure_delay_min"], 0)
        self.assertEqual(sanitized["passenger_departure_delay_min"], 0)

    def test_constraints_stay_empty_when_user_did_not_provide_limits(self) -> None:
        intent = {
            "driver_origin_address": "上海虹桥火车站",
            "passenger_origin_address": "上海世纪大道地铁站",
            "destination_address": "上海迪士尼乐园",
            "geocode_city": "",
            "candidate_mode": "auto",
            "pickup_addresses": [],
            "constraints": {},
            "weights": {
                "arrival_weight": 0.55,
                "wait_weight": 0.25,
                "detour_weight": 0.2,
            },
            "top_n": 3,
            "auto_pickup": {},
        }

        sanitized = sanitize_intent(intent)

        self.assertEqual(sanitized["constraints"], {})

    def test_request_constraint_overrides_fix_explicit_limits(self) -> None:
        intent = {
            "driver_origin_address": "上海虹桥火车站",
            "passenger_origin_address": "上海世纪大道地铁站",
            "destination_address": "上海迪士尼乐园",
            "geocode_city": "上海",
            "candidate_mode": "auto",
            "pickup_addresses": [],
            "constraints": {
                "passenger_travel_max_min": 240,
                "driver_detour_max_min": 180,
                "max_wait_min": 90,
            },
            "weights": {
                "arrival_weight": 0.4,
                "wait_weight": 0.3,
                "detour_weight": 0.3,
            },
            "top_n": 3,
            "auto_pickup": {},
        }
        user_request = (
            "我从上海虹桥火车站出发，朋友在上海世纪大道地铁站，我们一起去上海迪士尼乐园。"
            "自动找会合点，朋友公交不超过120分钟，我最多绕路90分钟，最多等待45分钟。"
        )

        patched = apply_request_constraint_overrides(intent, user_request)

        self.assertEqual(patched["constraints"]["passenger_travel_max_min"], 120)
        self.assertEqual(patched["constraints"]["driver_detour_max_min"], 90)
        self.assertEqual(patched["constraints"]["max_wait_min"], 45)


if __name__ == "__main__":
    unittest.main()
