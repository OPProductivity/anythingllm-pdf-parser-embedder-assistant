from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks import runner
from benchmarks import report


pytestmark = pytest.mark.offline_deterministic


def test_public_manifest_has_exactly_eight_anonymous_low_ocr_documents():
    documents = runner.load_manifest()

    assert sorted(documents) == [f"B{index:02d}" for index in range(1, 9)]
    assert all(document.ocr_risk == "low" for document in documents.values())


def test_public_result_rejects_private_paths_hashes_workspace_and_source_names():
    payload = {
        "document_id": "B01",
        "source_path": r"C:\\private\\source.pdf",
        "workspace_slug": "benchmark-private",
        "hash": "a" * 64,
        "message": "source.pdf",
    }

    violations = runner.public_payload_violations(payload, forbidden_values=["source.pdf"])

    assert "forbidden key: source_path" in violations
    assert "forbidden key: workspace_slug" in violations
    assert "absolute path" in violations
    assert "sha256-like value" in violations
    assert "private source value" in violations
    with pytest.raises(ValueError, match="unsafe public benchmark"):
        runner.assert_public_payload_safe(payload, forbidden_values=["source.pdf"])


def test_disjoint_wall_clock_attribution_sums_to_completed_duration_while_evidence_can_overlap():
    events = [
        {"recorded_monotonic": 101.0, "phase": "metadata", "value": 0.01},
        {"recorded_monotonic": 103.0, "phase": "queue_receipt", "value": 0.05},
        {"recorded_monotonic": 105.0, "phase": "desktop_queue", "value": 0.20},
        {"recorded_monotonic": 108.0, "phase": "identity_set", "value": 0.45},
        {"recorded_monotonic": 112.0, "phase": "validation", "value": 0.98},
        {"recorded_monotonic": 114.0, "phase": "reporting", "value": 0.99},
    ]

    attribution = runner.disjoint_wall_clock_attribution(events, 100.0, 116.0)
    evidence = runner.overlapping_evidence_spans(events)

    assert round(sum(attribution.values()), 3) == 16.0
    assert attribution["shared_ingestion"] == 7.0
    assert evidence["owned_queue"]["wall_seconds"] == 0.0
    assert evidence["confirmed_vectors"]["wall_seconds"] == 0.0


def test_retrospective_calibration_uses_final_completed_duration_not_own_eta():
    events = [
        {"recorded_monotonic": 120.0, "value": 0.20},
        {"recorded_monotonic": 140.0, "value": 0.40},
        {"recorded_monotonic": 160.0, "value": 0.60},
        {"recorded_monotonic": 180.0, "value": 0.80},
    ]

    calibration = runner.retrospective_calibration(events, 100.0, 200.0)

    assert calibration["20"]["progress_error_points"] == 0.0
    assert calibration["80"]["progress_error_points"] == 0.0
    assert runner.progress_calibration_passes(calibration)
    events[3]["value"] = 0.99
    assert not runner.progress_calibration_passes(runner.retrospective_calibration(events, 100.0, 200.0))


def test_trace_calibration_uses_visible_progress_and_records_eta_separately():
    rows = [
        {"elapsed_seconds": 20, "visible_progress_percent": 20, "expected_seconds": 120},
        {"elapsed_seconds": 40, "visible_progress_percent": 40, "expected_seconds": 120},
        {"elapsed_seconds": 60, "visible_progress_percent": 60, "expected_seconds": 120},
        {"elapsed_seconds": 80, "visible_progress_percent": 80, "expected_seconds": 120},
        {"elapsed_seconds": 100, "visible_progress_percent": 100, "expected_seconds": 120},
    ]

    calibration = runner.retrospective_trace_calibration(rows)

    assert calibration["40"]["progress_error_points"] == 0.0
    assert calibration["40"]["eta_error_seconds"] == 20.0


def test_trace_calibration_allows_ten_points_only_until_next_queue_vector_evidence():
    rows = [
        {"elapsed_seconds": 10, "visible_progress_percent": 10, "expected_seconds": 100, "state": "running"},
        {"elapsed_seconds": 19, "visible_progress_percent": 19, "expected_seconds": 72, "eta_reprice_reason": "owned_queue_rate", "state": "running"},
        {"elapsed_seconds": 20, "visible_progress_percent": 26, "expected_seconds": 72, "state": "running"},
        {"elapsed_seconds": 21, "visible_progress_percent": 27, "expected_seconds": 72, "progress_phase": "desktop_queue", "completed_units": 5, "total_units": 10, "state": "running"},
        {"elapsed_seconds": 40, "visible_progress_percent": 40, "expected_seconds": 72, "state": "running"},
        {"elapsed_seconds": 60, "visible_progress_percent": 60, "expected_seconds": 72, "state": "running"},
        {"elapsed_seconds": 80, "visible_progress_percent": 80, "expected_seconds": 72, "state": "running"},
        {"elapsed_seconds": 100, "visible_progress_percent": 100, "expected_seconds": 72, "state": "successful"},
    ]

    calibration = runner.retrospective_trace_calibration(rows)

    assert calibration["20"]["allowed_error_points"] == 10.0
    assert calibration["20"]["temporary_reprice_allowance"] is True
    assert calibration["40"]["allowed_error_points"] == 5.0
    assert runner.progress_calibration_passes(calibration)
    rows[2]["visible_progress_percent"] = 31
    assert not runner.progress_calibration_passes(runner.retrospective_trace_calibration(rows))


def test_benchmark_ui_timer_samples_the_same_paced_value_as_localhost_status(tmp_path: Path, monkeypatch):
    started = 1_000.0
    run_root = tmp_path / "private-run"
    samples = tmp_path / "ui-timer-presentation.jsonl"
    record = {
        "run_root": str(run_root),
        "started_epoch": started,
        "state": "running",
        "phase": "Waiting for confirmed vectors",
        "progress_phase": "identity_set",
        "completed_units": 4,
        "total_units": 10,
        "confirmed_fraction": 0.4,
        "phase_started_epoch": started,
        "phase_start_fraction": 0.4,
        "phase_allowance": 0.04,
        "phase_budget_seconds": 15,
        "display_anchor_fraction": 0.4,
        "display_anchor_epoch": started,
        "display_target_fraction": 0.4,
        "expected_seconds": 100,
    }
    monkeypatch.setattr(runner.app, "LIVE_AUTOMATIC_RUN_STATUS", record)
    monkeypatch.setattr(runner.time, "time", lambda: 1_010.0)

    sampler = runner.ProductionUiTimerSampler(samples, run_root=run_root, started_epoch=started)
    sampler._snapshot()
    rows = runner.read_progress_trace(samples)

    assert len(rows) == 1
    assert rows[0]["presentation_source"] == "ui_timer"
    assert rows[0]["visible_progress_percent"] == runner.app.paced_progress_percent(record, 1_010.0)
    # The merged timeline must prefer the actual timer's earlier visible
    # checkpoint, rather than pretending the callback cadence was the UI.
    merged = runner.merge_presentation_rows(
        [{"elapsed_seconds": 12, "visible_progress_percent": 25, "state": "running"}],
        rows,
    )
    assert merged[0]["presentation_source"] == "ui_timer"


def test_status_is_awaiting_rerun_until_all_production_route_trials_are_eligible():
    assert runner.benchmark_status_state([("B01", 1)], 0) == "awaiting-rerun"
    assert runner.benchmark_status_state([], 1) == "calibration-failed"
    assert runner.benchmark_status_state([], 0) == "completed"


def test_terminal_jump_cannot_fake_an_intermediate_calibration_checkpoint():
    calibration = runner.retrospective_trace_calibration([
        {"elapsed_seconds": 5, "visible_progress_percent": 10, "expected_seconds": 30, "state": "running"},
        {"elapsed_seconds": 30, "visible_progress_percent": 100, "expected_seconds": 30, "state": "successful"},
    ])

    assert calibration["20"]["status"] == "terminal_only"
    assert not runner.progress_calibration_passes(calibration)


def test_private_source_map_never_contributes_identity_to_public_manifest(tmp_path: Path):
    source = tmp_path / "private.pdf"
    source.write_bytes(b"%PDF-private")
    private_map = tmp_path / "private-map.json"
    private_map.write_text(json.dumps({"documents": [{"document_id": "B01", "path": str(source), "fingerprint": "secret"}]}), encoding="utf-8")

    loaded = runner.load_private_source_map(private_map)

    assert loaded["B01"]["path"] == source
    assert "private.pdf" not in runner.PUBLIC_MANIFEST.read_text(encoding="utf-8")


def test_private_fingerprint_is_computed_locally_not_needed_by_public_manifest(tmp_path: Path):
    source = tmp_path / "private.pdf"
    source.write_bytes(b"benchmark bytes")

    assert runner.sha256_file(source) == runner.sha256_file(source)
    assert runner.sha256_file(source) not in runner.PUBLIC_MANIFEST.read_text(encoding="utf-8")


def test_warm_up_is_private_and_does_not_create_a_public_trial(tmp_path: Path, monkeypatch):
    document = runner.BenchmarkDocument("B01", 1, 0.1, "low")
    private_root = tmp_path / "private"
    status_path = tmp_path / "results" / "benchmark-status.json"
    monkeypatch.setattr(runner, "load_manifest", lambda: {"B01": document})
    monkeypatch.setattr(runner, "load_private_source_map", lambda _path: {"B01": {"path": "private.pdf"}})
    monkeypatch.setattr(runner, "queue_guard", lambda _url: {"status": "idle"})
    monkeypatch.setattr(
        runner,
        "run_one",
        lambda *_args, **_kwargs: ({"document_id": "B01"}, {"source_path": "private.pdf"}),
    )

    assert runner.main([
        "--private-map", str(tmp_path / "map.json"), "--document-id", "B01", "--warm-up",
        "--private-root", str(private_root), "--status-path", str(status_path),
    ]) == 0

    assert len(list((private_root / "warm-ups").glob("B01-*.json"))) == 1
    assert not status_path.exists()
    assert not (status_path.parent / "runs" / "B01-trial-1.json").exists()


def test_rerun_archives_an_invalid_public_trial_without_exposing_private_data(tmp_path: Path):
    result_path = tmp_path / "results" / "runs" / "B01-trial-1.json"
    runner.write_json(result_path, {
        "document_id": "B01", "trial": 1, "invalid_for_calibration": True,
        "invalid_reasons": ["observer_uncertainty"], "page_count": 25,
    })

    archived = runner.archive_invalid_public_result(result_path)

    assert archived == tmp_path / "results" / "invalid" / "B01-trial-1-attempt-1.json"
    assert runner.read_json(archived)["invalid_reasons"] == ["observer_uncertainty"]


def test_queue_guard_blocks_non_owned_activity_without_attempting_queue_cleanup(monkeypatch):
    monkeypatch.setattr(runner.app, "anythingllm_observer_api_health", lambda _url: {"reachable": True})
    monkeypatch.setattr(runner.app, "local_workspace_choices", lambda: ([("one", "one"), ("two", "two")], ""))
    calls = []

    def observe(_url, _key, slug, _owned, **_kwargs):
        calls.append(slug)
        return {"stream_connected": True, "non_owned_event_count": 1 if slug == "two" else 0}

    monkeypatch.setattr(runner.pipeline, "observe_workspace_embedding_queue_activity", observe)

    result = runner.queue_guard("http://127.0.0.1:3001")

    assert result["status"] == "active"
    assert sorted(calls) == ["one", "two"]
    assert "clear" not in result["reason"].casefold()


def test_report_keeps_anonymous_stage_table_and_checkpoint_metrics(tmp_path: Path):
    results = tmp_path / "results" / "runs"
    results.mkdir(parents=True)
    for trial, duration in ((1, 40.0), (2, 44.0)):
        (results / f"B01-trial-{trial}.json").write_text(json.dumps({
            "document_id": "B01", "trial": trial, "page_count": 25, "total_wall_seconds": duration,
            "disjoint_wall_clock_seconds": {stage: duration / 6 for stage in report.STAGES},
            "disjoint_wall_clock_percent": {stage: 100 / 6 for stage in report.STAGES},
            "progress_calibration_passed": False,
            "queue_rate_records_per_minute": 12,
            "progress_calibration": {str(point): {"status": "recorded", "progress_error_points": 6, "eta_error_seconds": 10} for point in (20, 40, 60, 80)},
        }), encoding="utf-8")

    payload = report.write_report(tmp_path / "results")

    assert payload["run_count"] == 2
    assert payload["total_duration"]["median_seconds"] == 42.0
    assert payload["total_duration"]["min_seconds"] == 40.0
    assert payload["total_duration"]["max_seconds"] == 44.0
    assert payload["checkpoint_accuracy"]["20"]["eta_error_mean_seconds"] == 10.0
    assert payload["queue_rate_records_per_minute"]["mean"] == 12.0
    assert payload["data_quality"]["timing_valid_run_count"] == 2
    assert payload["data_quality"]["calibration_acceptance"] == "failed"
    assert payload["trial_to_trial_variance"][0]["sample_variance_seconds_squared"] == 8.0
    assert "groups" in payload["page_count_quartiles"]
    assert (tmp_path / "results" / "benchmark-report.md").is_file()


def test_benchmark_uses_the_production_gradio_handler_and_records_its_reprices():
    source = Path(runner.__file__).read_text(encoding="utf-8")

    assert "app.run_automatic(" in source
    assert "execute_automatic_preparation_in_worker(" not in source
    assert runner.production_reprices([
        {"elapsed_seconds": 0, "expected_seconds": 100},
        {"elapsed_seconds": 12, "expected_seconds": 115},
        {"elapsed_seconds": 20, "expected_seconds": 115},
    ]) == [{"reason": "production_eta_reprice", "elapsed_seconds": 12.0, "expected_seconds": 115}]


def test_reconnecting_queue_observer_is_retained_but_excluded_from_calibration():
    assert runner.observer_uncertainty_reasons([
        {"type": "timing", "batch_report": {"desktop_queue_observer_state": "connected"}},
        {"type": "timing", "batch_report": {"desktop_queue_observer_state": "reconnecting"}},
    ]) == ["observer_uncertainty"]

    result = {"invalid_for_calibration": False, "invalid_reasons": [], "overlapping_evidence": {"reconnecting": {"wall_seconds": 0.0}}}
    assert runner.refresh_public_calibration_eligibility(result)
    assert result["invalid_reasons"] == ["observer_uncertainty"]
    assert runner.trace_observer_uncertainty_reasons([
        {"phase": "SSE observer reconnecting; waiting for owned queue evidence"},
    ]) == ["observer_uncertainty"]
