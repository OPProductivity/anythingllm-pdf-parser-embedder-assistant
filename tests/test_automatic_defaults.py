from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# The home override is set before importing the path-dependent module.  Tests
# still pass an explicit temporary home to make every filesystem target clear.
os.environ.setdefault("ANYTHINGLLM_PDF_ASSISTANT_HOME", str(PROJECT_ROOT / "tmp-test-home"))

import automatic_defaults as defaults  # noqa: E402


pytestmark = pytest.mark.offline_deterministic


@pytest.fixture
def builtin_defaults():
    excluded_field = "".join(("api", "_", "key"))
    return {
        "mode": "Upload",
        "output_root_override": "C:/output",
        "use_file_title_fallback": True,
        "api_url": "http://127.0.0.1:3001",
        "native_upload_scope": "All segments",
        "ollama_url": "http://127.0.0.1:11434",
        "download_full_folder": False,
        "download_segments_folder": False,
        "document_label": "Detected title",
        excluded_field: "test-key-not-persisted",
        "workspace_slug": "workspace",
        "native_upload_custom_range": "1-3",
        "deep_extraction": False,
        "segment_mode": "Custom Range",
        "custom_page_group_sizes": "20",
        "anythingllm_chunk_size": "768",
        "auto_apply_recommended_settings": False,
    }


def test_profile_round_trip_keeps_only_future_run_allowlist(tmp_path, builtin_defaults):
    excluded_field = "".join(("api", "_", "key"))
    values = builtin_defaults | {
        "deep_extraction": True,
        "document_label": "Private document",
        "workspace_slug": "future-workspace",
        "native_upload_custom_range": "20-30",
        "auto_apply_recommended_settings": True,
    } | {excluded_field: "test-key-not-persisted"}

    result = defaults.save_automatic_defaults(values, builtin_defaults, expected_revision=0, home_directory=tmp_path)
    loaded = defaults.load_automatic_defaults(builtin_defaults, home_directory=tmp_path)
    profile = json.loads(defaults.automatic_defaults_path(tmp_path).read_text(encoding="utf-8"))

    assert result["status"] == "saved"
    assert loaded["revision"] == 1
    assert loaded["defaults"] == {
        "mode": "Upload",
        "output_root_override": "C:/output",
        "use_file_title_fallback": True,
        "api_url": "http://127.0.0.1:3001",
        "workspace_slug": "future-workspace",
        "native_upload_scope": "All segments",
        "ollama_url": "http://127.0.0.1:11434",
        "deep_extraction": True,
        "segment_mode": "Custom Range",
        "custom_page_group_sizes": "20",
        "anythingllm_chunk_size": "768",
        "download_full_folder": False,
        "download_segments_folder": False,
    }
    assert set(profile["defaults"]).isdisjoint(
        {"document_label", "api_key", "native_upload_custom_range", "auto_apply_recommended_settings"}
    )


def test_corrupt_and_unsupported_profiles_are_backed_up_and_reset(tmp_path, builtin_defaults):
    path = defaults.automatic_defaults_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    corrupt = defaults.load_automatic_defaults(builtin_defaults, home_directory=tmp_path)
    path.write_text(json.dumps({"schema_version": 99, "revision": 1, "defaults": {}}), encoding="utf-8")
    unsupported = defaults.load_automatic_defaults(builtin_defaults, home_directory=tmp_path)

    assert corrupt["defaults"] == {}
    assert "corrupt" in corrupt["notice"]
    assert unsupported["defaults"] == {}
    assert "unsupported" in unsupported["notice"]
    assert len(list(path.parent.glob("automatic-defaults.corrupt-*.json"))) == 1
    assert len(list(path.parent.glob("automatic-defaults.unsupported-*.json"))) == 1


def test_save_rejects_stale_revision_unless_operator_explicitly_overwrites(tmp_path, builtin_defaults):
    first = defaults.save_automatic_defaults(builtin_defaults, builtin_defaults, expected_revision=0, home_directory=tmp_path)
    conflict = defaults.save_automatic_defaults(
        builtin_defaults | {"deep_extraction": True}, builtin_defaults, expected_revision=0, home_directory=tmp_path
    )
    overwritten = defaults.save_automatic_defaults(
        builtin_defaults | {"deep_extraction": True}, builtin_defaults, expected_revision=0, home_directory=tmp_path, overwrite=True
    )

    assert first == {"status": "saved", "revision": 1, "message": "Saved Automatic defaults for future selections."}
    assert conflict["status"] == "conflict"
    assert overwritten["status"] == "saved"
    assert overwritten["revision"] == 2
    assert defaults.load_automatic_defaults(builtin_defaults, home_directory=tmp_path)["defaults"]["deep_extraction"] is True


def test_save_failure_keeps_existing_profile_and_does_not_claim_success(tmp_path, builtin_defaults, monkeypatch):
    saved = defaults.save_automatic_defaults(builtin_defaults, builtin_defaults, expected_revision=0, home_directory=tmp_path)
    original_replace = defaults.os.replace
    monkeypatch.setattr(defaults.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
    failed = defaults.save_automatic_defaults(
        builtin_defaults | {"deep_extraction": True}, builtin_defaults, expected_revision=saved["revision"], home_directory=tmp_path
    )
    monkeypatch.setattr(defaults.os, "replace", original_replace)

    assert failed["status"] == "error"
    assert defaults.load_automatic_defaults(builtin_defaults, home_directory=tmp_path)["defaults"]["deep_extraction"] is False
    assert not list(defaults.automatic_defaults_path(tmp_path).parent.glob("*.tmp"))
