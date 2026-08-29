from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from automatic_worker_protocol import serializable_automatic_worker_arguments


pytestmark = pytest.mark.offline_deterministic


def test_worker_contract_keeps_key_ephemeral_and_rejects_new_durable_fields():
    payload, key = serializable_automatic_worker_arguments(
        SimpleNamespace(
            document_label="Approved document",
            anythingllm_api_key="execution-only-key",  # pragma: allowlist secret -- redaction boundary fixture
            progress_callback=None,
        )
    )

    assert payload == {"document_label": "Approved document"}
    assert key == "execution-only-key"  # pragma: allowlist secret -- redaction boundary assertion

    with pytest.raises(ValueError, match="provider_token"):
        serializable_automatic_worker_arguments(
            SimpleNamespace(document_label="Approved document", provider_token="must-not-persist")
        )


def test_worker_contract_allows_a_verified_source_identity_snapshot():
    payload, key = serializable_automatic_worker_arguments(
        SimpleNamespace(
            precomputed_source_sha256="a" * 64,
            precomputed_source_fingerprint={"size": 123, "mtime_ns": 456},
        )
    )

    assert key == ""
    assert payload == {
        "precomputed_source_sha256": "a" * 64,
        "precomputed_source_fingerprint": {"size": 123, "mtime_ns": 456},
    }


def test_worker_contract_carries_only_compact_external_compatibility_authority():
    authority = {
        "status": "qualified",
        "native_mutation_contract": "contract-1",
        "package_fingerprint_sha256": "a" * 64,
    }
    payload, key = serializable_automatic_worker_arguments(
        SimpleNamespace(external_compatibility_evidence=authority)
    )

    assert key == ""
    assert payload == {"external_compatibility_evidence": authority}


def test_selected_input_duplicate_receipt_is_not_a_workspace_duplicate(tmp_path):
    import rag_pdf_gradio_app as app

    canonical = tmp_path / "canonical.pdf"
    duplicate = tmp_path / "copy.pdf"
    summary = app.selected_input_exact_duplicate_summary(
        duplicate,
        tmp_path / "output",
        canonical_path=canonical,
        source_sha256="b" * 64,
    )

    assert summary["post_upload_classification"] == "selected_input_exact_duplicate_skipped"
    assert summary["api_upload_status"] == "skipped_exact_duplicate"
    assert summary["selected_input_duplicate_of"] == str(canonical)
    assert "workspace_duplicate_preflight" not in summary
    assert (tmp_path / "output" / "selected-input-duplicate.json").is_file()


def test_exact_selected_duplicate_is_a_successful_non_upload_completion():
    import rag_pdf_gradio_app as app

    summary = {
        "pdf": "C:/copy.pdf",
        "api_upload_status": "skipped_exact_duplicate",
        "post_upload_verification_status": "pass",
        "post_upload_classification": "selected_input_exact_duplicate_skipped",
        "anythingllm_runtime_validation_status": "not_required_exact_selection_duplicate",
    }

    completion = app.automatic_completion([summary], prepare_and_upload=True)
    receipt = app.automatic_completion_receipt([summary])

    assert completion["state"] == "successful"
    assert receipt["failed_pdfs"] == 0
    assert receipt["selected_input_exact_duplicates"] == 1


def test_selection_acknowledgement_only_becomes_ready_after_a_file_snapshot():
    import rag_pdf_gradio_app as app

    pending, *_ = app.automatic_selection_begin_state({"revision": 2})
    assert pending == {
        "state": "pending",
        "revision": 3,
        "selection_signature": "",
        "accept_next_signature": False,
    }
    assert not app.automatic_selection_is_ready(pending)

    ready = app.automatic_selection_finish_state(pending, ["C:/approved.pdf"], [], {})[0]
    assert app.automatic_selection_is_ready(ready)


def test_confirm_guard_refuses_a_replayed_click_while_selection_is_pending():
    import rag_pdf_gradio_app as app

    original_status = app.LIVE_AUTOMATIC_RUN_STATUS
    try:
        app.LIVE_AUTOMATIC_RUN_STATUS = {"state": "preparing"}
        updates = list(
            app.run_automatic_from_ready_confirmation_stream(
                {"state": "pending", "revision": 1},
                *([None] * len(app.AUTOMATIC_RUN_FIELDS)),
            )
        )
        status = dict(app.LIVE_AUTOMATIC_RUN_STATUS)
    finally:
        app.LIVE_AUTOMATIC_RUN_STATUS = original_status

    assert len(updates) == 1
    assert len(updates[0]) == 12
    assert "run confirmation" in updates[0][0]["value"]
    assert "AUTO-CONFIRM-003" not in updates[0][0]["value"]
    assert status == {"state": "preparing"}


def test_confirm_claims_and_acknowledges_before_the_background_worker_runs(monkeypatch):
    """A first click must visibly respond even if worker scheduling is delayed.

    This is the exact window that previously made a legitimate Confirm look
    inert: Gradio could detach the browser stream before the worker produced
    its first generator value.  Keep the worker held so the test proves the
    response and lifecycle claim originate in the click handler itself.
    """
    import rag_pdf_gradio_app as app

    class HeldWorker:
        def __init__(self, *args, **kwargs):
            self.started = False

        def start(self):
            self.started = True

    original_status = app.LIVE_AUTOMATIC_RUN_STATUS
    monkeypatch.setattr(app.threading, "Thread", HeldWorker)
    try:
        app.LIVE_AUTOMATIC_RUN_STATUS = {}
        stream = app.run_automatic_from_confirmation_stream(
            *([None] * len(app.AUTOMATIC_RUN_FIELDS))
        )
        acknowledgement = next(stream)
        status = dict(app.LIVE_AUTOMATIC_RUN_STATUS)
        stream.close()
    finally:
        app.LIVE_AUTOMATIC_RUN_STATUS = original_status

    assert len(acknowledgement) == 12
    assert acknowledgement[4]["value"] == "Processing…"
    assert status["state"] == "preparing"
    assert status["confirmation_in_flight"] is True
    assert status["confirmation_owner_token"]


def test_browser_status_poller_ignores_a_run_owned_by_another_session():
    import rag_pdf_gradio_app as app

    original_status = app.LIVE_AUTOMATIC_RUN_STATUS
    try:
        app.LIVE_AUTOMATIC_RUN_STATUS = {
            "state": "running",
            "run_root": "C:/runs/other-browser",
            "expected_seconds": 30,
        }
        updates = app.refresh_live_automatic_run_ui("C:/runs/this-browser")
    finally:
        app.LIVE_AUTOMATIC_RUN_STATUS = original_status

    assert len(updates) == 10
    assert all(update.get("__type__") == "update" for update in updates)


def test_browser_stream_keeps_presentation_state_as_an_extra_output():
    import rag_pdf_gradio_app as app

    updates = list(
        app.run_automatic_for_browser_stream(
            {"state": "pending", "revision": 1},
            "",
            *([None] * len(app.AUTOMATIC_RUN_FIELDS)),
        )
    )

    assert len(updates) == 1
    assert len(updates[0]) == 16
    assert updates[0][-4] == ""
    assert all(update.get("__type__") == "update" for update in updates[0][-3:])


def test_successful_retention_reports_a_locked_artifact_without_failing_the_document(tmp_path, monkeypatch):
    import auto_anythingllm_pipeline as pipeline

    root = tmp_path / "output"
    selected = root / "selected"
    selected.mkdir(parents=True)
    prepared = selected / "prepared.txt"
    prepared.write_text("usable transcript", encoding="utf-8")
    locked = selected / "locked-evidence"
    locked.mkdir()
    (locked / "receipt.json").write_text("{}", encoding="utf-8")
    original_rmtree = pipeline.shutil.rmtree

    def sharing_violation(path, *args, **kwargs):
        if Path(path).name in {"locked-evidence", "selected"}:
            raise PermissionError("simulated Windows sharing violation")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(pipeline.shutil, "rmtree", sharing_violation)
    result = pipeline.retain_successful_run_leanly(
        root,
        {
            "readiness_status": "ready",
            "api_upload_status": "skipped_prepare_only",
            "post_upload_verification_status": "not_checked_no_upload",
            "anythingllm_runtime_validation_status": "not_checked_no_upload",
            "segments": 0,
        },
        {"source_file": "C:/approved.pdf", "filename": "approved.pdf"},
        prepared,
    )

    assert result["applied"] is True
    assert result["cleanup_pending"] is True
    assert (root / "prepared.txt").is_file()
    assert locked.is_dir()
