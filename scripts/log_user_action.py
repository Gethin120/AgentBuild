from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


ALLOWED_ACTIONS = {
    "share",
    "confirm",
    "replan",
    "discuss",
    "dismiss",
    "open_navigation",
}


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Log a user action for a rendezvous planning request.")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--action", required=True, choices=sorted(ALLOWED_ACTIONS))
    parser.add_argument("--actions-path", default=str(Path(".runs/actions.jsonl")))
    parser.add_argument("--pickup-point")
    parser.add_argument("--note", default="")
    parser.add_argument("--metadata-json", default="")
    args = parser.parse_args()

    metadata: Dict[str, Any] = {}
    if args.metadata_json:
        metadata = json.loads(args.metadata_json)

    payload = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "request_id": args.request_id,
        "action": args.action,
        "pickup_point": args.pickup_point or "",
        "note": args.note,
        "metadata": metadata,
    }
    append_jsonl(Path(args.actions_path), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
