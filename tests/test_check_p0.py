from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_p0 import maybe_load_json, summarize_action_file, validate_eval_cases


class CheckP0HelpersTests(unittest.TestCase):
    def test_maybe_load_json_returns_parsed_object(self) -> None:
        parsed = maybe_load_json('{"ok": true, "count": 2}')

        self.assertEqual(parsed["ok"], True)
        self.assertEqual(parsed["count"], 2)

    def test_maybe_load_json_returns_empty_dict_for_invalid_text(self) -> None:
        self.assertEqual(maybe_load_json("not-json"), {})

    def test_validate_eval_cases_returns_case_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cases_path = Path(tmpdir) / "cases.json"
            cases_path.write_text(
                json.dumps(
                    [
                        {"id": "base"},
                        {"id": "replan", "previous_case_id": "base"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            summary = validate_eval_cases(cases_path)

        self.assertEqual(summary["total_cases"], 2)
        self.assertEqual(summary["replan_case_count"], 1)
        self.assertEqual(summary["case_ids"], ["base", "replan"])

    def test_summarize_action_file_collects_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            actions_path = Path(tmpdir) / "actions.jsonl"
            actions_path.write_text(
                '{"request_id":"r1","action":"share"}\n'
                '{"request_id":"r1","action":"confirm"}\n'
                '{"request_id":"r2","action":"share"}\n',
                encoding="utf-8",
            )

            summary = summarize_action_file(actions_path)

        self.assertEqual(summary["total_actions"], 3)
        self.assertEqual(summary["action_types"], ["confirm", "share"])


if __name__ == "__main__":
    unittest.main()
