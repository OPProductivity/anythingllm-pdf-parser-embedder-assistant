"""Client-only metadata status behavior while preview callbacks are queued."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.ui_local


def test_selected_pdf_metadata_status_is_visible_until_preview_finishes(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("This local browser test requires Node.js and Playwright")
    root = Path(__file__).resolve().parent.parent
    from rag_pdf_gradio_app import APP_BROWSER_THEME_HEAD, APP_CSS

    fixture = tmp_path / "metadata-status-source.json"
    fixture.write_text(json.dumps({"APP_BROWSER_THEME_HEAD": APP_BROWSER_THEME_HEAD,
                                   "APP_CSS": APP_CSS}), encoding="utf8")
    result = subprocess.run(
        [node, str(root / "tests/ui/metadata_status.cjs"), str(fixture)],
        capture_output=True, text=True, encoding="utf8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
