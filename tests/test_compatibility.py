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
    assert result["capabilities"]["can_create_workspace"]["status"] == "blocked"
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


def test_v116_fingerprinted_profile_grants_guarded_settings_writes(tmp_path, monkeypatch):
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

    assert result["matched_profile"] == anythingllm_compatibility.V116_PROFILE_ID
    assert result["desktop_release_status"] == "recognized_mutation_profile"
    assert result["desktop_version"] == "1.16.0.0"
    assert result["desktop_version_normalized"] == OBSERVED_CANDIDATE_DESKTOP_VERSIONS[0]
    assert result["capabilities"]["can_read_sqlite_state"]["status"] == "supported"
    assert result["capabilities"]["can_write_sqlite_settings"]["status"] == "supported"


def test_v116_exact_package_contract_grants_only_observed_native_capabilities(tmp_path, monkeypatch):
    expected = anythingllm_compatibility.OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS["1.16.0"]
    monkeypatch.setattr(anythingllm_compatibility, "_desktop_version", lambda _exe: ("1.16.0.0", [], []))
    monkeypatch.setattr(
        anythingllm_compatibility,
        "_desktop_package_identity",
        lambda _path, include_fingerprint: (
            {"app_asar_sha256": expected, "fingerprint_status": "computed"}, [], [],
        ),
    )
    (tmp_path / ".env").write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    (tmp_path / "documents").mkdir()
    (tmp_path / "lancedb").mkdir()
    create_profile_database(tmp_path / "anythingllm.db")

    result = anythingllm_compatibility.characterize(tmp_path, include_package_fingerprint=True)

    assert result["native_mutation_contract"] == anythingllm_compatibility.V116_NATIVE_CONTRACT_ID
    assert result["capabilities"]["can_create_workspace"]["status"] == "supported"
    assert result["capabilities"]["can_upload_native_metadata"]["status"] == "supported"
    assert result["capabilities"]["can_poll_post_upload_state"]["status"] == "supported"
    assert result["capabilities"]["can_delete_workspace"]["status"] == "unknown"
    assert result["capabilities"]["can_create_temp_api_key"]["status"] == "unknown"


def test_v1161_exact_package_contract_grants_qualified_probe_capabilities(tmp_path, monkeypatch):
    expected = anythingllm_compatibility.OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS["1.16.1"]
    monkeypatch.setattr(anythingllm_compatibility, "_desktop_version", lambda _exe: ("1.16.1.0", [], []))
    monkeypatch.setattr(
        anythingllm_compatibility,
        "_desktop_package_identity",
        lambda _path, include_fingerprint: (
            {"app_asar_sha256": expected, "fingerprint_status": "computed", "resources_dir": ""}, [], [],
        ),
    )
    (tmp_path / ".env").write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    (tmp_path / "documents").mkdir()
    (tmp_path / "lancedb").mkdir()
    create_profile_database(tmp_path / "anythingllm.db")

    result = anythingllm_compatibility.characterize(tmp_path, include_package_fingerprint=True)

    assert result["matched_profile"] == anythingllm_compatibility.V1161_PROFILE_ID
    assert result["native_mutation_contract"] == anythingllm_compatibility.V1161_NATIVE_CONTRACT_ID
    for capability in (
        "can_create_temp_api_key",
        "can_delete_temp_api_key",
        "can_create_workspace",
        "can_delete_workspace",
        "can_upload_native_metadata",
        "can_poll_post_upload_state",
        "can_runtime_verify_embedder",
    ):
        assert result["capabilities"][capability]["status"] == "supported"


def test_unknown_package_blocks_native_mutation_without_hiding_read_only_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(anythingllm_compatibility, "_desktop_version", lambda _exe: ("9.9.9", [], []))
    monkeypatch.setattr(
        anythingllm_compatibility,
        "_desktop_package_identity",
        lambda _path, include_fingerprint: (
            {"app_asar_sha256": "f" * 64, "fingerprint_status": "computed"}, [], [],
        ),
    )
    (tmp_path / ".env").write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    (tmp_path / "documents").mkdir()
    (tmp_path / "lancedb").mkdir()
    create_profile_database(tmp_path / "anythingllm.db")

    result = anythingllm_compatibility.characterize(tmp_path, include_package_fingerprint=True)

    assert result["capabilities"]["can_read_sqlite_state"]["status"] == "supported"
    assert result["capabilities"]["can_upload_native_metadata"]["status"] == "blocked"
    assert result["native_mutation_contract_status"] == "unmatched"


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


def test_api_contract_probe_uses_bounded_installed_openapi_when_swagger_initializer_is_empty(
    tmp_path, monkeypatch,
):
    class EmptyResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b""

    openapi = tmp_path / "openapi.json"
    openapi.write_text(
        __import__("json").dumps({
            "paths": {route: {} for route in anythingllm_compatibility.REQUIRED_API_CONTRACT_ROUTES}
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        anythingllm_compatibility.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: EmptyResponse(),
    )

    result = probe_api_contract(
        "http://127.0.0.1:3001",
        installed_openapi_path=openapi,
    )

    assert result["status"] == "qualified_read_only_contract"
    assert result["contract_evidence_source"] == "installed_package_openapi"
    assert result["missing_core_routes"] == []


def test_api_contract_probe_rejects_non_loopback_urls_without_network_access(monkeypatch):
    monkeypatch.setattr(
        anythingllm_compatibility.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("non-loopback URL must not be requested"),
    )

    result = probe_api_contract("https://example.invalid")

    assert result["status"] == "unavailable"
    assert result["error"] == "api_url_must_be_loopback_http"


def test_package_fingerprint_is_cached_only_for_unchanged_package_identity(tmp_path, monkeypatch):
    executable = tmp_path / "AnythingLLM.exe"
    executable.write_bytes(b"exe")
    asar = tmp_path / "resources" / "app.asar"
    asar.parent.mkdir()
    asar.write_bytes(b"package-one")
    calls = []
    anythingllm_compatibility._PACKAGE_FINGERPRINT_CACHE.clear()

    def fake_hash(path):
        calls.append(path.read_bytes())
        return "a" * 64 if path.read_bytes() == b"package-one" else "b" * 64

    monkeypatch.setattr(anythingllm_compatibility, "_sha256_file", fake_hash)
    first, *_ = anythingllm_compatibility._desktop_package_identity(
        executable, include_fingerprint=True,
    )
    second, *_ = anythingllm_compatibility._desktop_package_identity(
        executable, include_fingerprint=True,
    )
    assert first["app_asar_sha256"] == second["app_asar_sha256"] == "a" * 64
    assert calls == [b"package-one"]

    asar.write_bytes(b"package-two-with-different-size")
    third, *_ = anythingllm_compatibility._desktop_package_identity(
        executable, include_fingerprint=True,
    )
    assert third["app_asar_sha256"] == "b" * 64
    assert calls == [b"package-one", b"package-two-with-different-size"]
