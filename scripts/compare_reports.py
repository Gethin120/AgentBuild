from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _flatten_metrics(data: Dict[str, Any]) -> Dict[str, float]:
    metrics: Dict[str, float] = {}

    if "pass_rate" in data:
        metrics["pass_rate"] = float(data.get("pass_rate", 0.0) or 0.0)
        metrics["avg_feasible_option_count"] = float(data.get("avg_feasible_option_count", 0.0) or 0.0)

    checks = data.get("checks", [])
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict):
                continue
            name = str(check.get("name", "") or "")
            summary = check.get("summary")
            if name == "trace_summary" and isinstance(summary, dict):
                for key in [
                    "success_rate",
                    "no_solution_rate",
                    "error_rate",
                    "avg_retry_count",
                    "avg_constraint_hit_rate",
                    "avg_candidate_utilization",
                    "avg_feasible_option_count",
                    "avg_filtered_candidate_count",
                    "replan_rate",
                    "preference_alignment_rate",
                    "share_rate",
                    "confirmation_rate",
                    "engagement_rate",
                ]:
                    metrics[key] = float(summary.get(key, 0.0) or 0.0)
    return metrics


def _counter_fields(data: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    counters: Dict[str, Dict[str, int]] = {}
    direct_counter_keys = [
        "status_counts",
        "failure_category_counts",
        "primary_bottleneck_counts",
        "recommendation_basis_counts",
        "preference_profile_counts",
        "replan_type_counts",
    ]
    for key in direct_counter_keys:
        value = data.get(key)
        if isinstance(value, dict):
            counters[key] = {str(k): int(v) for k, v in value.items()}

    checks = data.get("checks", [])
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict):
                continue
            name = str(check.get("name", "") or "")
            summary = check.get("summary")
            if name == "trace_summary" and isinstance(summary, dict):
                for key in [
                    "status_counts",
                    "failure_category_counts",
                    "primary_bottleneck_counts",
                    "constraint_reason_counts",
                    "recommendation_basis_counts",
                    "recommendation_tag_counts",
                    "preference_profile_counts",
                    "replan_type_counts",
                    "action_counts",
                ]:
                    value = summary.get(key)
                    if isinstance(value, dict):
                        counters[key] = {str(k): int(v) for k, v in value.items()}
    return counters


def _diff_counters(
    baseline: Dict[str, Dict[str, int]],
    current: Dict[str, Dict[str, int]],
) -> Dict[str, Dict[str, int]]:
    result: Dict[str, Dict[str, int]] = {}
    counter_names = sorted(set(baseline) | set(current))
    for counter_name in counter_names:
        before = baseline.get(counter_name, {})
        after = current.get(counter_name, {})
        keys = sorted(set(before) | set(after))
        diff = {
            key: int(after.get(key, 0)) - int(before.get(key, 0))
            for key in keys
            if int(after.get(key, 0)) - int(before.get(key, 0)) != 0
        }
        if diff:
            result[counter_name] = diff
    return result


def compare_reports(baseline: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    before_metrics = _flatten_metrics(baseline)
    after_metrics = _flatten_metrics(current)
    metric_names = sorted(set(before_metrics) | set(after_metrics))
    metric_deltas = {
        name: round(after_metrics.get(name, 0.0) - before_metrics.get(name, 0.0), 4)
        for name in metric_names
    }

    return {
        "baseline_metrics": before_metrics,
        "current_metrics": after_metrics,
        "metric_deltas": metric_deltas,
        "counter_deltas": _diff_counters(_counter_fields(baseline), _counter_fields(current)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two replay/check reports.")
    parser.add_argument("--baseline-path", required=True)
    parser.add_argument("--current-path", required=True)
    parser.add_argument("--output-path")
    args = parser.parse_args()

    baseline = load_json(Path(args.baseline_path))
    current = load_json(Path(args.current_path))
    comparison = compare_reports(baseline, current)
    output = json.dumps(comparison, ensure_ascii=False, indent=2)
    if args.output_path:
        Path(args.output_path).write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
