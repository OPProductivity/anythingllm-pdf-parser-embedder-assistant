"""Generate privacy-safe aggregates from anonymised benchmark result files."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from benchmarks.runner import assert_public_payload_safe, read_json, write_json


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
    summary = {}
    for stage in STAGES:
        values = [float((row.get("disjoint_wall_clock_seconds") or {}).get(stage) or 0.0) for row in rows]
        shares = [float((row.get("disjoint_wall_clock_percent") or {}).get(stage) or 0.0) for row in rows]
        summary[stage] = {
            "mean_seconds": _mean(values),
            "median_seconds": _median(values),
            "mean_percent": _mean(shares),
            "median_percent": _median(shares),
        }
    return summary


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


def report_payload(runs: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(row.get("total_wall_seconds") or 0.0) for row in runs if float(row.get("total_wall_seconds") or 0.0) > 0]
    q1, q3 = _quartiles(durations)
    iqr = (q3 - q1) if q1 is not None and q3 is not None else None
    lower = q1 - 1.5 * iqr if iqr is not None else None
    upper = q3 + 1.5 * iqr if iqr is not None else None
    stage_summary = _stage_summary(runs)
    checkpoints = {}
    for checkpoint in ("20", "40", "60", "80"):
        progress_errors = [
            float((row.get("progress_calibration") or {}).get(checkpoint, {}).get("progress_error_points"))
            for row in runs
            if (row.get("progress_calibration") or {}).get(checkpoint, {}).get("status") == "recorded"
        ]
        eta_errors = [
            float((row.get("progress_calibration") or {}).get(checkpoint, {}).get("eta_error_seconds"))
            for row in runs
            if (row.get("progress_calibration") or {}).get(checkpoint, {}).get("eta_error_seconds") is not None
        ]
        checkpoints[checkpoint] = {
            "progress_error_mean_points": _mean(progress_errors),
            "progress_error_median_points": _median(progress_errors),
            "eta_error_mean_seconds": _mean(eta_errors),
            "eta_error_median_seconds": _median(eta_errors),
        }
    pages = [int(row.get("page_count") or 0) for row in runs if int(row.get("page_count") or 0) > 0]
    queue_rates = [
        float(row.get("queue_rate_records_per_minute"))
        for row in runs if row.get("queue_rate_records_per_minute") is not None
    ]
    page_q1, page_q3 = _quartiles([float(page) for page in pages])
    page_groups = {
        "lower": [row for row in runs if page_q1 is not None and int(row.get("page_count") or 0) <= page_q1],
        "median": [row for row in runs if page_q1 is not None and page_q3 is not None and page_q1 < int(row.get("page_count") or 0) < page_q3],
        "upper": [row for row in runs if page_q3 is not None and int(row.get("page_count") or 0) >= page_q3],
    }
    table = []
    for row in runs:
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
    outliers = [
        {"document_id": row["document_id"], "trial": row["trial"], "total_wall_seconds": row["total_wall_seconds"]}
        for row in table if row["outlier"]
    ]
    timing_valid = [row for row in runs if not bool(row.get("invalid_for_calibration"))]
    calibration_passed = [row for row in timing_valid if bool(row.get("progress_calibration_passed"))]
    return {
        "schema_version": 1,
        "run_count": len(runs),
        "table": table,
        "data_quality": {
            "timing_valid_run_count": len(timing_valid),
            "timing_invalid_run_count": len(runs) - len(timing_valid),
            "calibration_passed_count": len(calibration_passed),
            "calibration_failed_count": len(timing_valid) - len(calibration_passed),
            "calibration_acceptance": "passed" if timing_valid and len(calibration_passed) == len(timing_valid) else "failed",
            "note": "Timing validity and progress-calibration acceptance are separate: a timing-valid run may fail calibration.",
        },
        "total_duration": {
            "mean_seconds": _mean(durations), "median_seconds": _median(durations),
            "min_seconds": _minimum(durations), "max_seconds": _maximum(durations),
        },
        "stage_summary": stage_summary,
        "page_count_quartiles": {
            "lower_threshold": page_q1,
            "upper_threshold": page_q3,
            "groups": {
                name: {
                    "run_count": len(rows),
                    "total_duration": {
                        "mean_seconds": _mean([float(row.get("total_wall_seconds") or 0.0) for row in rows]),
                        "median_seconds": _median([float(row.get("total_wall_seconds") or 0.0) for row in rows]),
                    },
                    "stage_share": _stage_summary(rows),
                }
                for name, rows in page_groups.items()
            },
        },
        "outlier_bounds_seconds": {"lower": round(lower, 3) if lower is not None else None, "upper": round(upper, 3) if upper is not None else None},
        "outliers": outliers,
        "trial_to_trial_variance": _trial_variance(runs),
        "checkpoint_accuracy": checkpoints,
        "queue_rate_records_per_minute": {"mean": _mean(queue_rates), "median": _median(queue_rates)},
    }


def report_markdown(payload: dict[str, Any]) -> str:
    quality = payload.get("data_quality") or {}
    duration = payload.get("total_duration") or {}
    lines = [
        "# Anonymised benchmark report", "", f"Measured runs: {payload['run_count']}",
        f"Timing-valid runs: {quality.get('timing_valid_run_count', 0)}; calibration acceptance: {quality.get('calibration_acceptance', 'unknown')}",
        "", "| ID | Trial | Pages | Total s | Calibration | IQR outlier |", "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for row in payload.get("table") or []:
        lines.append(f"| {row['document_id']} | {row['trial']} | {row['page_count']} | {row['total_wall_seconds']:.3f} | {'pass' if row['progress_calibration_passed'] else 'fail'} | {'yes' if row['outlier'] else 'no'} |")
    lines.extend([
        "", f"Duration (s): min {duration.get('min_seconds')}, median {duration.get('median_seconds')}, mean {duration.get('mean_seconds')}, max {duration.get('max_seconds')}",
        "", "## Trial-to-trial variance", "", "| ID | Trials | Mean s | Min–max s | Variance s² |", "| --- | ---: | ---: | --- | ---: |",
    ])
    for row in payload.get("trial_to_trial_variance") or []:
        lines.append(f"| {row['document_id']} | {row['trial_count']} | {row['mean_seconds']} | {row['min_seconds']}–{row['max_seconds']} | {row['sample_variance_seconds_squared']} |")
    lines.extend(["", "## Page-count groups (mean stage share)", "", "| Group | Runs | Preparation | Submission | Shared ingestion | Validation | Reporting |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for name, group in ((payload.get("page_count_quartiles") or {}).get("groups") or {}).items():
        stages = group.get("stage_share") or {}
        lines.append("| " + " | ".join([name, str(group.get("run_count", 0)), *[str((stages.get(stage) or {}).get("mean_percent")) for stage in STAGES[:-1]]]) + " |")
    return "\n".join(lines) + "\n"


def write_report(results_dir: Path) -> dict[str, Any]:
    payload = report_payload(load_runs(results_dir / "runs"))
    assert_public_payload_safe(payload)
    write_json(results_dir / "benchmark-report.json", payload)
    (results_dir / "benchmark-report.md").write_text(report_markdown(payload), encoding="utf-8")
    return payload
