from __future__ import annotations

import unittest

from app.core.feedback import (
    apply_feedback_event,
    build_selection_payload,
    feedback_parser_system_prompt,
    feedback_parser_user_prompt,
    normalize_feedback_event,
    resolve_option_reference,
    should_use_llm_feedback_parser,
    validate_feedback_event,
)


class FeedbackTests(unittest.TestCase):
    def test_reason_feedback_maps_to_min_wait_signal(self) -> None:
        event = normalize_feedback_event({"reason": "别让我等太久，我们可以晚点出发"})

        self.assertEqual(event["type"], "plan_feedback")
        self.assertIn({"kind": "wait", "value": "min_wait", "strength": "soft"}, event["signals"])
        self.assertIn(
            {"kind": "wait", "value": "can_depart_later", "strength": "soft"},
            event["signals"],
        )

    def test_reason_feedback_maps_do_not_transfer_phrase(self) -> None:
        event = normalize_feedback_event({"reason": "朋友尽量不要换乘。"})

        self.assertEqual(event["type"], "plan_feedback")
        self.assertIn(
            {"kind": "passenger_experience", "value": "low_transfer", "strength": "soft"},
            event["signals"],
        )

    def test_reason_only_feedback_prefers_llm_parser(self) -> None:
        self.assertTrue(should_use_llm_feedback_parser({"reason": "朋友尽量不要换乘。"}))
        self.assertFalse(
            should_use_llm_feedback_parser(
                {
                    "type": "plan_feedback",
                    "signals": [{"kind": "passenger_experience", "value": "low_transfer"}],
                }
            )
        )

    def test_feedback_parser_prompt_mentions_available_options(self) -> None:
        prompt = feedback_parser_user_prompt(
            "就用龙阳路那个点",
            previous_response_payload={
                "recommended_option": {"pickup_point": "龙阳路地铁站"},
                "alternative_options": [{"pickup_point": "锦绣路地铁站"}],
            },
        )

        self.assertIn("recommended:龙阳路地铁站", prompt)
        self.assertIn("alternative_1:锦绣路地铁站", prompt)
        self.assertIn("strict JSON", feedback_parser_system_prompt())

    def test_invalid_feedback_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "无法理解这条反馈"):
            validate_feedback_event({"reason": "随便调一下"})

    def test_apply_feedback_updates_preferences_and_tags(self) -> None:
        intent = {
            "constraints": {},
            "preference_profile": "balanced",
            "preference_overrides": [],
        }
        updated, control = apply_feedback_event(
            intent,
            {
                "type": "plan_feedback",
                "signals": [
                    {"kind": "wait", "value": "min_wait", "strength": "soft"},
                    {"kind": "passenger_experience", "value": "low_transfer", "strength": "soft"},
                    {"kind": "pickup_point", "value": "prefer_metro", "strength": "soft"},
                ],
            },
        )

        self.assertFalse(control["selection_only"])
        self.assertEqual(updated["preference_profile"], "min_wait")
        self.assertIn("low_transfer", updated["preference_overrides"])
        self.assertIn("metro", updated["prefer_pickup_tags"])

    def test_apply_feedback_recovers_from_empty_feedback_state(self) -> None:
        updated, _ = apply_feedback_event(
            {
                "constraints": {},
                "feedback_state": {},
            },
            {
                "type": "plan_feedback",
                "signals": [{"kind": "wait", "value": "min_wait", "strength": "soft"}],
            },
        )

        self.assertEqual(updated["feedback_state"]["latest_type"], "plan_feedback")
        self.assertEqual(len(updated["feedback_state"]["events"]), 1)

    def test_option_selection_resolves_alternative(self) -> None:
        previous_payload = {
            "recommended_option": {"pickup_point": "A"},
            "alternative_options": [{"pickup_point": "B"}, {"pickup_point": "C"}],
        }

        option = resolve_option_reference(previous_payload, "alternative_2")

        self.assertEqual(option["pickup_point"], "C")

    def test_selection_reason_can_resolve_pickup_point_name(self) -> None:
        previous_payload = {
            "recommended_option": {"pickup_point": "龙阳路地铁站"},
            "alternative_options": [{"pickup_point": "锦绣路地铁站"}],
        }
        updated, control = apply_feedback_event(
            {"constraints": {}},
            {"reason": "就用锦绣路地铁站那个点", "type": "option_selection"},
            previous_response_payload=previous_payload,
        )

        self.assertEqual(updated["selected_option_ref"], "alternative_1")
        self.assertEqual(control["selected_option"]["pickup_point"], "锦绣路地铁站")

    def test_selection_payload_exposes_execution_state(self) -> None:
        previous_payload = {
            "summary": {"destination_name": "上海迪士尼乐园"},
            "recommended_option": {"pickup_point": "龙阳路地铁站"},
            "alternative_options": [
                {
                    "pickup_point": "锦绣路地铁站",
                    "eta_driver_to_pickup": "2026-03-30T09:20",
                    "eta_passenger_to_pickup": "2026-03-30T09:18",
                    "pickup_wait_time_min": 6,
                    "optimized_wait_time_min": 3,
                    "driver_detour_time_min": 18,
                    "total_arrival_time": "2026-03-30T10:35",
                }
            ],
        }

        selected = resolve_option_reference(previous_payload, "alternative_1")
        payload = build_selection_payload(
            previous_response_payload=previous_payload,
            selected_option=selected,
            selected_option_ref="alternative_1",
            feedback_event={"reason": "就按这个方案走"},
        )

        self.assertEqual(payload["status"], "selected")
        self.assertEqual(payload["selected_option"]["pickup_point"], "锦绣路地铁站")
        self.assertEqual(payload["selection_summary"]["selected_option_ref"], "alternative_1")
        self.assertEqual(payload["next_actions"], ["share_plan", "replan_if_delay", "open_navigation"])


if __name__ == "__main__":
    unittest.main()
