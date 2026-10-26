import os
from pathlib import Path
from unittest.mock import patch

import pytest

import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


def old_snapshot(storage):
    documents = (Path(storage) / "documents").resolve()
    return sorted(
        path.relative_to(documents).as_posix()
        for path in (documents / "custom-documents").rglob("*.json")
        if not path.is_symlink()
    )


def test_nested_snapshot_retains_all_paths_without_per_file_path_stat(tmp_path):
    custom = tmp_path / "documents/custom-documents"
    for name in [
        "first.json",
        "nested/second.JSON",
        "nested/deeper/third.json",
        "skip.txt",
        ".hidden.json",
    ]:
        path = custom / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf8")
    (custom / "directory.json").mkdir()
    expected = old_snapshot(tmp_path)

    def no_path_symlink_stat(*args, **kwargs):
        raise AssertionError("Do not re-stat each enumerated file")

    with patch.object(Path, "is_symlink", no_path_symlink_stat):
        assert pipeline.snapshot_staged_document_locations(tmp_path) == expected


def test_symlinks_are_excluded_and_not_traversed(tmp_path):
    custom = tmp_path / "documents/custom-documents"
    custom.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text("{}", encoding="utf8")
    try:
        os.symlink(outside, custom / "linked-folder", target_is_directory=True)
        os.symlink(outside / "secret.json", custom / "linked-file.json")
    except OSError:
        pytest.skip("Creating Windows symbolic links is unavailable")
    assert (
        pipeline.snapshot_staged_document_locations(tmp_path)
        == old_snapshot(tmp_path)
        == []
    )


def test_empty_and_unavailable_snapshot_keep_previous_contract(tmp_path):
    assert pipeline.snapshot_staged_document_locations("") == []
    assert pipeline.snapshot_staged_document_locations(tmp_path) == []


def test_unreadable_branch_does_not_erase_other_observed_locations(
    tmp_path, monkeypatch
):
    custom = tmp_path / "documents/custom-documents"
    (custom / "blocked").mkdir(parents=True)
    (custom / "blocked/missing.json").write_text("{}", encoding="utf8")
    (custom / "kept.json").write_text("{}", encoding="utf8")
    original = os.scandir

    def partially_unavailable(path):
        if Path(path).name == "blocked":
            raise PermissionError("branch became unavailable")
        return original(path)

    monkeypatch.setattr(os, "scandir", partially_unavailable)
    expected = old_snapshot(tmp_path)
    assert expected == ["custom-documents/kept.json"]
    assert pipeline.snapshot_staged_document_locations(tmp_path) == expected
