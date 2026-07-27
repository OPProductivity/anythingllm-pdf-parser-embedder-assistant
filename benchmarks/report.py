"""Generate privacy-safe operational and calibration benchmark aggregates."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from benchmarks.runner import (
    assert_public_payload_safe,
    calibration_eligible_public_result,
    read_json,
    write_json,
)


STAGES = ("preparation", "submission", "shared_ingestion", "validation", "reporting", "unattributed")


def load_runs(results_dir: Path) -> list[dict[str, Any]]:
    runs = []
    for path in sorted(results_dir.glob("B??-trial-[12].json")):
        try:
            row = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(row, dict) and row.get("document_id"):
            runs.append(row)
    return runs


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 3) if values else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def _minimum(values: list[float]) -> float | None:
    return round(min(values), 3) if values else None


def _maximum(values: list[float]) -> float | None:
    return round(max(values), 3) if values else None


def _sample_variance(values: list[float]) -> float | None:
    return round(statistics.variance(values), 3) if len(values) > 1 else None


def _quartiles(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    quartiles = statistics.quantiles(values, n=4, method="inclusive")
    return round(quartiles[0], 3), round(quartiles[2], 3)


def _stage_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    return {
        stage: {
            "mean_seconds": _mean([float((row.get("disjoint_wall_clock_seconds") or {}).get(stage) or 0.0) for row in rows]),
            "median_seconds": _median([float((row.get("disjoint_wall_clock_seconds") or {}).get(stage) or 0.0) for row in rows]),
            "mean_percent": _mean([float((row.get("disjoint_wall_clock_percent") or {}).get(stage) or 0.0) for row in rows]),
            "median_percent": _median([float((row.get("disjoint_wall_clock_percent") or {}).get(stage) or 0.0) for row in rows]),
        }
        for stage in STAGES
    }


def _trial_variance(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        pairs.setdefault(str(row.get("document_id") or ""), []).append(row)
    output = []
    for document_id, pair in sorted(pairs.items()):
        durations = [float(row.get("total_wall_seconds") or 0.0) for row in pair]
        output.append({
            "document_id": document_id,
            "trial_count": len(pair),
            "mean_seconds": _mean(durations),
            "min_seconds": _minimum(durations),
            "max_seconds": _maximum(durations),
            "sample_variance_seconds_squared": _sample_variance(durations),
            "absolute_trial_delta_seconds": round(abs(durations[0] - durations[1]), 3) if len(durations) == 2 else None,
        })
    return output


def _checkpoint_accuracy(rows: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    checkpoints = {}
    for checkpoint in ("20", "40", "60", "80"):
        progress_errors = [
            float((row.get("progress_calibration") or {}).get(checkpoint, {}).get("progress_error_points"))
            for row in rows
            if (row.get("progress_calibration") or {}).get(checkpoint, {}).get("status") == "recorded"
        ]
        eta_errors = [
            float((row.get("progress_calibration") or {}).get(checkpoint, {}).get("eta_error_seconds"))
            for row in rows
            if (row.get("progress_calibration") or {}).get(checkpoint, {}).get("eta_error_seconds") is not None
        ]
        checkpoints[checkpoint] = {
            "progress_error_mean_points": _mean(progress_errors),
            "progress_error_median_points": _median(progress_errors),
            "eta_error_mean_seconds": _mean(eta_errors),
            "eta_error_median_seconds": _median(eta_errors),
        }
    return checkpoints


def _analysis_view(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise one named population without silently mixing cohorts."""
    durations = [float(row.get("total_wall_seconds") or 0.0) for row in rows if float(row.get("total_wall_seconds") or 0.0) > 0]
    q1, q3 = _quartiles(durations)
    iqr = (q3 - q1) if q1 is not None and q3 is not None else None
    lower = q1 - 1.5 * iqr if iqr is not None else None
    upper = q3 + 1.5 * iqr if iqr is not None else None
    pages = [int(row.get("page_count") or 0) for row in rows if int(row.get("page_count") or 0) > 0]
    page_q1, page_q3 = _quartiles([float(page) for page in pages])
    page_groups = {
        "lower": [row for row in rows if page_q1 is not None and int(row.get("page_count") or 0) <= page_q1],
        "median": [row for row in rows if page_q1 is not None and page_q3 is not None and page_q1 < int(row.get("page_count") or 0) < page_q3],
        "upper": [row for row in rows if page_q3 is not None and int(row.get("page_count") or 0) >= page_q3],
    }
    table = []
    for row in rows:
        total = float(row.get("total_wall_seconds") or 0.0)
        table.append({
            "document_id": row.get("document_id"),
            "trial": row.get("trial"),
            "page_count": row.get("page_count"),
            "total_wall_seconds": total,
            "stage_seconds": row.get("disjoint_wall_clock_seconds") or {},
            "stage_percent": row.get("disjoint_wall_clock_percent") or {},
            "progress_calibration_passed": bool(row.get("progress_calibration_passed")),
            "outlier": bool(lower is not None and (total < lower or total > upper)),
        })
    queue_rates = [float(row.get("queue_rate_records_per_minute")) for row in rows if row.get("queue_rate_records_per_minute") is not None]
    return {
        "run_count": len(rows),
        "table": table,
        "total_duration": {"mean_seconds": _mean(durations), "median_seconds": _median(durations), "min_seconds": _minimum(durations), "max_seconds": _maximum(durations)},
        "stage_summary": _stage_summary(rows),
        "page_count_quartiles": {
            "lower_threshold": page_q1,
            "upper_threshold": page_q3,
            "groups": {
                name: {
                    "run_count": len(group),
                    "total_duration": {
                        "mean_seconds": _mean([float(row.get("total_wall_seconds") or 0.0) for row in group]),
                        "median_seconds": _median([float(row.get("total_wall_seconds") or 0.0) for row in group]),
                    },
                    "stage_share": _stage_summary(group),
                }
                for name, group in page_groups.items()
            },
        },
        "outlier_bounds_seconds": {"lower": round(lower, 3) if lower is not None else None, "upper": round(upper, 3) if upper is not None else None},
        "outliers": [{"document_id": row["document_id"], "trial": row["trial"], "total_wall_seconds": row["total_wall_seconds"]} for row in table if row["outlier"]],
        "trial_to_trial_variance": _trial_variance(rows),
        "checkpoint_accuracy": _checkpoint_accuracy(rows),
        "queue_rate_records_per_minute": {"mean": _mean(queue_rates), "median": _median(queue_rates)},
    }


def report_payload(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Return separate operational and strict calibration views."""
    operational_runs = [row for row in runs if str(row.get("status") or "").casefold() in {"successful", "warning"}]
    calibration_runs = [row for row in operational_runs if calibration_eligible_public_result(row)]
    calibration_passed = [row for row in calibration_runs if bool(row.get("progress_calibration_passed"))]
    return {
        "schema_version": 2,
        "run_count": len(runs),
        "data_quality": {
            "operational_completed_run_count": len(operational_runs),
            "calibration_eligible_run_count": len(calibration_runs),
            "calibration_excluded_run_count": len(operational_runs) - len(calibration_runs),
            "calibration_passed_count": len(calibration_passed),
            "calibration_failed_count": len(calibration_runs) - len(calibration_passed),
            "calibration_acceptance": (
                "pending" if not calibration_runs
                else "passed" if len(calibration_passed) == len(calibration_runs)
                else "failed"
            ),
            "note": "Operational metrics include all completed runs. Calibration metrics include only successful, environment-comparable, observer-healthy current-protocol runs.",
        },
        "operational": _analysis_view(operational_runs),
        "calibration": _analysis_view(calibration_runs),
    }


def report_markdown(payload: dict[str, Any]) -> str:
    quality = payload.get("data_quality") or {}
    operational = payload.get("operational") or {}
    calibration = payload.get("calibration") or {}
    duration = operational.get("total_duration") or {}
    lines = [
        "# Anonymised benchmark report", "", f"Measured runs: {payload['run_count']}",
        f"Operational completed runs: {quality.get('operational_completed_run_count', 0)}; calibration-eligible runs: {quality.get('calibration_eligible_run_count', 0)}; calibration acceptance: {quality.get('calibration_acceptance', 'unknown')}",
        "", "## Operational view", "", "Includes all completed runs, including warnings and observer uncertainty.",
        "", "| ID | Trial | Pages | Total s | Calibration | IQR outlier |", "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for row in operational.get("table") or []:
        lines.append(f"| {row['document_id']} | {row['trial']} | {row['page_count']} | {row['total_wall_seconds']:.3f} | {'pass' if row['progress_calibration_passed'] else 'fail'} | {'yes' if row['outlier'] else 'no'} |")
    lines.extend(["", f"Duration (s): min {duration.get('min_seconds')}, median {duration.get('median_seconds')}, mean {duration.get('mean_seconds')}, max {duration.get('max_seconds')}", "", "## Trial-to-trial variance", "", "| ID | Trials | Mean s | Min–max s | Variance s² |", "| --- | ---: | ---: | --- | ---: |"])
    for row in operational.get("trial_to_trial_variance") or []:
        lines.append(f"| {row['document_id']} | {row['trial_count']} | {row['mean_seconds']} | {row['min_seconds']}–{row['max_seconds']} | {row['sample_variance_seconds_squared']} |")
    lines.extend(["", "## Page-count groups (mean stage share)", "", "| Group | Runs | Preparation | Submission | Shared ingestion | Validation | Reporting |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for name, group in ((operational.get("page_count_quartiles") or {}).get("groups") or {}).items():
        stages = group.get("stage_share") or {}
        lines.append("| " + " | ".join([name, str(group.get("run_count", 0)), *[str((stages.get(stage) or {}).get("mean_percent")) for stage in STAGES[:-1]]]) + " |")
    calibration_duration = calibration.get("total_duration") or {}
    lines.extend(["", "## Calibration view", "", "Only successful, environment-comparable, observer-healthy runs from the current benchmark protocol may influence progress allocation.", f"Eligible duration (s): min {calibration_duration.get('min_seconds')}, median {calibration_duration.get('median_seconds')}, mean {calibration_duration.get('mean_seconds')}, max {calibration_duration.get('max_seconds')}"])
    return "\n".join(lines) + "\n"


def write_report(results_dir: Path) -> dict[str, Any]:
    payload = report_payload(load_runs(results_dir / "runs"))
    assert_public_payload_safe(payload)
    write_json(results_dir / "benchmark-report.json", payload)
    (results_dir / "benchmark-report.md").write_text(report_markdown(payload), encoding="utf-8")
    return payload
