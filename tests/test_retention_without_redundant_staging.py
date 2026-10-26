import json
from pathlib import Path

import pytest

import auto_anythingllm_pipeline as pipeline
import portable_paths

pytestmark = pytest.mark.offline_deterministic


def test_existing_identical_export_is_verified_without_a_temporary_copy(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv(portable_paths.DATA_DIRECTORY_ENVIRONMENT_VARIABLE, str(home))
    root = home / "run-state/automatic-runs/r-test/paper"
    selected = root / "selected"
    selected.mkdir(parents=True)
    prepared = selected / "Paper-pdf-parsed.txt"
    prepared.write_text("Full prepared transcript", encoding="utf8")
    payload = root / "payload.txt"
    body = "First line\nSecond line — preserving Unicode."
    payload.write_text(body, encoding="utf8")
    original_bytes = payload.read_bytes()
    (root / "artifact-locations.json").write_text(
        json.dumps({"roles": {"payload-role.txt": "payload.txt"}}), encoding="utf8"
    )
    writes = []
    original_write = Path.write_text

    def track(path, text, *args, **kwargs):
        if ".retained-segments-" in str(path):
            writes.append(text)
        return original_write(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", track)
    summary = {
        "api_upload_status": "skipped_prepare_only",
        "post_upload_verification_status": "not_checked_no_upload",
        "anythingllm_runtime_validation_status": "not_checked_no_upload",
        "readiness_status": "ready",
        "segment_mode": "page_passages",
    }
    result = pipeline.retain_successful_run_leanly(
        root,
        summary,
        {},
        prepared,
        segments=[
            {"pdf_page": 1, "text": body},
            {"pdf_page": 2, "text": "New unique body"},
        ],
    )
    assert result["applied"]
    assert writes == ["New unique body"]
    assert result["retained_segment_exports"] == [
        {"filename": "Paper-p001-s01.txt", "path": "payload.txt"},
        {"filename": "Paper-p002-s01.txt", "path": "Paper-p002-s01.txt"},
    ]
    assert payload.read_bytes() == original_bytes
    assert (root / "Paper-p002-s01.txt").read_text(encoding="utf8") == "New unique body"
    assert list(root.rglob(".retained-segments-*")) == []
