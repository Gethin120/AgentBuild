from __future__ import annotations

import unittest

from scripts.compare_reports import compare_reports


class CompareReportsTests(unittest.TestCase):
    def test_compare_reports_handles_replay_summary(self) -> None:
        baseline = {
            "pass_rate": 0.5,
            "avg_feasible_option_count": 1.0,
            "status_counts": {"ok": 1, "no_solution": 1},
            "replan_type_counts": {"expand_wait": 1},
        }
        current = {
            "pass_rate": 0.75,
            "avg_feasible_option_count": 1.5,
            "status_counts": {"ok": 2, "no_solution": 1},
            "replan_type_counts": {"expand_wait": 1, "passenger_delay": 1},
        }

        comparison = compare_reports(baseline, current)

        self.assertEqual(comparison["metric_deltas"]["pass_rate"], 0.25)
        self.assertEqual(comparison["metric_deltas"]["avg_feasible_option_count"], 0.5)
        self.assertEqual(comparison["counter_deltas"]["status_counts"]["ok"], 1)
        self.assertEqual(comparison["counter_deltas"]["replan_type_counts"]["passenger_delay"], 1)

    def test_compare_reports_handles_check_report_trace_summary(self) -> None:
        baseline = {
            "checks": [
                {
                    "name": "trace_summary",
                    "summary": {
                        "success_rate": 0.2,
                        "error_rate": 0.3,
                        "status_counts": {"ok": 2, "error": 3},
                        "action_counts": {"share": 1},
                    },
                }
            ]
        }
        current = {
            "checks": [
                {
                    "name": "trace_summary",
                    "summary": {
                        "success_rate": 0.4,
                        "error_rate": 0.1,
                        "status_counts": {"ok": 4, "error": 1},
                        "action_counts": {"share": 2, "confirm": 1},
                    },
                }
            ]
        }

        comparison = compare_reports(baseline, current)

        self.assertEqual(comparison["metric_deltas"]["success_rate"], 0.2)
        self.assertEqual(comparison["metric_deltas"]["error_rate"], -0.2)
        self.assertEqual(comparison["counter_deltas"]["status_counts"]["ok"], 2)
        self.assertEqual(comparison["counter_deltas"]["action_counts"]["confirm"], 1)


if __name__ == "__main__":
    unittest.main()
