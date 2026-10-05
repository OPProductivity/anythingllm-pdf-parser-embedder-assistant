"""Contract boundaries for the measured embedding startup and upload fixes."""
import hashlib
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import urllib.request
from unittest.mock import Mock

import pytest

import authenticated_http as transport
import anythingllm_compatibility as compatibility
import anythingllm_source_atomic_v117 as adapter
from test_source_atomic_v117 import fixture_source, report, setup_install

pytestmark = pytest.mark.offline_deterministic


def test_plain_http_never_initializes_tls(monkeypatch):
    initialize = Mock(side_effect=AssertionError("TLS initialized for HTTP"))
    monkeypatch.setattr(urllib.request.HTTPSHandler, "__init__", initialize)
    opener = transport.authenticated_opener(urllib.request.Request("http://127.0.0.1:3001/api/ping"))
    assert sum(isinstance(handler, urllib.request.HTTPSHandler) for handler in opener.handlers) == 1
    initialize.assert_not_called()


def test_https_initializes_standard_handler_before_open(monkeypatch):
    events = []
    monkeypatch.setattr(urllib.request.HTTPSHandler, "__init__", lambda self: events.append("initialize"))
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", lambda self, request: events.append("open"))
    handler = transport.DeferredHTTPSHandler()
    assert not events
    handler.https_open(urllib.request.Request("https://localhost/"))
    handler.https_open(urllib.request.Request("https://localhost/"))
    assert events == ["initialize", "open", "open"]


def test_failed_tls_initialization_can_retry(monkeypatch):
    initialize = Mock(side_effect=[OSError("fixture trust store failure"), None])
    monkeypatch.setattr(urllib.request.HTTPSHandler, "__init__", initialize)
    opened = Mock()
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", opened)
    handler = transport.DeferredHTTPSHandler()
    request = urllib.request.Request("https://localhost/")
    with pytest.raises(OSError):
        handler.https_open(request)
    opened.assert_not_called()
    handler.https_open(request)
    assert initialize.call_count == 2 and opened.call_count == 1


def test_version_is_fresh_and_avoids_subprocess(monkeypatch):
    monkeypatch.setattr(compatibility.platform, "system", lambda: "Windows")
    versions = iter(["1.17.0.0", "1.18.0-beta.1"])
    monkeypatch.setattr(compatibility, "_native_product_version", lambda path: next(versions))
    shell = Mock(side_effect=AssertionError("unneeded subprocess"))
    monkeypatch.setattr(compatibility.subprocess, "run", shell)
    assert compatibility._desktop_version(Path("fixture.exe"))[0] == "1.17.0.0"
    assert compatibility._desktop_version(Path("fixture.exe"))[0] == "1.18.0-beta.1"
    shell.assert_not_called()


@pytest.mark.parametrize("error", [OSError, ValueError, AttributeError])
def test_resource_failure_retains_powershell_fallback(monkeypatch, error):
    monkeypatch.setattr(compatibility.platform, "system", lambda: "Windows")
    monkeypatch.setattr(compatibility, "_native_product_version", Mock(side_effect=error("fixture")))
    shell = Mock(return_value=SimpleNamespace(returncode=0, stdout="1.17.0.0\n"))
    monkeypatch.setattr(compatibility.subprocess, "run", shell)
    version, evidence, errors = compatibility._desktop_version(Path("odd'name.exe"))
    assert version == "1.17.0.0" and evidence and not errors
    assert "odd''name.exe" in shell.call_args.args[0][-1]
    assert shell.call_args.kwargs["timeout"] == 10


def test_missing_and_nonwindows_skip_native_probe(monkeypatch):
    native = Mock(side_effect=AssertionError("must not probe"))
    monkeypatch.setattr(compatibility, "_native_product_version", native)
    assert compatibility._desktop_version(None)[2] == ["desktop_executable_missing"]
    monkeypatch.setattr(compatibility.platform, "system", lambda: "Linux")
    assert compatibility._desktop_version(Path("fixture.exe"))[2] == ["desktop_version_probe_unsupported_platform"]
    native.assert_not_called()


def test_known_prior_patch_upgrade_preserves_backup(tmp_path, monkeypatch):
    target, executable, source = setup_install(tmp_path, monkeypatch)
    backup = target.with_name(target.name + ".pdf-assistant-v117.backup")
    backup.write_bytes(source)
    old = b"exact previously qualified fixture patch"
    monkeypatch.setattr(adapter, "PREVIOUS_PATCH_SHA256", hashlib.sha256(old).hexdigest())
    target.write_bytes(old)
    result = adapter.ensure_v117_embedding_server(report(executable))
    assert result["installed"] and result["restart_required"]
    assert target.read_text() == adapter.patch_v117_server_source(source.decode())
    assert backup.read_bytes() == source


def test_prior_patch_without_pristine_backup_is_refused(tmp_path, monkeypatch):
    target, executable, _ = setup_install(tmp_path, monkeypatch)
    old = b"exact previously qualified fixture patch"
    monkeypatch.setattr(adapter, "PREVIOUS_PATCH_SHA256", hashlib.sha256(old).hexdigest())
    target.write_bytes(old)
    assert not adapter.ensure_v117_embedding_server(report(executable))["installed"]
    assert target.read_bytes() == old


def test_generated_signer_reuses_key_and_invalidates_rotations(monkeypatch):
    source = fixture_source()
    monkeypatch.setattr(adapter, "V117_SERVER_SHA256", hashlib.sha256(source.encode()).hexdigest())
    script = r'''
const assert=require('assert'),crypto=require('crypto');let derivations=0;
class Xs{constructor(){derivations++;process.env.SIG_KEY ||= 'initialized-key';process.env.SIG_SALT ||= 'initialized-salt';this.xPayload=crypto.scryptSync(process.env.SIG_KEY,process.env.SIG_SALT,32).toString('base64')}}
'''
    script += adapter.patch_v117_server_source(source)
    script += r'''
(async()=>{
process.env.SIG_KEY='key-1';process.env.SIG_SALT='salt-1';
async function verify(count,expectedDerivations){
 const values=await Promise.all(Array.from({length:count},()=>new Collector().processDocument('file')));
 const expected=crypto.scryptSync(process.env.SIG_KEY,process.env.SIG_SALT,32).toString('base64');
 assert(values.every(value=>value===expected));assert.equal(derivations,expectedDerivations);
}
await verify(38,1);
process.env.SIG_KEY='key-2';await verify(3,2);
process.env.SIG_SALT='salt-2';await verify(3,3);
delete process.env.SIG_KEY;delete process.env.SIG_SALT;await verify(3,4);
await verify(3,4);
process.env.SIG_KEY='';await verify(3,5);
console.log(JSON.stringify({passed:true,requests:53,derivations}));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True, "requests": 53, "derivations": 5}


@pytest.mark.parametrize("translation_bytes", [0, 3, 6, 8])
def test_native_resource_translation_bounds_and_language_fallback(monkeypatch, translation_bytes):
    translations = (wintypes.WORD * 4)(0x409, 1200, 0x413, 1200)
    version = ctypes.create_unicode_buffer("1.18.0-beta.2")
    queries = []

    def query(buffer, name, pointer, length):
        queries.append(name)
        if name == "\\VarFileInfo\\Translation":
            pointer._obj.value = ctypes.addressof(translations)
            length._obj.value = translation_bytes
            return True
        if name == "\\StringFileInfo\\041304b0\\ProductVersion":
            pointer._obj.value = ctypes.addressof(version)
            length._obj.value = len(version)
            return True
        return False

    library = SimpleNamespace(
        GetFileVersionInfoSizeW=Mock(return_value=64),
        GetFileVersionInfoW=Mock(return_value=True),
        VerQueryValueW=Mock(side_effect=query),
    )
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=library), raising=False)
    if translation_bytes != 8:
        with pytest.raises(ValueError, match="malformed"):
            compatibility._native_product_version(Path("fixture.exe"))
        assert queries == ["\\VarFileInfo\\Translation"]
    else:
        assert compatibility._native_product_version(Path("fixture.exe")) == "1.18.0-beta.2"
        assert queries[-2:] == ["\\StringFileInfo\\040904b0\\ProductVersion", "\\StringFileInfo\\041304b0\\ProductVersion"]
