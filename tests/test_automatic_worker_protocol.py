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


def test_selection_acknowledgement_only_becomes_ready_after_a_file_snapshot():
    import rag_pdf_gradio_app as app

    pending, *_ = app.automatic_selection_begin_state({"revision": 2})
    assert pending == {"state": "pending", "revision": 3}
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
    assert "AUTO-CONFIRM-003" in updates[0][0]["value"]
    assert status == {"state": "preparing"}


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
    assert len(updates[0]) == 13
    assert updates[0][-1] == ""


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
