import sqlite3

import pytest

from anythingllm_compatibility import (
    CAPABILITIES,
    OBSERVED_COMPATIBLE_DESKTOP_VERSIONS,
    PROFILE_ID,
    characterize,
)


pytestmark = pytest.mark.offline_deterministic


def create_profile_database(path):
    con = sqlite3.connect(path)
    con.execute("create table system_settings (label text, value text)")
    con.execute(
        "create table workspaces (id integer, name text, slug text, chatProvider text, chatModel text, "
        "topN integer, similarityThreshold real, vectorSearchMode text, chatMode text)"
    )
    con.execute(
        "create table workspace_documents (id integer, docId text, filename text, docpath text, "
        "metadata text, createdAt text)"
    )
    con.execute("create table document_vectors (docId text, vectorId text)")
    con.commit()
    con.close()


def test_recognized_profile_grants_only_filesystem_proven_capabilities(tmp_path):
    (tmp_path / ".env").write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    (tmp_path / "documents").mkdir()
    (tmp_path / "lancedb").mkdir()
    create_profile_database(tmp_path / "anythingllm.db")

    result = characterize(tmp_path)

    assert result["matched_profile"] == PROFILE_ID
    assert result["observed_compatible_desktop_versions"] == list(OBSERVED_COMPATIBLE_DESKTOP_VERSIONS)
    assert result["capabilities"]["can_read_sqlite_state"]["status"] == "supported"
    assert result["capabilities"]["can_write_sqlite_settings"]["status"] == "supported"
    assert result["capabilities"]["can_create_workspace"]["status"] == "unknown"
    assert set(result["capabilities"]) == set(CAPABILITIES)


def test_unknown_schema_blocks_mutation_but_retains_local_evidence(tmp_path):
    (tmp_path / ".env").write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    sqlite3.connect(tmp_path / "anythingllm.db").close()

    result = characterize(tmp_path)

    assert result["matched_profile"] == ""
    assert result["capabilities"]["can_read_env_state"]["status"] == "supported"
    assert result["capabilities"]["can_write_env_settings"]["status"] == "blocked"
    assert result["capabilities"]["can_write_sqlite_settings"]["status"] == "blocked"


def test_characterization_does_not_return_env_values(tmp_path):
    secret = "must-not-appear"  # pragma: allowlist secret -- redaction fixture
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY='{secret}'\n", encoding="utf-8")

    result = characterize(tmp_path)

    assert secret not in str(result)
