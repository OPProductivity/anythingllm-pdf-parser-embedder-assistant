import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from scripts.build_verified_release import build_release

pytestmark = pytest.mark.offline_deterministic


def release(tmp_path):
    source = tmp_path / "wheelhouse"
    source.mkdir()
    wheel = source / "anythingllm_pdf_assistant-0.5.7-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("anythingllm_pdf_assistant-0.5.7.dist-info/METADATA",
                         "Metadata-Version: 2.1\nName: anythingllm-pdf-assistant\nVersion: 0.5.7\n")
    destination = tmp_path / "release.zip"
    sha = build_release(source, destination, "pdf-assistant-wheels", "3.14")
    return destination, sha


def test_bundle_member_hashes_and_exclusive_publication(tmp_path):
    destination, sha = release(tmp_path)
    assert sha == hashlib.sha256(destination.read_bytes()).hexdigest()
    with zipfile.ZipFile(destination) as archive:
        manifest = json.loads(archive.read("release-manifest.json"))
        for member in manifest["files"]:
            assert hashlib.sha256(archive.read(member["path"])).hexdigest() == member["sha256"]
    with pytest.raises(FileExistsError):
        build_release(tmp_path / "wheelhouse", destination, "pdf-assistant-wheels", "3.14")
    assert sha == hashlib.sha256(destination.read_bytes()).hexdigest()


def test_powershell_verify_only_and_wrong_hash_execute_no_release_code(tmp_path):
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell unavailable")
    destination, sha = release(tmp_path)
    installer = Path(__file__).resolve().parents[1] / "Install-AnythingLLMPdfAssistant.ps1"
    command = [shell, "-NoProfile", "-File", str(installer), "-BundlePath", str(destination),
               "-VerifyOnly", "-BundleSha256"]
    valid = subprocess.run(command + [sha], capture_output=True, text=True, timeout=30)
    assert valid.returncode == 0, valid.stderr
    assert "No release code was executed" in valid.stdout
    invalid = subprocess.run(command + ["0" * 64], capture_output=True, text=True, timeout=30)
    assert invalid.returncode != 0
    assert "Release hash mismatch" in invalid.stderr
