"""Preserve the intended raster reading during bounded external OCR stalls."""

import subprocess
from types import SimpleNamespace

import fitz
import pytest

from author_metadata import evidence_collection as acquisition

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("primary_duration", [12, 25, 31])
def test_slow_primary_read_is_not_replaced_before_its_deadline(
    tmp_path, monkeypatch, primary_duration
):
    executable = tmp_path / "ocr-provider"
    executable.write_text("controlled external provider", encoding="utf8")
    calls = []

    def recognize(command, **kwargs):
        # Model elapsed external work without sleeping in the test suite.
        # A genuine deadline exhaustion must still take the bounded fallback.
        assert 0 < kwargs["timeout"] <= 30
        assert kwargs["env"]["OMP_THREAD_LIMIT"] == "1"
        calls.append((command, kwargs["input"]))
        if command[command.index("--psm") + 1] == "3":
            if primary_duration > kwargs["timeout"]:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return SimpleNamespace(stdout=b"By Amelia Hart")
        return SimpleNamespace(stdout=b"By Amella Hart")

    monkeypatch.setattr(acquisition.subprocess, "run", recognize)
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((50, 70), "By Amelia Hart")
        task = {
            "page": 1,
            "clip": [0, 0, page.rect.width, page.rect.height],
            "reason": "invisible_native_text_overlay",
        }
        readings, attempts = acquisition._read_region(document, task, executable)

    if primary_duration <= 25:
        assert len(calls) == 1
        assert readings[0]["text"] == "By Amelia Hart"
        assert readings[0]["psm"] == 3
        assert [a["status"] for a in attempts] == ["read"]
    else:
        assert len(calls) == 2
        assert readings[0]["text"] == "By Amella Hart"
        assert readings[0]["psm"] == 6
        assert [a["status"] for a in attempts] == ["failed", "read"]
        assert "timed out" in attempts[0]["error"]
        assert attempts[1]["reason"] == "transient_read_retry"
