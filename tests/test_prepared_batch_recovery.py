import json
from pathlib import Path

import pytest

from prepared_batch_recovery import (
    load_verified_prepared_summaries,
    verify_prepared_batch_checkpoint,
    write_prepared_batch_checkpoint,
)


pytestmark = pytest.mark.offline_deterministic


def _prepared_summary(root: Path, name: str = "one") -> dict:
    source_root = root / name
    source_root.mkdir(parents=True)
    text = source_root / f"{name}.txt"
    text.write_text("prepared text", encoding="utf-8")
    plan = source_root / "upload-plan.csv"
    plan.write_text(
        "filename,title,docAuthor,description,docSource,chunkSource,text_file\n"
        f"{name}.txt,{name},,,local-pdf://sha256/{'a' * 64},{name}-p001,{text}\n",
        encoding="utf-8",
    )
    summary = {
        "pdf": str(root / f"{name}.pdf"),
        "source_sha256": "a" * 64,
        "output_root": str(source_root),
        "native_upload_plan": str(plan),
        "api_upload_status": "not_started",
    }
    (source_root / "run-summary.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )
    return summary


def test_complete_checkpoint_verifies_and_loads_summaries(tmp_path):
    summary = _prepared_summary(tmp_path)
    write_prepared_batch_checkpoint(
        tmp_path,
        [summary],
        total_sources=1,
        workspace_slug="workspace",
        api_url="http://127.0.0.1:3001/api?secret=excluded",
        stage="preparation_complete",
    )

    result = verify_prepared_batch_checkpoint(tmp_path)
    loaded = load_verified_prepared_summaries(tmp_path)

    assert result["status"] == "ready"
    assert result["reusable"] is True
    assert result["api_origin"] == "http://127.0.0.1:3001/api"
    assert loaded == [summary]


def test_changed_prepared_text_blocks_reuse(tmp_path):
    summary = _prepared_summary(tmp_path)
    write_prepared_batch_checkpoint(
        tmp_path,
        [summary],
        total_sources=1,
        workspace_slug="workspace",
        api_url="http://127.0.0.1:3001/api",
        stage="preparation_complete",
    )
    Path(summary["output_root"], "one.txt").write_text("changed", encoding="utf-8")

    result = verify_prepared_batch_checkpoint(tmp_path)

    assert result["reusable"] is False
    assert "changed:prepared_text" in result["reason"]
    with pytest.raises(RuntimeError):
        load_verified_prepared_summaries(tmp_path)


def test_incomplete_checkpoint_is_not_reusable(tmp_path):
    summary = _prepared_summary(tmp_path)
    write_prepared_batch_checkpoint(
        tmp_path,
        [summary],
        total_sources=2,
        workspace_slug="workspace",
        api_url="http://127.0.0.1:3001/api",
        stage="preparation_in_progress",
    )

    result = verify_prepared_batch_checkpoint(tmp_path)

    assert result["reusable"] is False
    assert result["reason"] == "preparation_checkpoint_is_incomplete"


def test_submission_started_never_authorizes_replay(tmp_path):
    summary = _prepared_summary(tmp_path)
    write_prepared_batch_checkpoint(
        tmp_path,
        [summary],
        total_sources=1,
        workspace_slug="workspace",
        api_url="http://127.0.0.1:3001/api",
        stage="submission_started",
    )

    result = verify_prepared_batch_checkpoint(tmp_path)

    assert result["reusable"] is False
    assert "reconcile source transactions" in result["reason"]


def test_exact_selected_duplicate_receipt_is_preserved_in_loaded_batch(tmp_path):
    prepared = _prepared_summary(tmp_path, "canonical")
    duplicate_root = tmp_path / "duplicate"
    duplicate_root.mkdir()
    duplicate = {
        "pdf": str(tmp_path / "duplicate.pdf"),
        "source_sha256": "a" * 64,
        "output_root": str(duplicate_root),
        "api_upload_status": "skipped_exact_duplicate",
        "post_upload_classification": "selected_input_exact_duplicate_skipped",
    }
    (duplicate_root / "selected-input-duplicate.json").write_text(
        json.dumps(duplicate), encoding="utf-8"
    )
    write_prepared_batch_checkpoint(
        tmp_path,
        [prepared, duplicate],
        total_sources=2,
        workspace_slug="workspace",
        api_url="http://127.0.0.1:3001/api",
        stage="preparation_complete",
    )

    loaded = load_verified_prepared_summaries(tmp_path)

    assert loaded == [prepared, duplicate]


def test_all_workspace_existing_sources_are_valid_without_uploadable_records(tmp_path):
    summaries = []
    for index in range(2):
        source_root = tmp_path / f"cached-{index}"
        source_root.mkdir()
        summary = {
            "pdf": str(tmp_path / f"cached-{index}.pdf"),
            "source_sha256": str(index + 1) * 64,
            "output_root": str(source_root),
            "api_upload_status": "complete",
            "post_upload_classification": "workspace_existing_content_skipped",
        }
        (source_root / "run-summary.json").write_text(
            json.dumps(summary), encoding="utf-8",
        )
        summaries.append(summary)
    write_prepared_batch_checkpoint(
        tmp_path,
        summaries,
        total_sources=2,
        workspace_slug="workspace",
        api_url="http://127.0.0.1:3001/api",
        stage="preparation_complete",
    )

    result = verify_prepared_batch_checkpoint(tmp_path)
    loaded = load_verified_prepared_summaries(tmp_path)

    assert result["status"] == "ready"
    assert result["reason"] == "all_prepared_artifacts_match_no_submission_needed"
    assert loaded == summaries


def test_missing_source_summary_refuses_checkpoint_creation(tmp_path):
    summary = {
        "pdf": str(tmp_path / "missing.pdf"),
        "source_sha256": "b" * 64,
        "output_root": str(tmp_path / "missing"),
        "native_upload_plan": str(tmp_path / "missing.csv"),
    }

    with pytest.raises(ValueError, match="source-summary"):
        write_prepared_batch_checkpoint(
            tmp_path,
            [summary],
            total_sources=1,
            workspace_slug="workspace",
            api_url="http://127.0.0.1:3001/api",
            stage="preparation_complete",
        )
