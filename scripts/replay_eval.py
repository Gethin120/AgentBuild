from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
root_str = str(ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)

from app.core.runtime_env import load_project_env, preferred_python_command


DEFAULT_CASES_PATH = ROOT / "eval" / "p0_cases.json"


def load_cases(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Cases file must contain a list.")
    return data


def validate_cases(cases: List[Dict[str, Any]]) -> None:
    seen_ids = set()
    for case in cases:
        case_id = str(case.get("id", "") or "").strip()
        if not case_id:
            raise ValueError("Each case must have a non-empty id.")
        if case_id in seen_ids:
            raise ValueError(f"Duplicate case id: {case_id}")
        previous_case_id = str(case.get("previous_case_id", "") or "").strip()
        if previous_case_id and previous_case_id not in seen_ids:
            raise ValueError(
                f"Case '{case_id}' references previous_case_id '{previous_case_id}' "
                "which was not defined earlier in the file."
            )
        seen_ids.add(case_id)


def summarize_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    status_counter = Counter(str(item.get("status", "")) for item in results if item.get("status"))
    failure_counter = Counter(
        str(item.get("failure_category", ""))
        for item in results
        if item.get("failure_category")
    )
    bottleneck_counter = Counter(
        str(item.get("primary_bottleneck", ""))
        for item in results
        if item.get("primary_bottleneck")
    )
    basis_counter = Counter(
        str(item.get("recommendation_basis", ""))
        for item in results
        if item.get("recommendation_basis")
    )
    preference_counter = Counter(
        str(item.get("preference_profile", ""))
        for item in results
        if item.get("preference_profile")
    )
    replan_counter = Counter(
        str(item.get("replan_type", ""))
        for item in results
        if item.get("replan_type")
    )
    linked_replans = [item for item in results if item.get("linked_previous_case_id")]
    feasible_option_counts = [
        int(item.get("feasible_option_count", 0) or 0)
        for item in results
        if item.get("status")
    ]

    passed = sum(1 for item in results if bool(item.get("pass")))
    total = len(results)
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "status_counts": dict(status_counter),
        "failure_category_counts": dict(failure_counter),
        "primary_bottleneck_counts": dict(bottleneck_counter),
        "recommendation_basis_counts": dict(basis_counter),
        "preference_profile_counts": dict(preference_counter),
        "replan_type_counts": dict(replan_counter),
        "replan_case_count": len(linked_replans),
        "avg_feasible_option_count": round(sum(feasible_option_counts) / len(feasible_option_counts), 4)
        if feasible_option_counts
        else 0.0,
        "results": results,
    }


def run_case(
    case: Dict[str, Any],
    python_cmd: List[str],
    *,
    previous_payload: Dict[str, Any] | None = None,
    artifacts_dir: Path | None = None,
) -> Tuple[bool, Dict[str, Any], Dict[str, Any]]:
    intent = case.get("intent", {})
    replan_event = case.get("replan_event", {})
    feedback_event = case.get("feedback_event", {})
    expected = case.get("expected", {})
    allowed_statuses = list(expected.get("allowed_statuses", ["ok"]))
    min_feasible_options = int(expected.get("min_feasible_options", 1))
    expected_is_replan = expected.get("is_replan")
    expected_replan_type = expected.get("replan_type")

    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as tmp:
        json.dump(intent, tmp, ensure_ascii=False)
        tmp_path = tmp.name

    cmd = [
        *python_cmd,
        "-m",
        "app.agent",
        "--user-request",
        str(case.get("user_request", case.get("id", ""))),
        "--intent-json-path",
        tmp_path,
        "--json-stdout",
        "--disable-thinking",
        "--disable-llm-strategy",
        "--disable-llm-judge",
    ]
    replan_tmp_path = ""
    feedback_tmp_path = ""
    previous_response_tmp_path = ""
    output_json_path = ""
    intent_output_json_path = ""
    case_id = str(case.get("id", "case"))
    if artifacts_dir is not None:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        output_json_path = str(artifacts_dir / f"{case_id}.response.json")
        intent_output_json_path = str(artifacts_dir / f"{case_id}.intent.json")
        cmd.extend(["--output-json-path", output_json_path, "--intent-output-json-path", intent_output_json_path])
    if replan_event:
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as replan_tmp:
            json.dump(replan_event, replan_tmp, ensure_ascii=False)
            replan_tmp_path = replan_tmp.name
        cmd.extend(["--replan-event-json-path", replan_tmp_path])
    if feedback_event:
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as feedback_tmp:
            json.dump(feedback_event, feedback_tmp, ensure_ascii=False)
            feedback_tmp_path = feedback_tmp.name
        cmd.extend(["--feedback-json-path", feedback_tmp_path])
        if case.get("selected_option_ref"):
            cmd.extend(["--selected-option-ref", str(case.get("selected_option_ref"))])
    if previous_payload:
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as previous_tmp:
            json.dump(previous_payload, previous_tmp, ensure_ascii=False)
            previous_response_tmp_path = previous_tmp.name
        cmd.extend(["--previous-response-json-path", previous_response_tmp_path])
    try:
        env = os.environ.copy()
        env.setdefault("LANGSMITH_TRACING", "false")
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        if replan_tmp_path:
            try:
                os.unlink(replan_tmp_path)
            except FileNotFoundError:
                pass
        if feedback_tmp_path:
            try:
                os.unlink(feedback_tmp_path)
            except FileNotFoundError:
                pass
        if previous_response_tmp_path:
            try:
                os.unlink(previous_response_tmp_path)
            except FileNotFoundError:
                pass

    stdout = proc.stdout.strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as exc:
        return False, {
            "id": case.get("id"),
            "pass": False,
            "error": f"invalid_json_output: {exc}",
            "stdout": stdout[-1000:],
            "stderr": proc.stderr[-1000:],
        }, {}

    status = str(payload.get("status", ""))
    feasible_option_count = int(((payload.get("metrics_summary") or {}).get("feasible_option_count", 0)))
    actual_is_replan = (((payload.get("response_payload") or {}).get("summary") or {}).get("is_replan"))
    actual_replan_type = (((payload.get("metrics_summary") or {}).get("replan_type")))
    case_pass = status in allowed_statuses and feasible_option_count >= min_feasible_options
    if expected_is_replan is not None:
        case_pass = case_pass and bool(actual_is_replan) == bool(expected_is_replan)
    if expected_replan_type is not None:
        case_pass = case_pass and str(actual_replan_type or "") == str(expected_replan_type)

    return case_pass, {
        "id": case.get("id"),
        "description": case.get("description", ""),
        "pass": case_pass,
        "status": status,
        "allowed_statuses": allowed_statuses,
        "feasible_option_count": feasible_option_count,
        "min_feasible_options": min_feasible_options,
        "expected_is_replan": expected_is_replan,
        "expected_replan_type": expected_replan_type,
        "failure_category": ((payload.get("metrics_summary") or {}).get("failure_category")),
        "primary_bottleneck": ((payload.get("response_payload") or {}).get("primary_bottleneck")),
        "preference_profile": (((payload.get("response_payload") or {}).get("summary") or {}).get("preference_profile")),
        "preference_alignment": (((payload.get("metrics_summary") or {}).get("preference_alignment"))),
        "recommendation_basis": (((payload.get("response_payload") or {}).get("recommended_option") or {}).get("recommendation_basis")),
        "is_replan": (((payload.get("response_payload") or {}).get("summary") or {}).get("is_replan")),
        "replan_type": (((payload.get("metrics_summary") or {}).get("replan_type"))),
        "selected_option_ref": (((payload.get("metrics_summary") or {}).get("selected_option_ref"))),
        "replan_delta": (((payload.get("response_payload") or {}).get("summary") or {}).get("replan_delta")),
        "linked_previous_case_id": case.get("previous_case_id"),
        "artifacts": {
            "response_json_path": output_json_path,
            "intent_json_path": intent_output_json_path,
        },
        "stderr": proc.stderr[-1000:],
    }, payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay P0 evaluation cases for rendezvous planner.")
    parser.add_argument("--cases-path", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--python-bin")
    parser.add_argument("--output-path")
    parser.add_argument("--artifacts-dir")
    args = parser.parse_args()

    load_project_env(ROOT, override=False)
    cases = load_cases(Path(args.cases_path))
    validate_cases(cases)
    python_cmd = [args.python_bin] if args.python_bin else preferred_python_command(ROOT)
    results = []
    passed = 0
    payloads_by_case_id: Dict[str, Dict[str, Any]] = {}
    artifacts_dir = Path(args.artifacts_dir) if args.artifacts_dir else None
    for case in cases:
        previous_payload = None
        previous_case_id = str(case.get("previous_case_id", "") or "").strip()
        if previous_case_id:
            previous_payload = payloads_by_case_id.get(previous_case_id)
            if previous_payload is None:
                results.append(
                    {
                        "id": case.get("id"),
                        "description": case.get("description", ""),
                        "pass": False,
                        "error": f"missing_previous_case_id: {previous_case_id}",
                    }
                )
                continue
        case_pass, result, raw_payload = run_case(
            case,
            python_cmd,
            previous_payload=previous_payload,
            artifacts_dir=artifacts_dir,
        )
        results.append(result)
        if case_pass:
            passed += 1
        if case.get("id"):
            payloads_by_case_id[str(case["id"])] = raw_payload

    summary = summarize_results(results)

    output = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output_path:
        Path(args.output_path).write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
