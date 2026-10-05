import hashlib
import json
import sqlite3
import struct
import uuid

import pytest

from embedding_cache import (
    CACHE_CONTRACT,
    CACHE_MARKER,
    cache_configuration,
    cache_entry_usable,
)


pytestmark = pytest.mark.offline_deterministic


def certified_entry(tmp_path, *, location="custom-documents/one.json", model="model-a"):
    (tmp_path / CACHE_MARKER).write_text(
        json.dumps({"contract": CACHE_CONTRACT}), encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "EMBEDDING_ENGINE=openrouter\n"
        f"EMBEDDING_MODEL_PREF={model}\n"
        "EMBEDDING_MODEL_MAX_CHUNK_LENGTH=8191\n",
        encoding="utf-8",
    )
    with sqlite3.connect(tmp_path / "anythingllm.db") as connection:
        connection.execute("create table system_settings(label text, value text)")
        connection.executemany(
            "insert into system_settings values (?,?)",
            [("text_splitter_chunk_size", "1000"), ("text_splitter_chunk_overlap", "20")],
        )
    configuration = cache_configuration(tmp_path)
    metadata = {
        "text": "exact cached text", "docSource": "local-pdf://sha256/source",
        "chunkSource": "page-parent://source-p0001",
    }
    vector = [0.125, -0.25, 0.5]
    text = "\0".join(metadata[name] for name in ("text", "docSource", "chunkSource"))
    proof = {
        "contract": CACHE_CONTRACT,
        "configuration": configuration,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "vector_sha256": hashlib.sha256(struct.pack("<ddd", *vector)).hexdigest(),
    }
    cache_dir = tmp_path / "vector-cache"
    cache_dir.mkdir()
    path = cache_dir / f"{uuid.uuid5(uuid.NAMESPACE_URL, location)}.json"
    groups = [[{
        "values": vector, "metadata": metadata,
        "pdfAssistantCacheIdentity": proof,
    }]]
    path.write_text(json.dumps(groups), encoding="utf-8")
    return location, path, groups


def test_valid_entry_requires_matching_active_configuration_and_provenance(tmp_path):
    location, _, _ = certified_entry(tmp_path)
    assert cache_entry_usable(
        tmp_path, location,
        expected_metadata={
            "docSource": "local-pdf://sha256/source",
            "chunkSource": "page-parent://source-p0001",
        },
    )
    assert not cache_entry_usable(
        tmp_path, location,
        expected_metadata={"chunkSource": "page-parent://different"},
    )
    (tmp_path / ".env").write_text(
        "EMBEDDING_ENGINE=openrouter\nEMBEDDING_MODEL_PREF=model-b\n"
        "EMBEDDING_MODEL_MAX_CHUNK_LENGTH=8191\n",
        encoding="utf-8",
    )
    assert not cache_entry_usable(tmp_path, location)


@pytest.mark.parametrize("mutation", ["vector", "text", "corrupt", "legacy"])
def test_tampered_corrupt_and_uncertified_entries_are_rejected(tmp_path, mutation):
    location, path, groups = certified_entry(tmp_path)
    if mutation == "vector":
        groups[0][0]["values"][0] += 1
        path.write_text(json.dumps(groups), encoding="utf-8")
    elif mutation == "text":
        groups[0][0]["metadata"]["text"] += " changed"
        path.write_text(json.dumps(groups), encoding="utf-8")
    elif mutation == "corrupt":
        path.write_text("{partial", encoding="utf-8")
    else:
        groups[0][0].pop("pdfAssistantCacheIdentity")
        path.write_text(json.dumps(groups), encoding="utf-8")
    assert not cache_entry_usable(tmp_path, location)


def test_marker_absence_never_certifies_a_legacy_cache(tmp_path):
    location, _, _ = certified_entry(tmp_path)
    (tmp_path / CACHE_MARKER).unlink()
    assert cache_configuration(tmp_path) == {"unavailable": True}
    assert not cache_entry_usable(tmp_path, location)
