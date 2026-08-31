import hashlib
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import anythingllm_source_atomic_server as source_atomic_server  # noqa: E402
from anythingllm_compatibility import OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS  # noqa: E402


def _report(executable):
    return {
        "status": "pass",
        "characterization": {
            "desktop_version_normalized": "1.16.1",
            "native_mutation_contract": source_atomic_server.V1161_NATIVE_CONTRACT_ID,
            "desktop_package": {"app_asar_sha256": OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS["1.16.1"]},
            "desktop_executable": str(executable),
        },
    }


def _server_fixture():
    return (
        '"use strict";var UM={addDocuments:async function(s,e=[],t=null){'
        'let legacy=true;return legacy},removeDocuments:async function(){}};'
    )


def test_server_patch_is_idempotent_and_keeps_non_openrouter_branch():
    patched = source_atomic_server.patch_v1161_server_source(_server_fixture())

    assert source_atomic_server.SOURCE_ATOMIC_SERVER_PATCH_ID in patched
    assert source_atomic_server.OPENROUTER_GATE in patched
    assert "source_staging_provider_batch" in patched
    assert "let legacy=true" in patched
    assert source_atomic_server.patch_v1161_server_source(patched) == patched


def test_server_installer_is_hash_gated_and_requires_restart(tmp_path, monkeypatch):
    resources = tmp_path / "resources" / "backend"
    resources.mkdir(parents=True)
    server = resources / "server.js"
    source = _server_fixture()
    server.write_text(source, encoding="utf-8")
    baseline = hashlib.sha256(server.read_bytes()).hexdigest()
    monkeypatch.setattr(source_atomic_server, "V1161_SERVER_SHA256", baseline)
    executable = tmp_path / "AnythingLLM.exe"
    executable.write_bytes(b"desktop")

    installed = source_atomic_server.ensure_source_atomic_embedding_server(_report(executable))

    assert installed["status"] == "restart_required"
    assert source_atomic_server.SOURCE_ATOMIC_SERVER_PATCH_ID in server.read_text(encoding="utf-8")
    monkeypatch.setattr(
        source_atomic_server,
        "_activation_state_for_installed_worker",
        lambda *_args: (True, "", False),
    )
    active = source_atomic_server.ensure_source_atomic_embedding_server(_report(executable))
    assert active["status"] == "already_enabled"
    assert active["enabled"] is True


def test_server_installer_refuses_unknown_server_hash(tmp_path):
    resources = tmp_path / "resources" / "backend"
    resources.mkdir(parents=True)
    (resources / "server.js").write_text(_server_fixture(), encoding="utf-8")
    executable = tmp_path / "AnythingLLM.exe"
    executable.write_bytes(b"desktop")

    result = source_atomic_server.ensure_source_atomic_embedding_server(_report(executable))

    assert result["status"] == "disabled"
    assert result["reason"] == "v1_16_1_server_hash_not_matched"
