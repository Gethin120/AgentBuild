from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            rows.append(json.loads(text))
    return rows


def average(values: List[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _fallback_metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    metrics = row.get("metrics_summary")
    if isinstance(metrics, dict) and metrics:
        return metrics

    error_text = str(row.get("error", "") or "")
    plan_result = row.get("plan_result", {}) or {}
    options = plan_result.get("options", []) or []
    status = "error" if error_text else ("ok" if options else "unknown")
    return {
        "status": status,
        "success_flag": 1 if status == "ok" else 0,
        "no_solution_flag": 0,
        "error_flag": 1 if status == "error" else 0,
        "retry_count": float(row.get("retry_count", 0) or 0),
        "constraint_hit_rate": 0.0,
        "candidate_utilization": 0.0,
        "feasible_option_count": len(options),
        "failure_category": "",
        "primary_bottleneck": "",
        "recommendation_basis": "",
        "recommendation_tags": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize rendezvous planner trace metrics.")
    parser.add_argument("--trace-path", default=str(Path(".runs/trace.jsonl")))
    parser.add_argument("--actions-path", default=str(Path(".runs/actions.jsonl")))
    args = parser.parse_args()

    rows = load_jsonl(Path(args.trace_path))
    actions = load_jsonl(Path(args.actions_path))
    metrics = [_fallback_metrics(row) for row in rows]

    status_counter = Counter(str(m.get("status", "unknown")) for m in metrics)
    failure_counter = Counter(str(m.get("failure_category", "")) for m in metrics if m.get("failure_category"))
    bottleneck_counter = Counter(str(m.get("primary_bottleneck", "")) for m in metrics if m.get("primary_bottleneck"))
    basis_counter = Counter(str(m.get("recommendation_basis", "")) for m in metrics if m.get("recommendation_basis"))
    tag_counter = Counter(tag for m in metrics for tag in (m.get("recommendation_tags", []) or []))
    preference_counter = Counter(str(m.get("preference_profile", "")) for m in metrics if m.get("preference_profile"))
    filtered_counts = [float(m.get("filtered_candidate_count", 0) or 0) for m in metrics]
    replan_counter = Counter(str(m.get("replan_type", "")) for m in metrics if m.get("replan_type"))
    replan_flags = [1.0 if bool(m.get("is_replan", False)) else 0.0 for m in metrics]

    reason_counter = Counter()
    for metric in metrics:
        for key, value in (metric.get("reason_counts", {}) or {}).items():
            reason_counter[str(key)] += int(value)

    action_counter = Counter(str(item.get("action", "")) for item in actions if item.get("action"))
    requests_with_actions = {str(item.get("request_id", "")) for item in actions if item.get("request_id")}
    confirmed_requests = {
        str(item.get("request_id", ""))
        for item in actions
        if str(item.get("action", "")) == "confirm" and item.get("request_id")
    }
    shared_requests = {
        str(item.get("request_id", ""))
        for item in actions
        if str(item.get("action", "")) == "share" and item.get("request_id")
    }
    request_ids = {
        str(metric.get("request_id", ""))
        for metric in metrics
        if metric.get("request_id")
    }

    summary = {
        "trace_path": args.trace_path,
        "actions_path": args.actions_path,
        "total_runs": len(rows),
        "runs_with_metrics": len(metrics),
        "total_actions": len(actions),
        "status_counts": dict(status_counter),
        "success_rate": average([float(m.get("success_flag", 0)) for m in metrics]),
        "no_solution_rate": average([float(m.get("no_solution_flag", 0)) for m in metrics]),
        "error_rate": average([float(m.get("error_flag", 0)) for m in metrics]),
        "avg_retry_count": average([float(m.get("retry_count", 0)) for m in metrics]),
        "avg_constraint_hit_rate": average([float(m.get("constraint_hit_rate", 0.0)) for m in metrics]),
        "avg_candidate_utilization": average([float(m.get("candidate_utilization", 0.0)) for m in metrics]),
        "avg_feasible_option_count": average([float(m.get("feasible_option_count", 0)) for m in metrics]),
        "avg_filtered_candidate_count": average(filtered_counts),
        "replan_rate": average(replan_flags),
        "preference_alignment_rate": average(
            [1.0 if bool(m.get("preference_alignment", False)) else 0.0 for m in metrics if m.get("recommendation_basis")]
        ),
        "failure_category_counts": dict(failure_counter),
        "primary_bottleneck_counts": dict(bottleneck_counter),
        "constraint_reason_counts": dict(reason_counter),
        "preference_profile_counts": dict(preference_counter),
        "replan_type_counts": dict(replan_counter),
        "recommendation_basis_counts": dict(basis_counter),
        "recommendation_tag_counts": dict(tag_counter),
        "action_counts": dict(action_counter),
        "share_rate": round(len(shared_requests & request_ids) / len(request_ids), 4) if request_ids else 0.0,
        "confirmation_rate": round(len(confirmed_requests & request_ids) / len(request_ids), 4) if request_ids else 0.0,
        "engagement_rate": round(len(requests_with_actions & request_ids) / len(request_ids), 4) if request_ids else 0.0,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
