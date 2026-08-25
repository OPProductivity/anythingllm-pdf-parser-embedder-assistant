"""Machine-readable regression evidence for the classic ETA architecture.

This module is deliberately read-only.  It does not learn from private timing
history and it does not alter the estimator.  It replays anonymous workload
classes through the production formula and asserts architectural properties
that should remain stable while reliability code changes around it.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import rag_pdf_gradio_app as app
from run_control import atomic_write_json


SCHEMA = "anythingllm_pdf_assistant_eta_regression_evidence_v1"


def _features(*, documents: int, pages: int, records: int, ocr: bool = False) -> dict[str, Any]:
    return {
        "mode": app.MODE_NATIVE_UPLOAD_LABEL,
        "document_count": documents,
        "page_count": pages,
        "estimated_records": records,
        "estimated_batches": documents,
        "estimated_embedding_provider_requests": records,
        "embedding_submission_strategy": "desktop_queue",
        "embedding_provider_request_seconds_prior": 3.0,
        "native_upload_scope": app.NATIVE_UPLOAD_SCOPE_ALL_LABEL,
        "native_upload_transport": "file_upload",
        "segment_mode": app.SEGMENT_PAGE_LIMIT_LABEL,
        "ocr_planned": ocr,
        "ocr_risk_bucket": "high" if ocr else "low",
        "ocr_preflight_likely_pages": pages if ocr else 0,
        "layout_bucket": "ordinary",
        "line_density_bucket": "ordinary",
        "page_variability_bucket": "stable",
    }


def build_eta_regression_evidence() -> dict[str, Any]:
    scenarios = {
        "single_text": _features(documents=1, pages=20, records=20),
        "medium_text": _features(documents=10, pages=200, records=200),
        "large_text": _features(documents=100, pages=2_000, records=2_000),
        "large_ocr": _features(documents=100, pages=2_000, records=2_000, ocr=True),
    }
    estimates = {
        name: int(round(app.timing_model_base_seconds(features)))
        for name, features in scenarios.items()
    }
    large_expected = estimates["large_text"]
    all_cached = app.confirmed_prequeue_cache_eta_seconds(
        large_expected,
        120,
        fresh_provider_requests=0,
        provider_request_seconds=3.0,
        features=scenarios["large_text"],
    )
    partially_cached = app.confirmed_prequeue_cache_eta_seconds(
        large_expected,
        120,
        fresh_provider_requests=200,
        provider_request_seconds=3.0,
        features=scenarios["large_text"],
    )
    queue_lower = app.bounded_queue_eta_reprice(1_000, 100)
    queue_upper = app.bounded_queue_eta_reprice(1_000, 5_000)
    unchanged_without_evidence = app.recalibrated_run_eta_seconds(
        1_000, 100, 100, 2, [20, 20],
    )
    checks = {
        "workload_scale_is_monotonic": (
            estimates["single_text"] < estimates["medium_text"] < estimates["large_text"]
        ),
        "ocr_reserve_does_not_reduce_estimate": estimates["large_ocr"] >= estimates["large_text"],
        "cache_realization_never_increases_current_eta": (
            0 < all_cached <= partially_cached <= large_expected
        ),
        "queue_repricing_is_bounded_per_observation": (
            0 < queue_lower < 1_000 < queue_upper
            and queue_lower >= int(1_000 * (1.0 - app.QUEUE_ETA_MAX_CHANGE_RATIO))
            and queue_upper <= int(1_000 * (1.0 + app.QUEUE_ETA_MAX_CHANGE_RATIO)) + 1
        ),
        "recalibration_waits_for_three_samples": unchanged_without_evidence == 1_000,
    }
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "pass" if all(checks.values()) else "fail",
        "formula_owner": "rag_pdf_gradio_app.timing_model_base_seconds",
        "private_history_used": False,
        "estimates_seconds": estimates,
        "cache_realization_seconds": {
            "all_cached": all_cached,
            "partially_cached": partially_cached,
            "unadjusted_large": large_expected,
        },
        "queue_repricing_seconds": {
            "starting": 1_000,
            "toward_lower_forecast": queue_lower,
            "toward_upper_forecast": queue_upper,
        },
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit anonymous classic-ETA regression evidence.")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    report = build_eta_regression_evidence()
    if args.output:
        atomic_write_json(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
