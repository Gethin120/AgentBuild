from __future__ import annotations

import unittest

from scripts.replay_eval import summarize_results, validate_cases


class ReplayEvalValidationTests(unittest.TestCase):
    def test_validate_cases_accepts_linear_replan_chain(self) -> None:
        cases = [
            {"id": "base"},
            {"id": "replan-1", "previous_case_id": "base"},
            {"id": "replan-2", "previous_case_id": "replan-1"},
        ]

        validate_cases(cases)

    def test_validate_cases_rejects_duplicate_ids(self) -> None:
        cases = [
            {"id": "dup"},
            {"id": "dup"},
        ]

        with self.assertRaisesRegex(ValueError, "Duplicate case id: dup"):
            validate_cases(cases)

    def test_validate_cases_rejects_unknown_previous_case(self) -> None:
        cases = [
            {"id": "replan-1", "previous_case_id": "missing-base"},
        ]

        with self.assertRaisesRegex(ValueError, "missing-base"):
            validate_cases(cases)

    def test_validate_cases_rejects_forward_reference(self) -> None:
        cases = [
            {"id": "replan-1", "previous_case_id": "base"},
            {"id": "base"},
        ]

        with self.assertRaisesRegex(ValueError, "not defined earlier"):
            validate_cases(cases)

    def test_validate_cases_rejects_empty_id(self) -> None:
        cases = [
            {"id": "base"},
            {"id": "   "},
        ]

        with self.assertRaisesRegex(ValueError, "non-empty id"):
            validate_cases(cases)


class ReplayEvalSummaryTests(unittest.TestCase):
    def test_summarize_results_aggregates_key_counts(self) -> None:
        results = [
            {
                "id": "base",
                "pass": True,
                "status": "ok",
                "feasible_option_count": 3,
                "recommendation_basis": "fast_arrival",
                "preference_profile": "fast_arrival",
            },
            {
                "id": "tight",
                "pass": False,
                "status": "no_solution",
                "feasible_option_count": 0,
                "failure_category": "constraint_conflict",
                "primary_bottleneck": "wait_time_exceeded",
                "linked_previous_case_id": "base",
                "replan_type": "expand_wait",
            },
        ]

        summary = summarize_results(results)

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["status_counts"]["ok"], 1)
        self.assertEqual(summary["status_counts"]["no_solution"], 1)
        self.assertEqual(summary["failure_category_counts"]["constraint_conflict"], 1)
        self.assertEqual(summary["primary_bottleneck_counts"]["wait_time_exceeded"], 1)
        self.assertEqual(summary["recommendation_basis_counts"]["fast_arrival"], 1)
        self.assertEqual(summary["preference_profile_counts"]["fast_arrival"], 1)
        self.assertEqual(summary["replan_type_counts"]["expand_wait"], 1)
        self.assertEqual(summary["replan_case_count"], 1)
        self.assertEqual(summary["avg_feasible_option_count"], 1.5)


if __name__ == "__main__":
    unittest.main()
