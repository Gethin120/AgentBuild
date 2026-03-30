from __future__ import annotations

import unittest

from app.core.response import build_natural_language_output, build_response_payload


class ResponsePayloadTests(unittest.TestCase):
    def test_ok_payload_contains_share_and_preference_fields(self) -> None:
        intent = {
            "driver_origin_address": "上海虹桥火车站",
            "passenger_origin_address": "上海世纪大道地铁站",
            "destination_address": "上海迪士尼乐园",
            "constraints": {
                "passenger_travel_max_min": 120,
                "driver_detour_max_min": 90,
                "max_wait_min": 45,
            },
            "preference_profile": "min_wait",
        }
        result = {
            "resolved_locations": {
                "driver_origin": {"name": "上海虹桥火车站"},
                "passenger_origin": {"name": "上海世纪大道地铁站"},
                "destination": {"name": "上海迪士尼乐园"},
            },
            "pickup_candidates_count": 8,
            "options": [
                {
                    "pickup_point": "龙阳路地铁站",
                    "score": 12.5,
                    "eta_driver_to_pickup": "2026-03-30T09:20",
                    "eta_passenger_to_pickup": "2026-03-30T09:18",
                    "pickup_wait_time_min": 2,
                    "driver_detour_time_min": 18,
                    "total_arrival_time": "2026-03-30T10:35",
                },
                {
                    "pickup_point": "锦绣路地铁站",
                    "score": 14.0,
                    "eta_driver_to_pickup": "2026-03-30T09:22",
                    "eta_passenger_to_pickup": "2026-03-30T09:21",
                    "pickup_wait_time_min": 1,
                    "driver_detour_time_min": 25,
                    "total_arrival_time": "2026-03-30T10:40",
                },
            ],
        }

        payload = build_response_payload(intent=intent, result=result, retry_count=1)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["summary"]["preference_profile"], "min_wait")
        self.assertIn("share_text", payload)
        self.assertIn("share_card", payload)
        self.assertTrue(payload["share_card"]["title"].startswith("推荐会合点"))
        self.assertIn(
            payload["recommended_option"]["recommendation_basis"],
            {"min_wait", "best_wait_and_detour", "fast_arrival_with_low_wait", "fast_arrival"},
        )

    def test_no_solution_payload_contains_diagnostics(self) -> None:
        intent = {
            "driver_origin_address": "上海虹桥火车站",
            "passenger_origin_address": "上海陆家嘴地铁站",
            "destination_address": "上海迪士尼乐园",
            "constraints": {
                "passenger_travel_max_min": 90,
                "driver_detour_max_min": 45,
                "max_wait_min": 5,
            },
            "auto_pickup": {"radius_m": 1000},
            "preference_profile": "balanced",
        }
        result = {
            "resolved_locations": {
                "driver_origin": {"name": "上海虹桥火车站"},
                "passenger_origin": {"name": "上海陆家嘴地铁站"},
                "destination": {"name": "上海迪士尼乐园"},
            },
            "pickup_candidates_count": 3,
            "options": [],
            "diagnostics": [
                {"pickup_point": "点位A", "reasons": ["wait_time_exceeded (18 > 5)"]},
                {"pickup_point": "点位B", "reasons": ["wait_time_exceeded (12 > 5)"]},
                {"pickup_point": "点位C", "reasons": ["driver_detour_exceeded (70 > 45)"]},
            ],
        }

        payload = build_response_payload(intent=intent, result=result, retry_count=2)

        self.assertEqual(payload["status"], "no_solution")
        self.assertEqual(payload["primary_bottleneck"], "wait_time_exceeded")
        self.assertEqual(payload["constraint_diagnostics"]["filtered_candidate_count"], 3)
        self.assertEqual(payload["constraint_diagnostics"]["reason_counts"]["wait_time_exceeded"], 2)
        self.assertGreater(
            payload["constraint_diagnostics"]["avg_exceed_by_reason"]["wait_time_exceeded"],
            0,
        )
        self.assertTrue(payload["relaxation_suggestions"])

    def test_no_solution_without_explicit_constraints_uses_candidate_message(self) -> None:
        intent = {
            "driver_origin_address": "上海嘉定区曹安公路4750号",
            "passenger_origin_address": "上海张江高科地铁站",
            "destination_address": "苏州西山岛",
            "constraints": {},
            "preference_profile": "fast_arrival",
            "auto_pickup": {"radius_m": 1500},
        }
        result = {
            "resolved_locations": {
                "driver_origin": {"name": "上海嘉定区曹安公路4750号"},
                "passenger_origin": {"name": "上海张江高科地铁站"},
                "destination": {"name": "苏州西山岛"},
            },
            "pickup_candidates_count": 24,
            "options": [],
            "diagnostics": [],
        }

        payload = build_response_payload(intent=intent, result=result, retry_count=2)

        self.assertEqual(payload["status"], "no_solution")
        self.assertIn("自动候选点范围", payload["error"]["message"])

    def test_replan_summary_is_exposed_in_response(self) -> None:
        intent = {
            "driver_origin_address": "上海虹桥火车站",
            "passenger_origin_address": "上海世纪大道地铁站",
            "destination_address": "上海迪士尼乐园",
            "constraints": {
                "passenger_travel_max_min": 120,
                "driver_detour_max_min": 90,
                "max_wait_min": 45,
            },
            "preference_profile": "balanced",
            "replan_context": {
                "type": "passenger_delay",
                "delay_min": 20,
                "reason": "朋友晚到",
                "changes": [
                    {
                        "field": "passenger_departure_delay_min",
                        "before": 0,
                        "after": 20,
                    }
                ],
            },
            "previous_recommendation": {
                "pickup_point": "世纪公园地铁站",
                "pickup_wait_time_min": 8,
                "driver_detour_time_min": 22,
                "total_arrival_time": "2026-03-30T10:45",
            },
        }
        result = {
            "resolved_locations": {
                "driver_origin": {"name": "上海虹桥火车站"},
                "passenger_origin": {"name": "上海世纪大道地铁站"},
                "destination": {"name": "上海迪士尼乐园"},
            },
            "pickup_candidates_count": 6,
            "options": [
                {
                    "pickup_point": "龙阳路地铁站",
                    "score": 12.5,
                    "eta_driver_to_pickup": "2026-03-30T09:20",
                    "eta_passenger_to_pickup": "2026-03-30T09:38",
                    "pickup_wait_time_min": 18,
                    "driver_detour_time_min": 18,
                    "total_arrival_time": "2026-03-30T10:55",
                }
            ],
        }

        payload = build_response_payload(intent=intent, result=result, retry_count=0)

        self.assertTrue(payload["summary"]["is_replan"])
        self.assertEqual(payload["summary"]["replan_summary"]["title"], "朋友晚点")
        self.assertIn("passenger_departure_delay_min: 0 -> 20", payload["summary"]["replan_summary"]["changes"])
        self.assertTrue(payload["summary"]["replan_delta"]["pickup_changed"])
        self.assertEqual(payload["summary"]["replan_delta"]["previous_pickup_point"], "世纪公园地铁站")
        self.assertEqual(payload["summary"]["replan_delta"]["current_pickup_point"], "龙阳路地铁站")

    def test_long_wait_payload_exposes_warning_and_departure_advice(self) -> None:
        intent = {
            "driver_origin_address": "上海嘉定区曹安公路4750号",
            "passenger_origin_address": "上海张江高科地铁站",
            "destination_address": "苏州西山岛",
            "constraints": {},
            "preference_profile": "balanced",
        }
        result = {
            "resolved_locations": {
                "driver_origin": {"name": "上海嘉定区曹安公路4750号"},
                "passenger_origin": {"name": "上海张江高科地铁站"},
                "destination": {"name": "苏州西山岛"},
            },
            "pickup_candidates_count": 10,
            "options": [
                {
                    "pickup_point": "花桥地铁站",
                    "eta_driver_to_pickup": "2026-03-30T09:10",
                    "eta_passenger_to_pickup": "2026-03-30T10:32",
                    "pickup_wait_time_min": 82,
                    "driver_detour_time_min": 12,
                    "total_arrival_time": "2026-03-30T12:18",
                }
            ],
        }

        payload = build_response_payload(intent=intent, result=result, retry_count=0)

        self.assertEqual(payload["status"], "ok")
        self.assertIn("现场等待较长", payload["summary"]["experience_warning"])
        self.assertEqual(payload["summary"]["departure_advice"], "建议司机稍晚出发约 82 分钟，可明显减少现场等待。")
        self.assertEqual(
            payload["recommended_option"]["departure_advice"],
            "建议司机稍晚出发约 82 分钟，可明显减少现场等待。",
        )

    def test_replan_natural_language_mentions_delta(self) -> None:
        intent = {
            "driver_origin_address": "上海虹桥火车站",
            "passenger_origin_address": "上海世纪大道地铁站",
            "destination_address": "上海迪士尼乐园",
            "replan_context": {
                "type": "passenger_delay",
                "reason": "朋友晚到",
                "changes": [{"field": "passenger_departure_delay_min", "before": 0, "after": 20}],
            },
            "previous_recommendation": {
                "pickup_point": "世纪公园地铁站",
                "pickup_wait_time_min": 8,
                "driver_detour_time_min": 22,
                "total_arrival_time": "2026-03-30T10:45",
            },
        }
        result = {
            "resolved_locations": {
                "driver_origin": {"name": "上海虹桥火车站"},
                "passenger_origin": {"name": "上海世纪大道地铁站"},
                "destination": {"name": "上海迪士尼乐园"},
            },
            "options": [
                {
                    "pickup_point": "龙阳路地铁站",
                    "eta_driver_to_pickup": "2026-03-30T09:20",
                    "eta_passenger_to_pickup": "2026-03-30T09:38",
                    "pickup_wait_time_min": 18,
                    "driver_detour_time_min": 18,
                    "total_arrival_time": "2026-03-30T10:55",
                }
            ],
        }

        text = build_natural_language_output(
            status="ok",
            intent=intent,
            result=result,
            retry_count=0,
        )

        self.assertIn("本次为重规划：朋友晚点", text)
        self.assertIn("与上一次方案相比：", text)
        self.assertIn("会合点由「世纪公园地铁站」调整为「龙阳路地铁站」", text)

    def test_natural_language_mentions_wait_warning_and_departure_advice(self) -> None:
        intent = {
            "driver_origin_address": "上海嘉定区曹安公路4750号",
            "passenger_origin_address": "上海张江高科地铁站",
            "destination_address": "苏州西山岛",
            "constraints": {},
        }
        result = {
            "resolved_locations": {
                "driver_origin": {"name": "上海嘉定区曹安公路4750号"},
                "passenger_origin": {"name": "上海张江高科地铁站"},
                "destination": {"name": "苏州西山岛"},
            },
            "options": [
                {
                    "pickup_point": "花桥地铁站",
                    "eta_driver_to_pickup": "2026-03-30T09:10",
                    "eta_passenger_to_pickup": "2026-03-30T10:32",
                    "pickup_wait_time_min": 82,
                    "driver_detour_time_min": 12,
                    "total_arrival_time": "2026-03-30T12:18",
                }
            ],
        }

        text = build_natural_language_output(
            status="ok",
            intent=intent,
            result=result,
            retry_count=0,
        )

        self.assertIn("现场等待较长", text)
        self.assertIn("建议司机稍晚出发约 82 分钟", text)


if __name__ == "__main__":
    unittest.main()
