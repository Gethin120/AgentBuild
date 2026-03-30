from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
root_str = str(ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)

from scripts.replay_eval import load_cases, validate_cases
from scripts.summarize_trace import load_jsonl
from app.core.runtime_env import load_project_env, preferred_python_command


def run_command(cmd: List[str], *, cwd: Path) -> Dict[str, Any]:
    load_project_env(cwd, override=False)
    env = os.environ.copy()
    env.setdefault("LANGSMITH_TRACING", "false")
    pythonpath_parts = [str(cwd)]
    existing_pythonpath = env.get("PYTHONPATH", "").strip()
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def maybe_load_json(text: str) -> Dict[str, Any]:
    raw = text.strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def validate_eval_cases(cases_path: Path) -> Dict[str, Any]:
    cases = load_cases(cases_path)
    validate_cases(cases)
    linked_replans = [
        case for case in cases if str(case.get("previous_case_id", "") or "").strip()
    ]
    return {
        "cases_path": str(cases_path),
        "total_cases": len(cases),
        "replan_case_count": len(linked_replans),
        "case_ids": [str(case.get("id", "")) for case in cases],
    }


def summarize_action_file(actions_path: Path) -> Dict[str, Any]:
    actions = load_jsonl(actions_path)
    action_types = sorted(
        {
            str(item.get("action", "")).strip()
            for item in actions
            if str(item.get("action", "")).strip()
        }
    )
    return {
        "actions_path": str(actions_path),
        "total_actions": len(actions),
        "action_types": action_types,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the P0 quality checks for rendezvous planner.")
    parser.add_argument("--python-bin")
    parser.add_argument("--skip-replay", action="store_true")
    parser.add_argument("--skip-trace-summary", action="store_true")
    parser.add_argument("--trace-path", default=str(ROOT / ".runs" / "trace.jsonl"))
    parser.add_argument("--actions-path", default=str(ROOT / ".runs" / "actions.jsonl"))
    parser.add_argument("--report-output-path")
    parser.add_argument("--replay-artifacts-dir")
    args = parser.parse_args()
    load_project_env(ROOT, override=False)
    python_cmd = [args.python_bin] if args.python_bin else preferred_python_command(ROOT)

    report: Dict[str, Any] = {
        "root": str(ROOT),
        "checks": [],
        "ok": True,
    }

    eval_cases_path = ROOT / "eval" / "p0_cases.json"
    try:
        eval_case_summary = validate_eval_cases(eval_cases_path)
        report["checks"].append(
            {
                "name": "eval_cases",
                "ok": True,
                "summary": eval_case_summary,
            }
        )
    except Exception as exc:
        report["checks"].append(
            {
                "name": "eval_cases",
                "ok": False,
                "error": str(exc),
            }
        )
        report["ok"] = False

    unit_test_result = run_command(
        [*python_cmd, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
        cwd=ROOT,
    )
    report["checks"].append(
        {
            "name": "unit_tests",
            "ok": unit_test_result["returncode"] == 0,
            "returncode": unit_test_result["returncode"],
            "stdout_tail": unit_test_result["stdout"][-1200:],
            "stderr_tail": unit_test_result["stderr"][-1200:],
        }
    )
    report["ok"] = report["ok"] and unit_test_result["returncode"] == 0

    if not args.skip_replay:
        replay_cmd = [*python_cmd, str(ROOT / "scripts" / "replay_eval.py")]
        if args.replay_artifacts_dir:
            replay_cmd.extend(["--artifacts-dir", args.replay_artifacts_dir])
        replay_result = run_command(replay_cmd, cwd=ROOT)
        replay_summary = maybe_load_json(replay_result["stdout"])
        report["checks"].append(
            {
                "name": "replay_eval",
                "ok": replay_result["returncode"] == 0,
                "returncode": replay_result["returncode"],
                "summary": replay_summary,
                "stderr_tail": replay_result["stderr"][-1200:],
            }
        )
        report["ok"] = report["ok"] and replay_result["returncode"] == 0

    if not args.skip_trace_summary:
        trace_result = run_command(
            [
                *python_cmd,
                str(ROOT / "scripts" / "summarize_trace.py"),
                "--trace-path",
                args.trace_path,
                "--actions-path",
                args.actions_path,
            ],
            cwd=ROOT,
        )
        trace_summary = maybe_load_json(trace_result["stdout"])
        report["checks"].append(
            {
                "name": "trace_summary",
                "ok": trace_result["returncode"] == 0,
                "returncode": trace_result["returncode"],
                "summary": trace_summary,
                "stderr_tail": trace_result["stderr"][-1200:],
            }
        )
        report["ok"] = report["ok"] and trace_result["returncode"] == 0

    report["actions_file"] = summarize_action_file(Path(args.actions_path))

    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report_output_path:
        output_path = Path(args.report_output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
