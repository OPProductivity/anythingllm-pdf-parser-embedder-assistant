from pathlib import Path

import pytest

import rag_pdf_gradio_app as app
from prepared_batch_recovery import build_prepared_batch_checkpoint


pytestmark = pytest.mark.offline_deterministic


def test_confirmed_source_version_detects_replacement(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"first-version")
    expected = app.local_source_version(source)

    changed, observed, reason = app.confirmed_source_version_changed(source, expected)
    assert changed is False
    assert observed == expected
    assert reason == "source_version_matches"

    source.write_bytes(b"replacement-version-with-different-size")
    changed, observed, reason = app.confirmed_source_version_changed(source, expected)
    assert changed is True
    assert observed != expected
    assert reason == "source_version_changed"


def test_confirmed_source_version_detects_disappearance(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"selected-version")
    expected = app.local_source_version(source)
    source.unlink()

    changed, observed, reason = app.confirmed_source_version_changed(source, expected)
    assert changed is True
    assert observed == {}
    assert reason.startswith("source_unreadable:")


def test_changed_source_receipt_is_durable_source_local_failure(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    source = tmp_path / "source.pdf"
    source.write_bytes(b"changed")
    summary = app.changed_source_failure_summary(
        source,
        run_root / "source-output",
        reason="source_version_changed",
        expected_version={"size": 10, "mtime_ns": 1, "ctime_ns": 1},
        observed_version={"size": 7, "mtime_ns": 2, "ctime_ns": 2},
    )

    checkpoint = build_prepared_batch_checkpoint(
        run_root,
        [summary],
        total_sources=1,
        workspace_slug="fixture",
        api_url="http://127.0.0.1:3001/api",
        stage="preparation_in_progress",
    )
    assert checkpoint["sources"][0]["state"] == "preparation_failed"
    assert summary["api_upload_status"] == "error_source_changed_after_confirmation"
    assert Path(summary["output_root"], "run-summary.json").is_file()
