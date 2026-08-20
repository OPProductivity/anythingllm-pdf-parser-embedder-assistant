import json
import sqlite3
from types import SimpleNamespace

import pytest
import run_control

from orchestration import (
    build_phase_timing_breakdown,
    compact_ready_run_control,
    execute_preparation,
    legacy_summary_from_run,
)
from anythingllm_state import resolve_state
from post_upload_polling import (
    MINIMUM_POLL_INTERVAL_SECONDS,
    PollingPolicy,
    operator_status,
    poll_post_upload,
)
from preflight import validate_planned_path
from run_control import RunRecorder, RunResult
from segmentation_policy import policy_for


pytestmark = pytest.mark.offline_deterministic


def test_run_recorder_persists_success_and_failure_evidence(tmp_path):
    result = RunResult("run-1", str(tmp_path), "page_limit", policy_for("page_limit").to_dict())
    recorder = RunRecorder(result)
    assert recorder.execute("compatibility_fingerprint", lambda: {"profile": "test"})["profile"] == "test"

    with pytest.raises(ValueError):
        recorder.execute("state_resolution", lambda: (_ for _ in ()).throw(ValueError("broken")))

    final = recorder.finish("error", "Expected test failure")
    checkpoint = json.loads((tmp_path / "run-checkpoint.json").read_text(encoding="utf-8"))

    assert final.stages["compatibility_fingerprint"].status == "success"
    assert final.stages["state_resolution"].status == "failed"
    assert checkpoint["status"] == "error"
    assert (tmp_path / "run-checkpoints.jsonl").exists()


def test_recovery_json_write_failure_preserves_last_valid_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / "run-checkpoint.json"
    run_control.atomic_write_json(path, {"status": "running"})
    monkeypatch.setattr(run_control.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        run_control.atomic_write_json(path, {"status": "finished"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "running"}
    assert not list(tmp_path.glob("*.tmp"))


def test_recovery_json_writer_retries_a_transient_windows_sharing_violation(tmp_path, monkeypatch):
    path = tmp_path / "run-checkpoint.json"
    original_replace = run_control.os.replace
    attempts = {"count": 0}

    def flaky_replace(source, target):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise PermissionError("temporary sharing violation")
        return original_replace(source, target)

    monkeypatch.setattr(run_control.os, "replace", flaky_replace)
    monkeypatch.setattr(run_control.time, "sleep", lambda _seconds: None)

    run_control.atomic_write_json(path, {"status": "finished"})

    assert attempts["count"] == 2
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "finished"}


def test_preflight_uses_4096_fallback_and_blocks_excessive_request():
    resolved = {
        "compatibility": {"capabilities": {}},
        "embedder": {},
        "anomalies": [],
    }
    result = validate_planned_path(
        resolved, "page_limit", target_length=750, requested_chunk_size=5000,
    )

    assert result.planned_hard_limit == 4096
    assert result.status == "error"
    assert any(row.code == "chunk_size_exceeds_operational_limit" for row in result.findings)


def test_preflight_uses_authoritative_embedder_hard_limit():
    resolved = {
        "compatibility": {"capabilities": {}},
        "embedder": {
            "hard_limit": {
                "effective": 1536,
                "effective_basis": "profile_defined",
            }
        },
        "anomalies": [],
    }
    result = validate_planned_path(
        resolved, "page_limit", target_length=750, requested_chunk_size=1600,
    )

    assert result.planned_hard_limit == 1536
    assert result.status == "error"
    assert any(row.code == "chunk_size_exceeds_operational_limit" for row in result.findings)


def test_preflight_allows_known_qwen_context_when_no_anythingllm_override():
    resolved = {
        "compatibility": {"capabilities": {}},
        "embedder": {
            "engine": {"effective": "openrouter"},
            "model": {"effective": "qwen/qwen3-embedding-8b"},
            "hard_limit": {
                "effective": 32768,
                "effective_basis": "catalog_capability",
            },
        },
        "anomalies": [],
    }
    result = validate_planned_path(
        resolved, "page", target_length=750, requested_chunk_size=8191,
    )

    assert result.planned_hard_limit == 32768
    assert result.status == "pass"


def test_state_resolver_uses_qwen_catalog_limit_without_anythingllm_override(tmp_path):
    (tmp_path / ".env").write_text(
        "EMBEDDING_ENGINE=openrouter\nEMBEDDING_MODEL_PREF=qwen/qwen3-embedding-8b\n",  # pragma: allowlist secret
        encoding="utf-8",
    )
    con = sqlite3.connect(tmp_path / "anythingllm.db")
    try:
        con.execute("create table system_settings (label text, value text)")
        con.commit()
    finally:
        con.close()

    resolved = resolve_state(tmp_path)
    hard_limit = resolved["embedder"]["hard_limit"]

    assert hard_limit["stored"] is None
    assert hard_limit["effective"] == 32768
    assert hard_limit["effective_basis"] == "catalog_capability"


def test_common_facade_names_chunk_limit_preflight_failure(tmp_path):
    args = SimpleNamespace(
        segment_mode="page_limit", target_passage_length=750, anythingllm_chunk_size=5000,
        anythingllm_storage_dir=str(tmp_path / "missing-storage"), prepare_and_upload=False,
        run_vector_eval=False,
    )

    result = execute_preparation(
        tmp_path / "input.pdf", tmp_path / "output", args,
        lambda *_args: pytest.fail("legacy engine must not run after a blocking preflight"),
    )

    assert result.status == "error"
    assert result.operator_summary.startswith("AUTO-CHUNK-LIMIT-001:")


def test_preflight_allows_explicit_no_local_segmentation_with_provenance_warning():
    resolved = {
        "compatibility": {"capabilities": {}},
        "embedder": {},
        "anomalies": [],
    }
    result = validate_planned_path(
        resolved, "none", target_length=640, requested_chunk_size=384,
    )

    assert result.status == "pass_with_review"
    finding = next(row for row in result.findings if row.code == "mode_not_page_local")
    assert finding.severity == "warning"


def test_preflight_allows_custom_page_ranges_with_range_provenance_warning():
    resolved = {
        "compatibility": {"capabilities": {}},
        "embedder": {},
        "anomalies": [],
    }
    result = validate_planned_path(
        resolved, "custom_page_ranges", target_length=640, requested_chunk_size=384,
    )

    assert result.status == "pass_with_review"
    finding = next(row for row in result.findings if row.code == "custom_range_page_group_provenance")
    assert finding.severity == "warning"
    assert "contiguous PDF page range" in finding.message


def test_preflight_checks_actual_upload_capabilities_and_runtime_probe():
    resolved = {
        "compatibility": {
            "capabilities": {
                "can_upload_native_metadata": {"status": "supported"},
                "can_create_temp_api_key": {"status": "unknown"},
            }
        },
        "embedder": {},
        "anomalies": [],
    }
    result = validate_planned_path(
        resolved,
        "page_limit",
        target_length=750,
        requested_chunk_size=768,
        prepare_upload=True,
        runtime_probe={"status": "pass"},
    )

    assert result.status == "error"
    assert "can_create_temp_api_key" in result.required_capabilities


def test_polling_accumulates_evidence_until_pass():
    rows = iter([
        {"status": "no_matching_native_docs"},
        {"status": "docs_without_vectors"},
        {"status": "pass"},
    ])
    clock = {"value": 0.0}

    def monotonic():
        return clock["value"]

    def sleeper(seconds):
        clock["value"] += seconds

    result = poll_post_upload(
        lambda: next(rows),
        interval_seconds=2,
        timeout_seconds=10,
        monotonic=monotonic,
        sleeper=sleeper,
    )

    assert result.status == "pass"
    assert result.attempts == 3
    assert len(result.observations) == 3


def test_polling_policy_clamps_timeout_and_sleep_cadence():
    policy = PollingPolicy.from_values(interval_seconds=-2, timeout_seconds=120, hard_cap_seconds=45)

    assert policy.interval_seconds == MINIMUM_POLL_INTERVAL_SECONDS
    assert policy.timeout_seconds == 45
    assert policy.hard_cap_seconds == 45

    clock = {"value": 0.0}
    sleeps = []

    def monotonic():
        return clock["value"]

    def sleeper(seconds):
        sleeps.append(seconds)
        clock["value"] += seconds

    result = poll_post_upload(
        lambda: {"status": "no_matching_native_docs"},
        interval_seconds=10,
        timeout_seconds=25,
        hard_cap_seconds=12,
        monotonic=monotonic,
        sleeper=sleeper,
    )

    assert result.status == "timeout"
    assert result.elapsed_seconds == 12
    assert sleeps == [10, 2]


def test_polling_zero_or_nonfinite_cadence_never_spins_tightly():
    policy = PollingPolicy.from_values(
        interval_seconds=0,
        timeout_seconds=float("inf"),
        hard_cap_seconds=float("nan"),
    )
    assert policy.interval_seconds == MINIMUM_POLL_INTERVAL_SECONDS
    assert policy.timeout_seconds == 60
    assert policy.hard_cap_seconds == 90

    clock = {"value": 0.0}
    sleeps = []

    def monotonic():
        return clock["value"]

    def sleeper(seconds):
        sleeps.append(seconds)
        clock["value"] += seconds

    result = poll_post_upload(
        lambda: {"status": "partial_vector_coverage"},
        interval_seconds=0,
        timeout_seconds=0.15,
        monotonic=monotonic,
        sleeper=sleeper,
        retryable_evidence_codes={"partial_vector_coverage"},
    )

    assert result.status == "timeout"
    assert sleeps == pytest.approx([MINIMUM_POLL_INTERVAL_SECONDS] * 2 + [0.05])


def test_polling_maps_technical_review_status_to_operator_status():
    result = poll_post_upload(
        lambda: {"status": "pass_with_missing_workspace_document_records"},
        interval_seconds=0,
        timeout_seconds=1,
    )

    assert result.status == "pass_with_review"
    assert operator_status({"status": "workspace_missing"}) == "error"
    assert operator_status({"status": "partial_vector_coverage"}) == "error"
    assert operator_status({"status": "verified_unavailable"}) == "pass_with_review"


def test_polling_can_treat_selected_error_evidence_as_transient():
    rows = iter([
        {"status": "partial_vector_coverage", "lancedb_matching_rows": 4},
        {"status": "pass", "lancedb_matching_rows": 5},
    ])
    clock = {"value": 0.0}

    def monotonic():
        return clock["value"]

    def sleeper(seconds):
        clock["value"] += seconds

    result = poll_post_upload(
        lambda: next(rows),
        interval_seconds=2,
        timeout_seconds=10,
        monotonic=monotonic,
        sleeper=sleeper,
        retryable_evidence_codes={"partial_vector_coverage"},
    )

    assert result.status == "pass"
    assert result.attempts == 2
    assert result.observations[0]["lancedb_matching_rows"] == 4


def test_polling_selected_transient_evidence_still_respects_timeout():
    clock = {"value": 0.0}

    def monotonic():
        return clock["value"]

    def sleeper(seconds):
        clock["value"] += seconds

    result = poll_post_upload(
        lambda: {"status": "partial_vector_coverage", "lancedb_matching_rows": 4},
        interval_seconds=2,
        timeout_seconds=5,
        monotonic=monotonic,
        sleeper=sleeper,
        retryable_evidence_codes={"partial_vector_coverage"},
    )

    assert result.status == "timeout"
    assert result.elapsed_seconds == 5
    assert result.attempts == 4


def test_polling_retains_success_when_observation_callback_fails():
    def broken_observer(_evidence, _status) -> None:
        raise RuntimeError("presentation callback failed")

    result = poll_post_upload(
        lambda: {"status": "pass"},
        interval_seconds=0,
        timeout_seconds=1,
        observation_callback=broken_observer,
    )

    assert result.status == "pass"
    assert result.attempts == 1
    assert result.observer_failures == [
        {
            "attempt": 1,
            "operator_status": "pass",
            "exception_type": "RuntimeError",
            "message": "presentation callback failed",
        }
    ]
    assert result.to_dict()["observer_failures"] == result.observer_failures


def test_polling_returns_durable_error_evidence_when_storage_inspector_raises():
    result = poll_post_upload(
        lambda: (_ for _ in ()).throw(OSError("LanceDB is temporarily unavailable")),
        interval_seconds=0,
        timeout_seconds=1,
    )

    assert result.status == "error"
    assert result.attempts == 1
    assert result.final_evidence["inspection_exception_type"] == "OSError"
    assert "temporarily unavailable" in result.final_evidence["inspection_error"]


def test_common_facade_returns_run_result_and_legacy_summary(tmp_path):
    args = SimpleNamespace(
        segment_mode="page_limit",
        target_passage_length=750,
        anythingllm_chunk_size=768,
        anythingllm_storage_dir=str(tmp_path / "missing-storage"),
        prepare_and_upload=False,
        run_vector_eval=False,
    )
    called = []

    def fake_prepare(pdf_path, output_root, received_args):
        called.append((pdf_path, output_root, received_args))
        return {"manifest": str(output_root / "segment-manifest.jsonl"), "readiness_status": "ready"}

    result = execute_preparation(tmp_path / "input.pdf", tmp_path / "output", args, fake_prepare)

    assert result.status == "pass"
    assert len(called) == 1
    assert result.selected_mode == "page_limit"
    assert result.stages["preflight"].status == "success"
    assert result.stages["legacy_preparation_engine"].status == "success"
    assert result.stages["segmentation"].status == "skipped"
    assert "Delegated to legacy_preparation_engine" in result.stages["segmentation"].operator_message
    assert (tmp_path / "output" / "run-result.json").exists()


def test_pipeline_timing_breakdown_keeps_nested_phases_non_additive_and_persists_it(tmp_path):
    args = SimpleNamespace(
        segment_mode="page_limit",
        target_passage_length=750,
        anythingllm_chunk_size=768,
        anythingllm_storage_dir=str(tmp_path / "missing-storage"),
        prepare_and_upload=False,
        run_vector_eval=False,
    )
    output = tmp_path / "output"

    def fake_prepare(_pdf_path, output_root, received_args):
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "run-summary.json").write_text(
            json.dumps({"readiness_status": "ready"}), encoding="utf-8"
        )
        received_args.timing_event_callback(
            "extraction_backend:pymupdf",
            {"timing_event": "phase_completed", "phase_elapsed_seconds": 2.5},
        )
        received_args.timing_event_callback(
            "anythingllm_configuration_resolution",
            {"timing_event": "phase_completed", "phase_elapsed_seconds": 0.5},
        )
        received_args.timing_event_callback(
            "anythingllm_native_queue",
            {
                "timing_event": "batch_completed",
                "records": 3,
                "batch_elapsed_seconds": 4.0,
                "submission_seconds": 0.6,
                "verification_seconds": 3.4,
            },
        )
        return {"readiness_status": "ready"}

    result = execute_preparation(tmp_path / "input.pdf", output, args, fake_prepare)

    timing = legacy_summary_from_run(result)["phase_timing"]
    assert timing["extraction_seconds"] == 2.5
    assert timing["phase_seconds"]["anythingllm_configuration_resolution"] == 0.5
    assert timing["desktop_queue"] == {
        "batches_completed": 1,
        "records_submitted": 3,
        "batch_elapsed_seconds": 4.0,
        "submission_seconds": 0.6,
        "verification_seconds": 3.4,
    }
    assert "may overlap" in timing["interpretation"]
    stored = json.loads((output / "run-summary.json").read_text(encoding="utf-8"))
    assert stored["phase_timing"] == timing


def test_pipeline_timing_breakdown_ignores_unusable_events():
    timing = build_phase_timing_breakdown(
        [
            None,
            {"stage": "", "timing_event": "phase_completed", "phase_elapsed_seconds": 2},
            {"stage": "no-duration", "timing_event": "phase_completed"},
            {"stage": "queue", "timing_event": "batch_completed", "records": -5},
        ]
    )

    assert timing["event_count"] == 2
    assert timing["phase_seconds"] == {}
    assert timing["desktop_queue"]["records_submitted"] == 0


def test_pipeline_timing_breakdown_uses_legacy_batch_requested_count():
    timing = build_phase_timing_breakdown(
        [
            {
                "stage": "anythingllm_native_queue",
                "timing_event": "batch_completed",
                "requested": 7,
                "batch_elapsed_seconds": 12.5,
            }
        ]
    )

    assert timing["desktop_queue"]["batches_completed"] == 1
    assert timing["desktop_queue"]["records_submitted"] == 7


def test_common_facade_records_degraded_anythingllm_evidence_without_losing_prepared_output(tmp_path):
    args = SimpleNamespace(
        segment_mode="page_limit",
        target_passage_length=750,
        anythingllm_chunk_size=768,
        anythingllm_storage_dir=str(tmp_path / "missing-storage"),
        prepare_and_upload=True,
        external_preflight_managed=True,
        run_vector_eval=False,
    )

    def fake_prepare(_pdf_path, _output_root, _received_args):
        return {
            "readiness_status": "ready",
            "api_upload_status": "complete",
            "api_uploaded": 2,
            "api_embedding_update_requested": 2,
            "api_embedding_update_accepted": 2,
            "post_upload_verification_status": "docs_without_vectors",
            "post_upload_classification": "raw_upload_present_not_embedded",
            "post_upload_matching_vectors": 0,
            "post_upload_expected_payloads": 2,
            "anythingllm_runtime_validation_status": "not_run_post_upload_incomplete",
        }

    result = execute_preparation(tmp_path / "input.pdf", tmp_path / "output", args, fake_prepare)

    assert result.status == "pass"
    assert result.stages["legacy_preparation_engine"].status == "degraded"
    assert result.upload_evidence["embedding_update_accepted"] == 2
    assert result.verification_evidence["matching_vectors"] == 0


def test_compact_ready_run_control_merges_recovery_and_removes_duplicate_state(tmp_path):
    summary_path = tmp_path / "run-summary.json"
    summary_path.write_text(json.dumps({"readiness_status": "ready", "recovery": {"prior": "kept"}}), encoding="utf-8")
    for name in ("run-checkpoint.json", "run-checkpoints.jsonl", "run-result.json"):
        (tmp_path / name).write_text("duplicate", encoding="utf-8")
    result = RunResult("run-compact", str(tmp_path), "page_limit", policy_for("page_limit").to_dict())
    result.started_at = "start"
    result.ended_at = "end"
    result.total_elapsed_seconds = 12.5
    result.status = "pass"

    compact_ready_run_control(tmp_path, result, {"readiness_status": "ready"})

    compact = json.loads(summary_path.read_text(encoding="utf-8"))
    assert compact["recovery"]["prior"] == "kept"
    assert compact["recovery"]["run_id"] == "run-compact"
    assert compact["recovery"]["resume_required"] is False
    assert not any((tmp_path / name).exists() for name in (
        "run-checkpoint.json", "run-checkpoints.jsonl", "run-result.json"
    ))


@pytest.mark.parametrize("summary_text", [None, "not json"])
def test_compact_ready_run_control_is_a_safe_noop_without_a_valid_summary(tmp_path, summary_text):
    if summary_text is not None:
        (tmp_path / "run-summary.json").write_text(summary_text, encoding="utf-8")
    duplicate = tmp_path / "run-result.json"
    duplicate.write_text("retain", encoding="utf-8")
    result = RunResult("run-noop", str(tmp_path), "page_limit", policy_for("page_limit").to_dict())

    compact_ready_run_control(tmp_path, result, {})

    assert duplicate.read_text(encoding="utf-8") == "retain"


def test_common_facade_blocks_invalid_intent_before_calling_the_legacy_engine(tmp_path):
    args = SimpleNamespace(
        segment_mode="page_limit", target_passage_length=0, anythingllm_chunk_size=0,
        anythingllm_storage_dir=str(tmp_path / "missing-storage"), prepare_and_upload=False,
        run_vector_eval=False,
    )

    result = execute_preparation(
        tmp_path / "input.pdf",
        tmp_path / "output",
        args,
        lambda *_args: pytest.fail("legacy engine must not run after a blocking preflight"),
    )

    assert result.status == "error"
    assert result.stages["pdf_extraction_normalization"].status == "skipped"
    assert "legacy_preparation_engine" not in result.stages


def test_common_facade_persists_a_failed_legacy_engine_stage(tmp_path):
    args = SimpleNamespace(
        segment_mode="page_limit", target_passage_length=750, anythingllm_chunk_size=0,
        anythingllm_storage_dir=str(tmp_path / "missing-storage"), prepare_and_upload=False,
        run_vector_eval=False,
    )

    result = execute_preparation(
        tmp_path / "input.pdf",
        tmp_path / "output",
        args,
        lambda *_args: (_ for _ in ()).throw(RuntimeError("extraction failed")),
    )

    assert result.status == "error"
    assert result.stages["legacy_preparation_engine"].status == "failed"
    assert legacy_summary_from_run(result) == {}
