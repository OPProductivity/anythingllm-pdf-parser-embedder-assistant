import hashlib
import json
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
    configured_cap = source_atomic_server.SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE
    assert (
        f'SOURCE_ATOMIC_EMBED_BATCH_SIZE||"{configured_cap}",10)||{configured_cap}'
        in patched
    )
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


def test_server_installer_reconfigures_a_known_previous_patch(tmp_path, monkeypatch):
    resources = tmp_path / "resources" / "backend"
    resources.mkdir(parents=True)
    server = resources / "server.js"
    original = _server_fixture()
    server.write_text(original, encoding="utf-8")
    baseline = hashlib.sha256(server.read_bytes()).hexdigest()
    monkeypatch.setattr(source_atomic_server, "V1161_SERVER_SHA256", baseline)
    executable = tmp_path / "AnythingLLM.exe"
    executable.write_bytes(b"desktop")

    expected = source_atomic_server.patch_v1161_server_source(original)
    old_generated_patch = expected.replace('||"36",10)||36', '||"28",10)||28')
    assert old_generated_patch != expected
    server.write_text(old_generated_patch, encoding="utf-8")
    (resources / "server.js.pdf-assistant-v1161.backup").write_text(original, encoding="utf-8")
    (resources / "server.js.pdf-assistant-source-atomic.json").write_text(
        json.dumps(
            {
                "patch_id": source_atomic_server.SOURCE_ATOMIC_SERVER_PATCH_ID,
                "original_server_sha256": baseline,
                "patched_server_sha256": hashlib.sha256(server.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    result = source_atomic_server.ensure_source_atomic_embedding_server(_report(executable))

    assert result["status"] == "restart_required"
    assert result["reason"] == "anythingllm_desktop_restart_required_after_source_atomic_reconfigure"
    assert server.read_text(encoding="utf-8") == expected
    manifest = json.loads(
        (resources / "server.js.pdf-assistant-source-atomic.json").read_text(encoding="utf-8")
    )
    assert manifest["provider_batch_size"] == 36
