from __future__ import annotations

import unittest
from datetime import datetime

from app.engine import (
    Location,
    PlanningConstraints,
    RendezvousPlanner,
    RendezvousRequest,
    ScoringWeights,
)


class _StubProvider:
    def __init__(self, times, transfers):
        self.times = times
        self.transfers = transfers

    def estimate_minutes(self, origin, destination, mode, depart_at):
        return self.times[(origin.name, destination.name, mode)]

    def estimate_details(self, origin, destination, mode, depart_at):
        return {
            "minutes": self.estimate_minutes(origin, destination, mode, depart_at),
            "transfer_count": self.transfers.get((origin.name, destination.name, mode), 0),
        }


class EnginePreferenceTests(unittest.TestCase):
    def test_min_wait_prefers_optimized_wait(self) -> None:
        driver = Location("driver", 0, 0)
        passenger = Location("passenger", 0, 0)
        destination = Location("destination", 0, 0)
        pickup_a = Location("A地铁站", 0, 0)
        pickup_b = Location("B地铁站", 0, 0)
        provider = _StubProvider(
            {
                ("driver", "destination", "driving"): 100,
                ("driver", "A地铁站", "driving"): 20,
                ("passenger", "A地铁站", "transit"): 80,
                ("A地铁站", "destination", "driving"): 95,
                ("driver", "B地铁站", "driving"): 55,
                ("passenger", "B地铁站", "transit"): 130,
                ("B地铁站", "destination", "driving"): 55,
            },
            {},
        )
        planner = RendezvousPlanner(provider)
        request = RendezvousRequest(
            driver_origin=driver,
            passenger_origin=passenger,
            destination=destination,
            departure_time=datetime(2026, 3, 30, 9, 0),
            passenger_departure_time=datetime(2026, 3, 30, 9, 0),
            pickup_candidates=[pickup_a, pickup_b],
            constraints=PlanningConstraints(),
            weights=ScoringWeights(arrival_weight=0.05, wait_weight=0.9, detour_weight=0.05),
            preference_profile="min_wait",
            max_departure_shift_min=60,
        )

        options = planner.plan(request)

        self.assertEqual(options[0].pickup_point.name, "A地铁站")
        self.assertEqual(options[0].raw_wait_time, 60)
        self.assertEqual(options[0].optimized_wait_time, 0)
        self.assertEqual(options[0].departure_shift_role, "driver")

    def test_low_transfer_and_fairness_affect_ranking(self) -> None:
        driver = Location("driver", 0, 0)
        passenger = Location("passenger", 0, 0)
        destination = Location("destination", 0, 0)
        pickup_a = Location("A地铁站", 0, 0)
        pickup_b = Location("B地铁站", 0, 0)
        provider = _StubProvider(
            {
                ("driver", "destination", "driving"): 80,
                ("driver", "A地铁站", "driving"): 20,
                ("passenger", "A地铁站", "transit"): 18,
                ("A地铁站", "destination", "driving"): 60,
                ("driver", "B地铁站", "driving"): 20,
                ("passenger", "B地铁站", "transit"): 18,
                ("B地铁站", "destination", "driving"): 60,
            },
            {
                ("passenger", "A地铁站", "transit"): 3,
                ("passenger", "B地铁站", "transit"): 1,
            },
        )
        planner = RendezvousPlanner(provider)
        request = RendezvousRequest(
            driver_origin=driver,
            passenger_origin=passenger,
            destination=destination,
            departure_time=datetime(2026, 3, 30, 9, 0),
            pickup_candidates=[pickup_a, pickup_b],
            constraints=PlanningConstraints(),
            weights=ScoringWeights(),
            preference_profile="balanced",
            preference_overrides=("low_transfer", "balanced_fairness"),
        )

        options = planner.plan(request)

        self.assertEqual(options[0].pickup_point.name, "B地铁站")
        self.assertEqual(options[0].passenger_transfer_count, 1)


if __name__ == "__main__":
    unittest.main()
