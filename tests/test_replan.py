from __future__ import annotations

import unittest

from app.core.replan import apply_replan_event


class ReplanEventTests(unittest.TestCase):
    def test_passenger_delay_event_updates_intent(self) -> None:
        intent = {
            "constraints": {
                "passenger_travel_max_min": 120,
                "driver_detour_max_min": 90,
                "max_wait_min": 45,
            },
            "auto_pickup": {"radius_m": 1000, "limit": 20},
            "passenger_departure_delay_min": 5,
        }
        event = {
            "type": "passenger_delay",
            "delay_min": 20,
            "reason": "朋友晚到",
        }

        updated = apply_replan_event(intent, event)

        self.assertEqual(updated["passenger_departure_delay_min"], 25)
        self.assertEqual(updated["replan_context"]["type"], "passenger_delay")
        self.assertEqual(updated["replan_context"]["reason"], "朋友晚到")
        self.assertEqual(updated["replan_context"]["changes"][0]["field"], "passenger_departure_delay_min")
        self.assertEqual(updated["replan_context"]["changes"][0]["before"], 5)
        self.assertEqual(updated["replan_context"]["changes"][0]["after"], 25)

    def test_expand_wait_and_radius_updates_constraints(self) -> None:
        intent = {
            "constraints": {
                "passenger_travel_max_min": 90,
                "driver_detour_max_min": 45,
                "max_wait_min": 5,
            },
            "auto_pickup": {"radius_m": 1000, "limit": 20},
        }

        updated_wait = apply_replan_event(intent, {"type": "expand_wait", "delta_min": 15})
        updated_radius = apply_replan_event(intent, {"type": "expand_search_radius", "delta_min": 500})

        self.assertEqual(updated_wait["constraints"]["max_wait_min"], 20)
        self.assertEqual(updated_wait["replan_context"]["changes"][0]["field"], "max_wait_min")
        self.assertEqual(updated_radius["auto_pickup"]["radius_m"], 1500)


if __name__ == "__main__":
    unittest.main()
