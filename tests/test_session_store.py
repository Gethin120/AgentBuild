from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.session_store import (
    close_session,
    create_session,
    get_active_session,
    get_active_session_id,
    list_sessions,
    load_session,
    persist_turn_state,
)


class SessionStoreTests(unittest.TestCase):
    def test_create_session_sets_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            session = create_session(root, title="测试会话", initial_intent={})

            self.assertEqual(get_active_session_id(root), session["session_id"])
            self.assertEqual(load_session(root, session["session_id"])["title"], "测试会话")

    def test_persist_turn_state_updates_latest_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            session = create_session(root, title="结伴", initial_intent={})
            persisted = persist_turn_state(
                root,
                session_id=session["session_id"],
                turn_type="feedback",
                user_input="别让我等太久",
                intent={
                    "driver_origin_address": "上海虹桥火车站",
                    "passenger_origin_address": "上海世纪大道地铁站",
                    "destination_address": "上海迪士尼乐园",
                    "preference_profile": "min_wait",
                    "preference_overrides": ["low_transfer"],
                    "constraints": {},
                },
                response_payload={
                    "status": "ok",
                    "recommended_option": {"pickup_point": "龙阳路地铁站"},
                },
                metrics_summary={
                    "feedback_event": {"reason": "别让我等太久"},
                },
            )

            self.assertEqual(persisted["turn_count"], 1)
            self.assertEqual(persisted["preference_state"]["primary_preference"], "min_wait")
            self.assertEqual(persisted["feedback_history"][0]["reason"], "别让我等太久")
            self.assertEqual(get_active_session(root)["current_response"]["status"], "ok")
            session_root = root / ".runs" / "sessions" / session["session_id"]
            self.assertFalse((session_root / "latest_intent.json").exists())
            self.assertFalse((session_root / "latest_response.json").exists())
            self.assertFalse((session_root / "latest_metrics.json").exists())

    def test_close_session_marks_closed_and_clears_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            session = create_session(root, title="结伴", initial_intent={})

            close_session(root, session["session_id"])

            self.assertEqual(load_session(root, session["session_id"])["status"], "closed")
            self.assertEqual(get_active_session_id(root), "")

    def test_list_sessions_returns_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            create_session(root, title="A", initial_intent={})
            create_session(root, title="B", initial_intent={})

            sessions = list_sessions(root, limit=10)

            self.assertEqual(len(sessions), 2)
            self.assertIn("session_id", sessions[0])


if __name__ == "__main__":
    unittest.main()
