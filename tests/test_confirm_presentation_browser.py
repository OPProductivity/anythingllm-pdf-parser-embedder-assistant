"""Opt-in client-only browser regression; no PDF or backend is processed."""
import ast
import json
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.ui_local


def test_native_and_wrapped_confirm_acknowledge_before_target_handler(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("The opt-in browser test requires Node.js and Playwright")
    root = Path(__file__).resolve().parent.parent
    constants = {}
    for statement in ast.parse((root / "rag_pdf_gradio_app.py").read_text("utf8")).body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            name = getattr(statement.targets[0], "id", None)
            if name in {"APP_JS", "APP_CSS"}:
                constants[name] = ast.literal_eval(statement.value)
    assert set(constants) == {"APP_JS", "APP_CSS"}
    fixture = tmp_path / "presentation-source.json"
    fixture.write_text(json.dumps(constants), encoding="utf8")
    result = subprocess.run(
        [node, str(root / "tests/ui/confirm_presentation.cjs"), str(fixture)],
        capture_output=True, text=True, encoding="utf8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
