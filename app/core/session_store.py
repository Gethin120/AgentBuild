from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4


DEFAULT_STATUS = "active"


def sessions_root(base_dir: Path) -> Path:
    return base_dir / ".runs" / "sessions"


def index_path(base_dir: Path) -> Path:
    return sessions_root(base_dir) / "index.json"


def session_dir(base_dir: Path, session_id: str) -> Path:
    return sessions_root(base_dir) / session_id


def load_index(base_dir: Path) -> Dict[str, Any]:
    path = index_path(base_dir)
    if not path.exists():
        return {"active_session_id": "", "sessions": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"active_session_id": "", "sessions": []}


def save_index(base_dir: Path, payload: Dict[str, Any]) -> None:
    path = index_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate_session_id() -> str:
    return f"sess_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


def build_session_title(intent: Dict[str, Any]) -> str:
    driver = str(intent.get("driver_origin_address", "") or "").strip()
    passenger = str(intent.get("passenger_origin_address", "") or "").strip()
    destination = str(intent.get("destination_address", "") or "").strip()
    if driver and passenger and destination:
        return f"{driver} + {passenger} -> {destination}"
    return "结伴出行会话"


def create_session(base_dir: Path, *, title: str, initial_intent: Dict[str, Any] | None = None) -> Dict[str, Any]:
    session_id = generate_session_id()
    now = datetime.now().isoformat(timespec="seconds")
    payload = {
        "session_id": session_id,
        "status": DEFAULT_STATUS,
        "created_at": now,
        "updated_at": now,
        "turn_count": 0,
        "title": title or "结伴出行会话",
        "current_intent": dict(initial_intent or {}),
        "current_response": {},
        "selected_option": {},
        "preference_state": {},
        "feedback_history": [],
        "turn_refs": [],
    }
    save_session(base_dir, payload)
    set_active_session_id(base_dir, session_id)
    return payload


def load_session(base_dir: Path, session_id: str) -> Dict[str, Any]:
    path = session_dir(base_dir, session_id) / "session.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_session(base_dir: Path, payload: Dict[str, Any]) -> None:
    session_id = str(payload.get("session_id", "") or "").strip()
    if not session_id:
        raise ValueError("session_id is required.")
    root = session_dir(base_dir, session_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "session.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _sync_index_summary(base_dir, payload)


def append_turn(base_dir: Path, session_id: str, turn_payload: Dict[str, Any]) -> None:
    root = session_dir(base_dir, session_id)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "turns.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(turn_payload, ensure_ascii=False) + "\n")


def set_active_session_id(base_dir: Path, session_id: str) -> None:
    index = load_index(base_dir)
    index["active_session_id"] = session_id
    save_index(base_dir, index)


def get_active_session_id(base_dir: Path) -> str:
    return str(load_index(base_dir).get("active_session_id", "") or "")


def get_active_session(base_dir: Path) -> Dict[str, Any]:
    session_id = get_active_session_id(base_dir)
    if not session_id:
        return {}
    return load_session(base_dir, session_id)


def list_sessions(base_dir: Path, limit: int = 10) -> List[Dict[str, Any]]:
    index = load_index(base_dir)
    sessions = list(index.get("sessions", []) or [])
    sessions.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
    return sessions[:limit]


def close_session(base_dir: Path, session_id: str) -> Dict[str, Any]:
    session = load_session(base_dir, session_id)
    if not session:
        return {}
    session["status"] = "closed"
    session["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_session(base_dir, session)
    if get_active_session_id(base_dir) == session_id:
        set_active_session_id(base_dir, "")
    return session


def persist_turn_state(
    base_dir: Path,
    *,
    session_id: str,
    turn_type: str,
    user_input: str,
    intent: Dict[str, Any],
    response_payload: Dict[str, Any],
    metrics_summary: Dict[str, Any],
) -> Dict[str, Any]:
    existing = load_session(base_dir, session_id)
    if not existing:
        existing = create_session(base_dir, title=build_session_title(intent), initial_intent=intent)
        session_id = existing["session_id"]

    now = datetime.now().isoformat(timespec="seconds")
    turn_id = f"turn_{existing.get('turn_count', 0) + 1:03d}"
    feedback_event = dict((metrics_summary.get("feedback_event") or {}))
    status = str(response_payload.get("status", "active") or "active")
    session_status = "selected" if status == "selected" else existing.get("status", DEFAULT_STATUS)
    if status == "error":
        session_status = existing.get("status", DEFAULT_STATUS)

    updated = dict(existing)
    updated["updated_at"] = now
    updated["turn_count"] = int(existing.get("turn_count", 0) or 0) + 1
    if not str(existing.get("title", "") or "").strip() or existing.get("title") == "结伴出行会话":
        updated["title"] = build_session_title(intent)
    updated["current_intent"] = dict(intent or {})
    updated["current_response"] = dict(response_payload or {})
    updated["status"] = session_status
    updated["turn_refs"] = list(existing.get("turn_refs", []) or []) + [turn_id]
    updated["preference_state"] = {
        "primary_preference": intent.get("preference_profile", "balanced"),
        "preference_overrides": list(intent.get("preference_overrides", []) or []),
        "prefer_pickup_tags": list(intent.get("prefer_pickup_tags", []) or []),
        "avoid_pickup_tags": list(intent.get("avoid_pickup_tags", []) or []),
        "exclude_pickup_points": list(intent.get("exclude_pickup_points", []) or []),
        "max_departure_shift_min": int(intent.get("max_departure_shift_min", 60) or 60),
        "hard_constraints": dict(intent.get("constraints", {}) or {}),
    }
    if feedback_event:
        updated["feedback_history"] = list(existing.get("feedback_history", []) or []) + [feedback_event]
    selected_option = response_payload.get("selected_option") or {}
    if selected_option:
        updated["selected_option"] = selected_option

    append_turn(
        base_dir,
        session_id,
        {
            "turn_id": turn_id,
            "session_id": session_id,
            "turn_type": turn_type,
            "user_input": user_input,
            "feedback_event": feedback_event,
            "intent_snapshot": intent,
            "response_snapshot": response_payload,
            "metrics_summary": metrics_summary,
            "created_at": now,
        },
    )
    updated["session_id"] = session_id
    save_session(base_dir, updated)
    set_active_session_id(base_dir, session_id)
    return updated


def _sync_index_summary(base_dir: Path, session_payload: Dict[str, Any]) -> None:
    index = load_index(base_dir)
    summaries = list(index.get("sessions", []) or [])
    session_id = str(session_payload.get("session_id", "") or "")
    summary = {
        "session_id": session_id,
        "title": session_payload.get("title", ""),
        "status": session_payload.get("status", DEFAULT_STATUS),
        "updated_at": session_payload.get("updated_at", ""),
        "turn_count": int(session_payload.get("turn_count", 0) or 0),
    }
    replaced = False
    for idx, item in enumerate(summaries):
        if str(item.get("session_id", "")) == session_id:
            summaries[idx] = summary
            replaced = True
            break
    if not replaced:
        summaries.append(summary)
    index["sessions"] = summaries
    if not index.get("active_session_id"):
        index["active_session_id"] = session_id
    save_index(base_dir, index)
