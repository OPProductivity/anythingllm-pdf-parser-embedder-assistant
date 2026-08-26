import json

import pytest

import reliability_release as release


pytestmark = pytest.mark.offline_deterministic


def _compatibility():
    return {
        "desktop_version_normalized": "1.16.0",
        "desktop_release_status": "recognized_mutation_profile",
        "matched_profile": "profile-1",
        "native_mutation_contract": "contract-1",
        "storage_schema_status": "matched",
        "desktop_package": {"app_asar_sha256": "a" * 64},
        "capabilities": {
            "can_upload_native_metadata": {"status": "supported"},
            "can_poll_post_upload_state": {"status": "supported"},
        },
    }


def test_release_certificate_requires_clean_source_and_valid_rollback(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("test-version", encoding="utf-8")
    monkeypatch.setattr(release, "characterize", lambda *_args, **_kwargs: _compatibility())
    monkeypatch.setattr(
        release,
        "run_offline_crash_acceptance",
        lambda _root: {"status": "pass", "scenario_count": 13},
    )
    monkeypatch.setattr(
        release,
        "run_transport_fault_acceptance",
        lambda _root: {
            "status": "pass",
            "scenario_count": 5,
            "scope": "loopback_transport_recovery_and_production_classifier",
            "production_classifier_checks": {"422": True, "500": True},
        },
    )
    monkeypatch.setattr(
        release,
        "_git_state",
        lambda _repo, _rollback: {
            "head": "1" * 40,
            "worktree_clean": True,
            "changed_entry_count": 0,
            "rollback_ref_supplied": True,
            "rollback_commit": "0" * 40,
            "rollback_valid": True,
        },
    )
    output = tmp_path / "certificate.json"
    canary = tmp_path / "canary.json"
    canary.write_text(json.dumps({
        "schema": "anythingllm_pdf_assistant_grouped_live_canary_v1",
        "status": "pass",
        "integrity_audit": "pass",
        "ambiguous_mutation": False,
        "selected_pdf_count": 10,
        "batch_scale": "medium",
        "workspace_retained": False,
        "document_folder_cleanup_status": "deleted",
    }), encoding="utf-8")
    ui_junit = tmp_path / "ui.xml"
    ui_junit.write_text(
        '<testsuites tests="17" failures="0" errors="0">'
        '<testsuite tests="17" failures="0" errors="0" />'
        '</testsuites>',
        encoding="utf-8",
    )
    default_junit = tmp_path / "default.xml"
    default_junit.write_text(
        '<testsuites tests="843" failures="0" errors="0" skipped="17">'
        '<testsuite tests="843" failures="0" errors="0" skipped="17" />'
        '</testsuites>',
        encoding="utf-8",
    )
    scale = tmp_path / "scale.json"
    scale.write_text(json.dumps({
        "schema": "anythingllm_pdf_assistant_scale_acceptance_v1",
        "status": "pass", "source_count": 1000, "artifact_count": 3000,
        "external_mutation_attempted": False,
        "scope": "prepared_checkpoint_durability_only", "checks": {
            "all_sources_checkpointed": True,
            "all_sources_reloadable": True,
            "single_changed_artifact_blocks_reuse": True,
            "restored_artifact_revalidates": True,
            "submission_started_never_replays": True,
        },
    }), encoding="utf-8")
    eta = tmp_path / "eta.json"
    eta.write_text(json.dumps({
        "schema": "anythingllm_pdf_assistant_eta_regression_evidence_v1",
        "status": "pass", "private_history_used": False,
        "checks": {
            "workload_scale_is_monotonic": True,
            "ocr_reserve_does_not_reduce_estimate": True,
            "cache_realization_never_increases_current_eta": True,
            "queue_repricing_is_bounded_per_observation": True,
            "recalibration_waits_for_three_samples": True,
        },
    }), encoding="utf-8")

    result = release.certify_release(
        tmp_path,
        rollback_ref="previous-good",
        output_path=output,
        live_canary_path=canary,
        scale_report_path=scale,
        eta_report_path=eta,
        default_junit_path=default_junit,
        ui_junit_path=ui_junit,
    )

    assert result["status"] == "pass"
    assert all(result["checks"].values())
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "pass"


def test_release_certificate_blocks_without_live_and_browser_evidence(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("test-version", encoding="utf-8")
    monkeypatch.setattr(release, "characterize", lambda *_args, **_kwargs: _compatibility())
    monkeypatch.setattr(
        release, "run_offline_crash_acceptance",
        lambda _root: {"status": "pass", "scenario_count": 13},
    )
    monkeypatch.setattr(
        release, "run_transport_fault_acceptance",
        lambda _root: {
            "status": "pass",
            "scenario_count": 5,
            "scope": "loopback_transport_recovery_and_production_classifier",
            "production_classifier_checks": {"422": True, "500": True},
        },
    )
    monkeypatch.setattr(
        release, "_git_state",
        lambda _repo, _rollback: {
            "head": "1" * 40, "worktree_clean": True, "changed_entry_count": 0,
            "rollback_ref_supplied": True, "rollback_commit": "0" * 40,
            "rollback_valid": True,
        },
    )

    result = release.certify_release(tmp_path, rollback_ref="previous-good")

    assert result["status"] == "blocked"
    assert result["checks"]["disposable_live_canary"] is False
    assert result["checks"]["prepared_checkpoint_scale_acceptance"] is False
    assert result["checks"]["eta_regression_evidence"] is False
    assert result["checks"]["default_python_suite"] is False
    assert result["checks"]["browser_ui_acceptance"] is False


def test_live_release_gate_rejects_a_small_canary_even_when_it_passed(tmp_path):
    canary = tmp_path / "small-canary.json"
    canary.write_text(json.dumps({
        "schema": "anythingllm_pdf_assistant_grouped_live_canary_v1",
        "status": "pass",
        "integrity_audit": "pass",
        "ambiguous_mutation": False,
        "selected_pdf_count": 8,
        "batch_scale": "small",
        "workspace_retained": False,
        "document_folder_cleanup_status": "deleted",
    }), encoding="utf-8")

    assert release._live_canary_passed(canary) is False


def test_default_suite_gate_rejects_focused_or_failing_junit(tmp_path):
    focused = tmp_path / "focused.xml"
    focused.write_text(
        '<testsuite tests="20" failures="0" errors="0" skipped="0" />',
        encoding="utf-8",
    )
    failing = tmp_path / "failing.xml"
    failing.write_text(
        '<testsuite tests="843" failures="1" errors="0" skipped="17" />',
        encoding="utf-8",
    )

    assert release._default_suite_passed(focused) is False
    assert release._default_suite_passed(failing) is False


def test_environment_fingerprint_does_not_retain_compatibility_paths(tmp_path):
    (tmp_path / "VERSION").write_text("test-version", encoding="utf-8")
    compatibility = _compatibility()
    compatibility["desktop_package"]["app_asar"] = r"C:\Users\Private\app.asar"
    compatibility["storage_dir"] = r"C:\Users\Private\storage"

    fingerprint = release.environment_fingerprint(tmp_path, compatibility)
    serialized = json.dumps(fingerprint)

    assert "Private" not in serialized
    assert "app.asar" not in serialized
    assert fingerprint["anythingllm"]["app_asar_sha256"] == "a" * 64
