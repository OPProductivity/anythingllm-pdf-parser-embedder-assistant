import sqlite3

import pytest

import anythingllm_compatibility
from anythingllm_compatibility import (
    CAPABILITIES,
    Evidence,
    OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS,
    OBSERVED_CANDIDATE_DESKTOP_VERSIONS,
    OBSERVED_COMPATIBLE_DESKTOP_VERSIONS,
    PROFILE_ID,
    characterize,
    probe_api_contract,
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


def _desktop_version(version):
    return version, [Evidence("fixture", "desktop", version, "fixture")], []


def test_recognized_profile_grants_only_filesystem_proven_capabilities(tmp_path, monkeypatch):
    monkeypatch.setattr(anythingllm_compatibility, "_desktop_version", lambda _path: _desktop_version("1.14.2"))
    (tmp_path / ".env").write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    (tmp_path / "documents").mkdir()
    (tmp_path / "lancedb").mkdir()
    create_profile_database(tmp_path / "anythingllm.db")

    result = characterize(tmp_path)

    assert result["matched_profile"] == PROFILE_ID
    assert result["desktop_release_status"] == "recognized_mutation_profile"
    assert result["storage_schema_status"] == "matched"
    assert result["observed_compatible_desktop_versions"] == list(OBSERVED_COMPATIBLE_DESKTOP_VERSIONS)
    assert result["capabilities"]["can_read_sqlite_state"]["status"] == "supported"
    assert result["capabilities"]["can_write_sqlite_settings"]["status"] == "supported"
    assert result["capabilities"]["can_create_workspace"]["status"] == "unknown"
    assert set(result["capabilities"]) == set(CAPABILITIES)


def test_unknown_schema_blocks_mutation_but_retains_local_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(anythingllm_compatibility, "_desktop_version", lambda _path: _desktop_version("1.14.2"))
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


def test_v116_candidate_keeps_read_only_sqlite_inspection_but_blocks_settings_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(anythingllm_compatibility, "_desktop_version", lambda _path: _desktop_version("1.16.0.0"))
    candidate_hash = OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS["1.16.0"]
    monkeypatch.setattr(
        anythingllm_compatibility,
        "_desktop_package_identity",
        lambda _path, include_fingerprint: (
            {"app_asar_sha256": candidate_hash, "fingerprint_status": "computed"},
            [],
            [],
        ),
    )
    (tmp_path / ".env").write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    (tmp_path / "documents").mkdir()
    (tmp_path / "lancedb").mkdir()
    create_profile_database(tmp_path / "anythingllm.db")

    result = characterize(tmp_path)

    assert result["matched_profile"] == ""
    assert result["desktop_release_status"] == "observed_candidate"
    assert result["desktop_version"] == "1.16.0.0"
    assert result["desktop_version_normalized"] == OBSERVED_CANDIDATE_DESKTOP_VERSIONS[0]
    assert result["capabilities"]["can_read_sqlite_state"]["status"] == "supported"
    assert result["capabilities"]["can_write_sqlite_settings"]["status"] == "blocked"


def test_loopback_api_contract_probe_reports_core_routes_without_granting_writes(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'''window.onload = function() { const spec = {"paths": {
                "/v1/document/raw-text": {},
                "/v1/document/upload": {},
                "/v1/workspaces": {},
                "/v1/workspace/{slug}/update-embeddings": {}
            }}; };'''

    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, dict(request.header_items()), timeout))
        return Response()

    monkeypatch.setattr(anythingllm_compatibility.urllib.request, "urlopen", fake_urlopen)

    result = probe_api_contract("http://127.0.0.1:3001")

    assert result["status"] == "qualified_read_only_contract"
    assert result["missing_core_routes"] == []
    assert result["write_authority"] == "not_granted_by_api_contract"
    assert result["advisory_routes"]["/v1/workspace/{slug}/embed-progress"] == "undocumented_advisory"
    assert requests[0][0].endswith("/api/docs/swagger-ui-init.js")
    assert "Authorization" not in requests[0][1]


def test_api_contract_probe_rejects_non_loopback_urls_without_network_access(monkeypatch):
    monkeypatch.setattr(
        anythingllm_compatibility.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("non-loopback URL must not be requested"),
    )

    result = probe_api_contract("https://example.invalid")

    assert result["status"] == "unavailable"
    assert result["error"] == "api_url_must_be_loopback_http"
