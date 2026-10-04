"""Untrusted Desktop upload locations must not move unrelated files."""
from pathlib import Path

import pytest

import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("location", [
    "custom-documents/../../victim.txt", "custom-documents/../victim.txt",
    "custom-documents\\..\\..\\victim.txt", "custom-documents/C:evil.txt",
    "custom-documents//record.json", "custom-documents/./record.json",
    "custom-documents/record\x00.json",
])
@pytest.mark.parametrize("folder", ["custom-documents", "custom-documents/academic"])
def test_unsafe_locations_are_rejected_without_mutation(tmp_path, location, folder):
    victim = tmp_path / "victim.txt"
    victim.write_text("Keep this file.")
    storage = tmp_path / "storage"
    (storage / "documents" / "custom-documents").mkdir(parents=True)
    returned, error = pipeline.relocate_uploaded_document(storage, location, folder)
    assert error
    assert returned == location
    assert victim.read_text() == "Keep this file."
    assert not (storage / "documents" / "custom-documents" / "academic").exists()


def test_directory_link_cannot_escape_storage(tmp_path):
    storage = tmp_path / "storage"
    root = storage / "documents" / "custom-documents"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "record.json"
    victim.write_text("Keep this file.")
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks require an OS privilege on this machine.")
    _, error = pipeline.relocate_uploaded_document(
        storage, "custom-documents/linked/record.json", "custom-documents/academic")
    assert error
    assert victim.read_text() == "Keep this file."


@pytest.mark.parametrize("absolute", [False, True])
def test_valid_nested_upload_keeps_existing_layout(tmp_path, absolute):
    source = tmp_path / "documents" / "custom-documents" / "incoming" / "record.json"
    source.parent.mkdir(parents=True)
    source.write_text("Academic text.")
    location = str(source) if absolute else "custom-documents/incoming/record.json"
    returned, error = pipeline.relocate_uploaded_document(
        tmp_path, location, "custom-documents/workspace/document")
    assert not error
    assert returned == "custom-documents/workspace/document/record.json"
    assert (tmp_path / "documents" / Path(returned)).read_text() == "Academic text."
