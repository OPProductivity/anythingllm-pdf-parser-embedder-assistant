r"""Opt-in browser acceptance tests for the localhost Gradio application.

Run explicitly with:
  .\.venv\Scripts\python.exe -m pytest -m ui_local --browser chromium tests/test_ui_acceptance.py

The suite starts separate app instances and never contacts AnythingLLM Desktop.
The optional real-source test reads only root-level PDFs from an explicitly
provided private directory and keeps all generated state in pytest's temp area.
"""

from __future__ import annotations

import os
import json
import re
import secrets
import socket
import subprocess
import sys
import time
import http.client
import urllib.request
from pathlib import Path

import fitz
import pytest
from browser_access import browser_ticket

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "rag_pdf_gradio_app.py"

pytestmark = pytest.mark.ui_local
LOCAL_BROWSER_KEY = secrets.token_urlsafe(32)


def open_local_app(page, base_url):
    # A fresh ticket is required for every browser session; it is consumed once.
    page.goto(f"{base_url}/assistant-browser-session#{browser_ticket(LOCAL_BROWSER_KEY)}")
    expect(page.locator(".pdf-upload-input input[type='file']")).to_have_count(1, timeout=30000)


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_http(url: str, process: subprocess.Popen[bytes], timeout_seconds: float = 45) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Local Gradio app stopped early with exit code {process.returncode}.")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if 200 <= int(response.status) < 500:
                    return
        except (OSError, http.client.HTTPException):
            time.sleep(0.25)
    raise TimeoutError(f"Local Gradio app did not listen at {url} within {timeout_seconds}s.")


@pytest.fixture(scope="module")
def local_app_home(tmp_path_factory):
    return tmp_path_factory.mktemp("gradio-local-home")


@pytest.fixture(scope="module")
def local_app_url(tmp_path_factory, local_app_home):
    port = free_local_port()
    url = f"http://127.0.0.1:{port}"
    environment = dict(os.environ)
    environment["GRADIO_SERVER_PORT"] = str(port)
    environment["ANYTHINGLLM_PDF_ASSISTANT_BROWSER_KEY"] = LOCAL_BROWSER_KEY
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["ANYTHINGLLM_PDF_ASSISTANT_HOME"] = str(local_app_home)
    environment["NO_PROXY"] = environment["no_proxy"] = "localhost,127.0.0.1"
    stderr_path = tmp_path_factory.mktemp("gradio-local-app") / "stderr.log"
    with stderr_path.open("wb") as stderr_handle:
        process = subprocess.Popen(
            [sys.executable, str(APP_PATH)],
            cwd=str(PROJECT_ROOT),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=stderr_handle,
        )
        try:
            wait_for_http(url + "/healthz", process)
            yield url
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    assert "didn't receive enough input values" not in stderr_text


@pytest.fixture
def paused_metadata_app(tmp_path, tmp_path_factory):
    """Hold one real preview at inference so editing during scanning is deterministic."""
    port = free_local_port()
    app_home = tmp_path_factory.mktemp("edit-home")
    started, release = tmp_path / "scan-started", tmp_path / "scan-release"
    environment = dict(os.environ)
    environment.update({
        "GRADIO_SERVER_PORT": str(port),
        "ANYTHINGLLM_PDF_ASSISTANT_BROWSER_KEY": LOCAL_BROWSER_KEY,
        "ANYTHINGLLM_PDF_ASSISTANT_HOME": str(app_home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    })
    script = """
import sys, time
from pathlib import Path
import rag_pdf_gradio_app as app
started, release = (Path(p) for p in sys.argv[1:3])
infer = app.infer_author_from_initial_pdf_pages
def paused_infer(*args, **kwargs):
    started.touch()
    deadline = time.monotonic() + 60
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Browser test did not release metadata preview')
        time.sleep(0.05)
    return infer(*args, **kwargs)
app.infer_author_from_initial_pdf_pages = paused_infer
app.launch_application(port=int(sys.argv[3]), inbrowser=False)
"""
    with (tmp_path / "app-stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(started), str(release), str(port)],
            cwd=str(PROJECT_ROOT), env=environment,
            stdout=subprocess.DEVNULL, stderr=stderr,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            wait_for_http(url + "/healthz", process)
            yield url, started, release, app_home
        finally:
            release.touch()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)


@pytest.fixture
def one_page_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "browser-acceptance.pdf"
    document = fitz.open()
    try:
        page = document.new_page()
        page.insert_text((72, 72), "Browser acceptance PDF: fresh run state must be empty.")
        document.save(path)
    finally:
        document.close()
    return path


@pytest.fixture
def paused_confirm_preflight_app(tmp_path, tmp_path_factory):
    """Hold confirmed OCR preflight before it can reserve a run root."""
    port = free_local_port()
    app_home = tmp_path_factory.mktemp("cancel-preflight-home")
    started, release = tmp_path / "preflight-started", tmp_path / "preflight-release"
    environment = dict(os.environ)
    environment.update({
        "GRADIO_SERVER_PORT": str(port),
        "ANYTHINGLLM_PDF_ASSISTANT_BROWSER_KEY": LOCAL_BROWSER_KEY,
        "ANYTHINGLLM_PDF_ASSISTANT_HOME": str(app_home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    })
    script = """
import sys, time
from pathlib import Path
import rag_pdf_gradio_app as app
started, release = (Path(p) for p in sys.argv[1:3])
original = app.automatic_ocr_preflight_manifest
def held_preflight(*args, **kwargs):
    started.touch()
    deadline = time.monotonic() + 60
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Browser test did not release confirmed preflight')
        time.sleep(0.05)
    return original(*args, **kwargs)
app.automatic_ocr_preflight_manifest = held_preflight
app.launch_application(port=int(sys.argv[3]), inbrowser=False)
"""
    with (tmp_path / "preflight-stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(started), str(release), str(port)],
            cwd=str(PROJECT_ROOT), env=environment,
            stdout=subprocess.DEVNULL, stderr=stderr,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            wait_for_http(url + "/healthz", process)
            yield url, started, release, app_home
        finally:
            release.touch()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)


@pytest.fixture
def paused_auto_handoff_app(tmp_path, tmp_path_factory):
    """Hold the final preview result and record the run's actual server inputs."""
    port = free_local_port()
    app_home = tmp_path_factory.mktemp("auto-handoff-home")
    scan_started = tmp_path / "handoff-scan-started"
    scan_release = tmp_path / "handoff-scan-release"
    preflight_started = tmp_path / "handoff-preflight-started"
    preflight_release = tmp_path / "handoff-preflight-release"
    payload = tmp_path / "handoff-payload.json"
    environment = dict(os.environ)
    environment.update({
        "GRADIO_SERVER_PORT": str(port),
        "ANYTHINGLLM_PDF_ASSISTANT_BROWSER_KEY": LOCAL_BROWSER_KEY,
        "ANYTHINGLLM_PDF_ASSISTANT_HOME": str(app_home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    })
    script = """
import json, sys, time
from pathlib import Path
import rag_pdf_gradio_app as app
scan_started, scan_release, preflight_started, preflight_release, payload = (
    Path(p) for p in sys.argv[1:6]
)
def wait_for_release(started, release):
    started.touch()
    deadline = time.monotonic() + 90
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Browser test did not release handoff gate')
        time.sleep(0.05)
infer = app.infer_author_from_initial_pdf_pages
def held_infer(*args, **kwargs):
    wait_for_release(scan_started, scan_release)
    return infer(*args, **kwargs)
app.infer_author_from_initial_pdf_pages = held_infer
preflight = app.automatic_ocr_preflight_manifest
def held_preflight(*args, **kwargs):
    wait_for_release(preflight_started, preflight_release)
    return preflight(*args, **kwargs)
app.automatic_ocr_preflight_manifest = held_preflight
run = app.run_automatic_for_browser_stream
def captured_run(selection_state, viewed_run_root, *values, **kwargs):
    settings = dict(zip(app.AUTOMATIC_RUN_FIELDS, values))
    selected = settings.get('pdf_files') or []
    payload.write_text(json.dumps({
        'revision': selection_state.get('revision'),
        'files': [Path(str(p)).name for p in selected],
        'mode': settings.get('mode'),
        'output_root_override': settings.get('output_root_override'),
        'segment_mode': settings.get('segment_mode'),
        'include_front_matter': settings.get('include_front_matter'),
        'include_back_matter': settings.get('include_back_matter'),
        'first_page_override': settings.get('first_page_override'),
        'end_page_override': settings.get('end_page_override'),
    }), encoding='utf-8')
    yield from run(selection_state, viewed_run_root, *values, **kwargs)
app.run_automatic_for_browser_stream = captured_run
app.launch_application(port=int(sys.argv[6]), inbrowser=False)
"""
    with (tmp_path / "handoff-stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(scan_started), str(scan_release),
             str(preflight_started), str(preflight_release), str(payload), str(port)],
            cwd=str(PROJECT_ROOT), env=environment,
            stdout=subprocess.DEVNULL, stderr=stderr,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            wait_for_http(url + "/healthz", process)
            yield url, scan_started, scan_release, preflight_started, preflight_release, payload
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)
            scan_release.touch()
            preflight_release.touch()


@pytest.fixture
def batch_metadata_app(tmp_path, tmp_path_factory, two_pdf_files):
    """Exercise real folder/direct paths with only the second preview paused."""
    port = free_local_port()
    app_home = tmp_path_factory.mktemp("batch-edit")
    started, release = tmp_path / "second-started", tmp_path / "second-release"
    environment = dict(os.environ)
    environment.update({
        "GRADIO_SERVER_PORT": str(port),
        "ANYTHINGLLM_PDF_ASSISTANT_BROWSER_KEY": LOCAL_BROWSER_KEY,
        "ANYTHINGLLM_PDF_ASSISTANT_HOME": str(app_home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    })
    script = """
import sys, time
from pathlib import Path
import rag_pdf_gradio_app as app
folder, started, release = (Path(p) for p in sys.argv[1:4])
app.choose_pdf_input_directory = lambda *_a, **_kw: str(folder)
infer = app.infer_author_from_initial_pdf_pages
calls = 0
def paused_infer(*args, **kwargs):
    global calls
    calls += 1
    if calls == 2:
        started.touch()
        deadline = time.monotonic() + 60
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Browser test did not release second preview')
            time.sleep(0.05)
    return infer(*args, **kwargs)
app.infer_author_from_initial_pdf_pages = paused_infer
app.launch_application(port=int(sys.argv[4]), inbrowser=False)
"""
    with (tmp_path / "batch-app-stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(two_pdf_files[0].parent), str(started), str(release), str(port)],
            cwd=str(PROJECT_ROOT), env=environment, stdout=subprocess.DEVNULL, stderr=stderr,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            wait_for_http(url + "/healthz", process)
            yield url, started, release, app_home, two_pdf_files
        finally:
            release.touch()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)


@pytest.fixture
def two_pdf_files(tmp_path: Path) -> list[Path]:
    paths = [tmp_path / "browser-first.pdf", tmp_path / "browser-second.pdf"]
    for index, path in enumerate(paths, start=1):
        document = fitz.open()
        try:
            title = "Harbor Studies Alpha" if index == 1 else "Meadow Studies Beta"
            author = "Ada Miriam West" if index == 1 else "Clara Ellen North"
            document.set_metadata({"title": title, "author": author})
            page = document.new_page()
            page.insert_text((72, 72), title + "\nby " + author + "\n\n" + "\n".join(
                f"Browser multi-file acceptance PDF {index}, native text line {line}."
                for line in range(1, 12)
            ))
            document.save(path)
        finally:
            document.close()
    return paths


@pytest.fixture
def six_pdf_files(tmp_path: Path) -> list[Path]:
    paths = [tmp_path / f"browser-batch-{index}.pdf" for index in range(1, 7)]
    for index, path in enumerate(paths, start=1):
        document = fitz.open()
        try:
            page = document.new_page()
            text = "\n".join(
                f"Browser six-file batch PDF {index}, native text line {line}. "
                "This paragraph provides enough ordinary selectable text for the native path."
                for line in range(1, 18)
            )
            page.insert_textbox((72, 72, 540, 760), text, fontsize=10)
            document.save(path)
        finally:
            document.close()
    return paths


@pytest.fixture
def forty_two_pdf_files(tmp_path: Path) -> list[Path]:
    paths = [tmp_path / f"browser-large-batch-{index:02d}.pdf" for index in range(1, 43)]
    for index, path in enumerate(paths, start=1):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 72), f"Browser batch source {index}\nby Test Author {index}")
            document.save(path)
        finally:
            document.close()
    return paths


@pytest.fixture
def optional_real_pdf_files() -> list[Path]:
    root = os.environ.get("MMT_REAL_PDF_SOURCE_DIR", "")
    if not root:
        pytest.skip("Set MMT_REAL_PDF_SOURCE_DIR for the opt-in real-source UI test")
    directory = Path(root)
    assert directory.is_dir()
    files = sorted((path for path in directory.iterdir()
                    if path.is_file() and path.suffix.casefold() == ".pdf"),
                   key=lambda path: (path.stat().st_size, path.name))[:8]
    assert len(files) >= 6
    return files


def test_large_batch_auto_confirm_handoff_timing(
    page, paused_confirm_preflight_app, forty_two_pdf_files
):
    url, started, release, _app_home = paused_confirm_preflight_app
    open_local_app(page, url)
    page.get_by_role("radio", name="Create local files only").check()
    began = time.monotonic()
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in forty_two_pdf_files]
    )
    page.get_by_role("checkbox", name="Process metadata for all files").check()
    page.get_by_role("checkbox", name="Confirm and start run automatically").check()
    transitions = []
    last = None
    metadata_ready_at = None
    deadline = time.monotonic() + 240
    try:
        while not started.exists() and time.monotonic() < deadline:
            state = page.evaluate("""() => {
              const m = document.querySelector('#document-metadata-scan-status .metadata-scan-inline');
              const a = document.querySelector('.automatic-run-activity');
              const b = document.querySelector('#confirm-automatic-run-button button');
              return [m?.dataset.state, m?.dataset.previewScope,
                m?.dataset.previewComplete, a?.dataset.runState,
                b?.disabled, document.querySelector('#metadata-auto-confirm-control input')?.checked];
            }""")
            if state != last:
                transitions.append((round(time.monotonic() - began, 1), state))
                last = state
            if state[:3] == ["ready", "all", "true"] and metadata_ready_at is None:
                metadata_ready_at = time.monotonic()
            page.wait_for_timeout(200)
        assert started.exists(), "Automatic Confirm did not reach preflight"
        assert metadata_ready_at is not None, "All-file metadata never completed"
        assert sum(state[0:2] == ["scanning", "all"]
                   for _, state in transitions) == 1, transitions
        assert time.monotonic() - metadata_ready_at < 30, transitions
        expect(page.locator("#confirm-automatic-run-button")).to_have_text("Processing…")
    finally:
        release.touch()


def test_auto_confirm_uses_server_handoff_without_dom_click(
    page, paused_confirm_preflight_app, six_pdf_files
):
    url, started, release, _app_home = paused_confirm_preflight_app
    open_local_app(page, url)
    page.get_by_role("radio", name="Create local files only").check()
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    page.get_by_role("checkbox", name="Process metadata for all files").check()
    page.wait_for_function(
        "(() => { const s = document.querySelector('#document-metadata-scan-status .metadata-scan-inline'); "
        "return s?.dataset.state === 'ready' && s?.dataset.previewScope === 'all' "
        "&& s?.dataset.previewComplete === 'true' "
        "&& document.querySelector('.automatic-run-activity')?.dataset.runState === 'ready'; })()",
        timeout=90000,
    )
    page.evaluate("""() => {
      window.__autoConfirmButtonReplacements = 0;
      const originalClick = HTMLButtonElement.prototype.click;
      HTMLButtonElement.prototype.click = function(...args) {
        if (this.closest('#confirm-automatic-run-button') || this.id === 'confirm-automatic-run-button') {
          window.__autoConfirmButtonReplacements += 1;
        }
        return originalClick.apply(this, args);
      };
    }""")
    try:
        page.get_by_role("checkbox", name="Confirm and start run automatically").check()
        deadline = time.monotonic() + 12
        while not started.exists() and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert page.evaluate("window.__autoConfirmButtonReplacements") == 0
        assert started.exists(), "Server did not accept the automatic handoff"
    finally:
        release.touch()


def test_auto_confirm_stays_armed_across_setting_changes_and_cancel(
    page, paused_metadata_app, six_pdf_files
):
    url, started, release, app_home = paused_metadata_app
    open_local_app(page, url)
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    page.wait_for_function(
        "(() => { const s = document.querySelector('#document-metadata-scan-status .metadata-scan-inline'); "
        "return s?.dataset.state === 'ready' && s?.dataset.previewScope === 'none' "
        "&& document.querySelector('.automatic-run-activity')?.dataset.runState === 'ready'; })()",
        timeout=60000,
    )
    page.get_by_role("radio", name="Create local files only").check()
    all_files = page.get_by_role("checkbox", name="Process metadata for all files")
    automatic = page.get_by_role("checkbox", name="Confirm and start run automatically")
    all_files.check()
    deadline = time.monotonic() + 30
    while not started.exists() and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert started.exists(), "Explicit all-file preview did not start"
    try:
        automatic.check()
        page.get_by_role("radio", name="Create local files with diagnostic logs").check()
        page.get_by_role("checkbox", name="Include foreword/preface").uncheck()
        page.get_by_role("checkbox", name="Include bibliography/index/end notes").uncheck()
        page.get_by_role("checkbox", name="Force Unstructured (instead of automatic)").check()
        page.get_by_label("Segmentation mode", exact=True).first.click()
        page.get_by_role("option", name="All in one file").click()
        page.get_by_role("button", name=re.compile(r"^Advanced preparation overrides")).click()
        page.get_by_label("First PDF page override", exact=True).fill("1")
        page.get_by_label("End-matter start override", exact=True).fill("8")
        page.wait_for_timeout(800)
        expect(all_files).to_be_checked()
        expect(automatic).to_be_checked()
        expect(page.locator(".metadata-header-status")).to_have_attribute("data-state", "scanning")
        expect(page.get_by_role("radio", name="Create local files with diagnostic logs")).to_be_checked()
        page.get_by_role("button", name="Cancel").click()
        expect(automatic).not_to_be_checked()
        release.touch()
        page.wait_for_timeout(900)
        assert not list((app_home / "run-state").rglob("source-profile.json"))
    finally:
        release.touch()


def test_auto_handoff_reads_latest_settings_at_metadata_completion(
    page, paused_auto_handoff_app, six_pdf_files
):
    (url, scan_started, scan_release, preflight_started,
     preflight_release, payload) = paused_auto_handoff_app
    open_local_app(page, url)
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    all_files = page.get_by_role("checkbox", name="Process metadata for all files")
    automatic = page.get_by_role("checkbox", name="Confirm and start run automatically")
    all_files.check()
    automatic.check()
    try:
        deadline = time.monotonic() + 30
        while not scan_started.exists():
            assert time.monotonic() < deadline, "Full metadata preview did not start"
            page.wait_for_timeout(100)
        page.get_by_role("radio", name="Create local files with diagnostic logs").check()
        page.get_by_role("checkbox", name="Include foreword/preface").uncheck()
        page.get_by_role("checkbox", name="Include bibliography/index/end notes").uncheck()
        page.get_by_label("Segmentation mode", exact=True).first.click()
        page.get_by_role("option", name="All in one file").click()
        page.get_by_role("button", name=re.compile(r"^Advanced preparation overrides")).click()
        page.get_by_label("First PDF page override", exact=True).fill("1")
        page.get_by_label("End-matter start override", exact=True).fill("8")
        expect(all_files).to_be_checked()
        expect(automatic).to_be_checked()
        scan_release.touch()
        deadline = time.monotonic() + 90
        while not preflight_started.exists():
            assert time.monotonic() < deadline, "Server did not enter confirmed preflight"
            page.wait_for_timeout(100)
        actual = json.loads(payload.read_text(encoding="utf-8"))
        assert len(actual["files"]) == 6
        assert actual["mode"] == "Create local files with diagnostic logs"
        assert actual["segment_mode"] == "All in one file"
        assert actual["include_front_matter"] is False
        assert actual["include_back_matter"] is False
        assert actual["first_page_override"] == 1
        assert actual["end_page_override"] == 8
        expect(automatic).to_be_checked()
        expect(page.locator("#confirm-automatic-run-button")).to_have_text("Processing…")
    finally:
        scan_release.touch()
        preflight_release.touch()


def test_auto_start_without_metadata_preview_uses_custom_output_folder(
    page, paused_auto_handoff_app, six_pdf_files, tmp_path
):
    (url, _scan_started, _scan_release, preflight_started,
     preflight_release, payload) = paused_auto_handoff_app
    open_local_app(page, url)
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    page.wait_for_function(
        "(() => { const s = document.querySelector('#document-metadata-scan-status .metadata-scan-inline'); "
        "return s?.dataset.state === 'ready' && s?.dataset.previewScope === 'none' "
        "&& document.querySelector('.automatic-run-activity')?.dataset.runState === 'ready'; })()",
        timeout=60000,
    )
    page.get_by_role("radio", name="Create local files only").check()
    chosen_output = tmp_path / "chosen-output"
    output = page.get_by_label("(Change) Output Folder Path")
    output.fill(str(chosen_output))
    output.press("Tab")
    metadata = page.get_by_role("checkbox", name="Process metadata for all files")
    automatic = page.get_by_role("checkbox", name="Confirm and start run automatically")
    expect(metadata).not_to_be_checked()
    try:
        automatic.check()
        deadline = time.monotonic() + 30
        while not preflight_started.exists():
            assert time.monotonic() < deadline, "Auto-start did not reach preflight"
            page.wait_for_timeout(100)
        actual = json.loads(payload.read_text(encoding="utf-8"))
        assert actual["mode"] == "Create local files only"
        assert Path(actual["output_root_override"]) == chosen_output
        assert len(actual["files"]) == 6
        expect(metadata).not_to_be_checked()
        expect(automatic).to_be_checked()
        expect(page.locator("#confirm-automatic-run-button")).to_have_text("Processing…")
        page.get_by_role("button", name="Cancel").click()
    finally:
        preflight_release.touch()


@pytest.mark.parametrize("mode", [
    "Create local files only",
    "Create local files with diagnostic logs",
    "Create local files and upload to AnythingLLM",
])
@pytest.mark.parametrize("preview_all", [False, True])
def test_all_output_modes_keep_auto_start_and_cancel_consistent(
    page, paused_auto_handoff_app, six_pdf_files, mode, preview_all
):
    (url, scan_started, scan_release, preflight_started,
     preflight_release, payload) = paused_auto_handoff_app
    open_local_app(page, url)
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    page.wait_for_function(
        "(() => { const s = document.querySelector('#document-metadata-scan-status .metadata-scan-inline'); "
        "return s?.dataset.state === 'ready' && s?.dataset.previewScope === 'none' "
        "&& document.querySelector('.automatic-run-activity')?.dataset.runState === 'ready'; })()",
        timeout=60000,
    )
    page.get_by_role("radio", name=mode, exact=True).check()
    metadata = page.get_by_role("checkbox", name="Process metadata for all files")
    automatic = page.get_by_role("checkbox", name="Confirm and start run automatically")
    expect(metadata).not_to_be_checked()
    if preview_all:
        metadata.check()
        deadline = time.monotonic() + 30
        while not scan_started.exists():
            assert time.monotonic() < deadline, "Metadata preview did not start"
            page.wait_for_timeout(100)
    automatic.check()
    if preview_all:
        assert not preflight_started.exists(), "Auto-start bypassed requested metadata preview"
        scan_release.touch()
    deadline = time.monotonic() + 30
    while not preflight_started.exists():
        assert time.monotonic() < deadline, "Auto-start did not reach preflight"
        page.wait_for_timeout(100)
    actual = json.loads(payload.read_text(encoding="utf-8"))
    assert actual["mode"] == mode
    assert len(actual["files"]) == 6
    if preview_all:
        expect(metadata).to_be_checked()
    else:
        expect(metadata).not_to_be_checked()
    expect(automatic).to_be_checked()
    expect(page.locator("#confirm-automatic-run-button")).to_have_text("Processing…")
    # Let the one-second status observer repaint the action row. It must not
    # disable Cancel while confirmed preflight is still waiting at this gate.
    page.wait_for_timeout(1300)
    cancel = page.locator("#cancel-automatic-run-button")
    expect(cancel).to_be_enabled(timeout=5000)
    cancel.click()
    expect(cancel).to_be_disabled()
    expect(page.locator(".automatic-run-activity")).to_have_attribute(
        "data-cancel-requested", "true"
    )


def test_metadata_preview_only_does_not_arm_auto_start(
    page, paused_metadata_app, six_pdf_files
):
    url, started, release, app_home = paused_metadata_app
    open_local_app(page, url)
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    page.wait_for_function(
        "(() => { const s = document.querySelector('#document-metadata-scan-status .metadata-scan-inline'); "
        "return s?.dataset.state === 'ready' && s?.dataset.previewScope === 'none' "
        "&& document.querySelector('.automatic-run-activity')?.dataset.runState === 'ready'; })()",
        timeout=60000,
    )
    metadata = page.get_by_role("checkbox", name="Process metadata for all files")
    automatic = page.get_by_role("checkbox", name="Confirm and start run automatically")
    try:
        metadata.check()
        deadline = time.monotonic() + 30
        while not started.exists():
            assert time.monotonic() < deadline, "Metadata preview did not start"
            page.wait_for_timeout(100)
        expect(automatic).not_to_be_checked()
        release.touch()
        page.wait_for_function(
            "(() => { const s = document.querySelector('#document-metadata-scan-status .metadata-scan-inline'); "
            "return s?.dataset.state === 'ready' && s?.dataset.previewScope === 'all' "
            "&& s?.dataset.previewComplete === 'true'; })()",
            timeout=90000,
        )
        expect(automatic).not_to_be_checked()
        assert not list((app_home / "run-state").rglob("source-profile.json"))
    finally:
        release.touch()


def test_real_sources_settings_then_all_file_auto_confirm(
    page, paused_confirm_preflight_app, optional_real_pdf_files
):
    url, started, release, _app_home = paused_confirm_preflight_app
    open_local_app(page, url)
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in optional_real_pdf_files]
    )
    page.wait_for_function(
        "(() => { const s = document.querySelector('#document-metadata-scan-status .metadata-scan-inline'); "
        "return s?.dataset.state === 'ready' && s?.dataset.previewScope === 'none' "
        "&& document.querySelector('.automatic-run-activity')?.dataset.runState === 'ready'; })()",
        timeout=90000,
    )
    page.get_by_role("radio", name="Create local files only").check()
    page.get_by_role("checkbox", name="Include foreword/preface").uncheck()
    page.get_by_role("checkbox", name="Include bibliography/index/end notes").uncheck()
    all_files = page.get_by_role("checkbox", name="Process metadata for all files")
    automatic = page.get_by_role("checkbox", name="Confirm and start run automatically")
    try:
        all_files.check()
        automatic.check()
        deadline = time.monotonic() + 180
        while not started.exists() and time.monotonic() < deadline:
            page.wait_for_timeout(250)
        assert started.exists(), "Real-source automatic Confirm did not reach preflight"
        expect(page.get_by_role("radio", name="Create local files only")).to_be_checked()
        expect(page.get_by_role("checkbox", name="Include foreword/preface")).not_to_be_checked()
        expect(page.get_by_role("checkbox", name="Include bibliography/index/end notes")).not_to_be_checked()
        expect(page.locator("#document-metadata-scan-status .metadata-scan-inline")).to_have_attribute(
            "data-preview-scope", "all"
        )
        expect(automatic).to_be_checked()
        page.get_by_role("button", name="Cancel").click()
    finally:
        release.touch()


def test_fresh_file_selection_never_displays_prior_success(page, local_app_url, one_page_pdf):
    open_local_app(page, local_app_url)
    expect(page.get_by_text("Processing successful", exact=False)).to_have_count(0)
    expect(page.get_by_text("Overall progress: 100%", exact=False)).to_have_count(0)
    cancel = page.locator("#cancel-automatic-run-button")
    expect(cancel).to_be_visible()
    expect(cancel).to_be_disabled()


def test_local_only_mode_hides_upload_controls_even_when_advanced_is_open(page, local_app_url):
    """Mode changes must survive Gradio's lazy accordion mounting."""
    open_local_app(page, local_app_url)

    local_only = page.get_by_role("radio", name="Create local files only")
    expect(local_only).to_have_count(1)
    local_only.check()

    expect(page.get_by_text("Local-only run:", exact=True)).to_have_count(0)
    expect(page.locator("#native-metadata-upload-section")).to_be_hidden()
    expect(page.locator("#anythingllm-output-folder")).to_have_count(0)

    advanced = page.get_by_role("button", name="Advanced preparation overrides ▼")
    expect(advanced).to_have_count(1)
    advanced.click()
    expect(local_only).to_be_checked()
    expect(page.get_by_label("Extraction backend", exact=True)).to_have_count(0)
    # The future-defaults editor intentionally has its own API URL default.
    # Scope this assertion to the active run's native controls instead of
    # treating that stored-default field as a visible upload control.
    run_api_url = page.locator("#anythingllm-run-api-controls").get_by_label(
        "AnythingLLM API URL", exact=True
    )
    expect(run_api_url).to_be_hidden()
    expect(page.get_by_label("AnythingLLM chunk size", exact=True)).to_be_hidden()
    expect(page.get_by_role("checkbox", name="Auto-apply before upload run")).to_be_hidden()

    upload_mode = page.get_by_role("radio", name="Create local files and upload to AnythingLLM")
    expect(upload_mode).to_have_count(1)
    upload_mode.check()

    expect(page.locator("#native-metadata-upload-section")).to_be_visible(timeout=5000)
    expect(run_api_url).to_be_visible(timeout=5000)
    expect(page.get_by_label("AnythingLLM chunk size", exact=True)).to_be_visible()
    expect(page.get_by_role("checkbox", name="Auto-apply before upload run")).to_be_visible()


def test_compact_local_only_mode_remains_a_visible_choice(page, local_app_url):
    open_local_app(page, local_app_url)

    compact_local_only = page.get_by_role("radio", name="Create local files only")
    expect(compact_local_only).to_have_count(1)
    compact_local_only.check()
    expect(compact_local_only).to_be_checked()
    expect(page.locator("#native-metadata-upload-section")).to_be_hidden()
    expect(page.get_by_label("AnythingLLM API URL", exact=True)).to_have_count(0)


def test_pdf_picker_exposes_only_the_native_pdf_accept_filter(page, local_app_url):
    """Exercise Gradio's browser-to-native-picker contract, not just source text.

    ``file_types=[".pdf"]`` must render as ``accept=".pdf"`` on the actual
    browser file input.  The browser uses that attribute to construct the
    Windows Open-dialog filter; it is also intentionally a multiple-file
    picker for the batch workflow.
    """
    open_local_app(page, local_app_url)
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    expect(upload).to_have_attribute("accept", ".pdf")
    expect(upload).to_have_attribute("multiple", "")

    visible_upload_action = page.locator(
        ".pdf-upload-input button[aria-label='Click to upload or drop files']"
    )
    expect(visible_upload_action).to_have_count(1)
    with page.expect_file_chooser() as chooser_info:
        visible_upload_action.click()
    assert chooser_info.value.is_multiple


def test_new_workspace_name_field_enforces_the_creation_length_limit(page, local_app_url):
    open_local_app(page, local_app_url)
    workspace_name = page.get_by_placeholder(
        "Optional: type a name, or leave blank to derive one after Confirm",
        exact=True,
    )

    expect(workspace_name).to_have_attribute("maxlength", "120")
    workspace_name.press_sequentially("W" * 125)
    expect(workspace_name).to_have_value("W" * 120)


def test_selecting_a_pdf_resets_a_prior_local_only_choice_to_the_new_run_defaults(
    page, local_app_url, one_page_pdf
):
    """A new selection is a new run, rather than a partially inherited run."""
    open_local_app(page, local_app_url)
    local_only = page.get_by_role("radio", name="Create local files only")
    expect(local_only).to_have_count(1)
    local_only.check()
    expect(page.get_by_text("Local-only run:", exact=True)).to_have_count(0)

    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files(str(one_page_pdf))

    upload_mode = page.get_by_role("radio", name="Create local files and upload to AnythingLLM")
    expect(upload_mode).to_be_checked(timeout=15000)
    expect(page.get_by_text("Local-only run:", exact=True)).to_have_count(0)
    expect(page.locator("#native-metadata-upload-section")).to_be_visible(timeout=15000)


def test_future_defaults_editor_does_not_change_a_selected_automatic_job(page, local_app_url, one_page_pdf):
    open_local_app(page, local_app_url)
    page.locator(".pdf-upload-input input[type='file']").set_input_files(str(one_page_pdf))
    expect(page.get_by_role("button", name="Confirm and start processing")).to_be_enabled(timeout=15000)
    local_only = page.get_by_role("radio", name="Create local files only")
    local_only.check()

    page.get_by_role("tab", name="Advanced").click()
    page.get_by_role("button", name="Edit future Automatic defaults").click()
    expect(page.locator("#future-automatic-defaults-editor").first).to_be_visible()
    expect(page.get_by_text("Editing future defaults", exact=True).first).to_be_visible()

    page.get_by_role("tab", name="Automatic").click()
    expect(local_only).to_be_checked()


def test_selected_pdf_exposes_confirm_and_prestart_cancel(page, local_app_url, one_page_pdf):
    """Selection permits cancelling preview work before Confirm."""
    open_local_app(page, local_app_url)
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files(str(one_page_pdf))

    confirm = page.get_by_role("button", name="Confirm and start processing")
    cancel = page.get_by_role("button", name="Cancel")
    expect(confirm).to_be_visible(timeout=15000)
    expect(confirm).to_be_enabled(timeout=20000)
    expect(cancel).to_be_visible()
    expect(cancel).to_be_enabled()
    expect(page.get_by_role("button", name="Review settings and continue")).to_be_hidden()
    expect(page.get_by_text("Settings reviewed", exact=True)).to_have_count(0)
    confirm_box = confirm.bounding_box()
    cancel_box = cancel.bounding_box()
    assert confirm_box is not None and cancel_box is not None
    assert confirm_box["x"] + confirm_box["width"] <= cancel_box["x"]
    assert confirm_box["y"] == pytest.approx(cancel_box["y"], abs=1)
    expect(page.get_by_text("Ready — Confirm to begin processing.")).to_be_visible()
    expect(page.get_by_text("Processing successful", exact=False)).to_have_count(0)
    expect(page.get_by_text("Overall progress: 100%", exact=False)).to_have_count(0)


def test_multiple_pdf_selection_keeps_both_files_in_the_pending_batch(page, local_app_url, two_pdf_files):
    open_local_app(page, local_app_url)
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files([str(path) for path in two_pdf_files])

    confirm = page.get_by_role("button", name="Confirm and start processing")
    expect(confirm).to_be_enabled(timeout=15000)
    expect(page.get_by_role("button", name="Remove this file")).to_have_count(2)
    expect(page.locator("#automatic-pdf-upload .rag-selected-pdf-count")).to_have_text(
        "2 PDFs selected", timeout=15000
    )
    placement = page.locator("#automatic-pdf-upload").evaluate(
        """root => {
          const badge = root.querySelector(".rag-selected-pdf-count");
          const rootBox = root.getBoundingClientRect();
          const badgeBox = badge.getBoundingClientRect();
          return {
            centreDelta: Math.abs(
              (badgeBox.left + badgeBox.width / 2) - (rootBox.left + rootBox.width / 2)
            ),
            nativeClearCount: root.querySelectorAll("button[aria-label='Clear']").length,
          };
        }"""
    )
    assert placement["centreDelta"] <= 2
    assert placement["nativeClearCount"] == 1


def test_multiple_pdf_transfer_does_not_replace_the_native_picker_before_local_copy_finishes(
    page, local_app_url, two_pdf_files
):
    """The native picker remains visible while Gradio transfers files."""
    open_local_app(page, local_app_url)
    upload = page.locator("#automatic-pdf-upload input[type='file']")
    expect(upload).to_have_count(1, timeout=15000)
    first_frame = page.evaluate(
        """async () => {
          const input = document.querySelector("#automatic-pdf-upload input[type='file']");
          const transfer = new DataTransfer();
          const payload = new Uint8Array(16 * 1024 * 1024);
          transfer.items.add(new File([payload], "browser-first.pdf", {type: "application/pdf"}));
          transfer.items.add(new File([payload], "browser-second.pdf", {type: "application/pdf"}));
          input.files = transfer.files;
          input.dispatchEvent(new Event("change", {bubbles: true}));
          await new Promise(requestAnimationFrame);
          const root = document.querySelector("#automatic-pdf-upload");
          return {
            pendingListPresent: Boolean(root.querySelector(".rag-pending-pdf-list")),
            replacementActionsPresent: Boolean(root.querySelector(".rag-pending-picker-actions")),
            rootText: root.textContent || "",
          };
        }"""
    )
    assert first_frame["pendingListPresent"] is False
    assert first_frame["replacementActionsPresent"] is False
    assert "Adding 2 files" not in first_frame["rootText"]


def test_six_pdf_selection_keeps_every_file_in_the_pending_batch(page, local_app_url, six_pdf_files):
    """The ordinary picker must not truncate a six-file selection before Confirm."""
    open_local_app(page, local_app_url)
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files([str(path) for path in six_pdf_files])

    expect(page.get_by_role("button", name="Confirm and start processing")).to_be_enabled(timeout=20000)
    expect(page.get_by_role("button", name="Remove this file")).to_have_count(6)
    for path in six_pdf_files:
        expect(page.locator(f".pdf-upload-input td.filename[aria-label='{path.name}']")).to_have_count(1)


def test_six_pdf_local_batch_prepares_every_selected_source(page, local_app_url, six_pdf_files):
    """Exercise the real batch loop without contacting AnythingLLM Desktop."""
    open_local_app(page, local_app_url)
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    confirm = page.get_by_role("button", name="Confirm and start processing")
    expect(confirm).to_be_enabled(timeout=20000)
    page.get_by_role("radio", name="Create local files only").check()
    confirm.click()
    live_confirm = page.locator("#confirm-automatic-run-button")
    expect(live_confirm).to_have_text("Processing…", timeout=500)

    # The terminal status is outside the collapsed downloads section and is
    # therefore the durable user-visible completion signal for every source.
    expect(page.get_by_text(re.compile(r"PDF source decisions recorded: 6/6"))).to_be_visible(
        timeout=120000
    )
    # Source decisions alone can also include failures/skips. Require the
    # successful terminal handoff, not just a completed local decision count.
    expect(live_confirm).to_have_text("Processing successful ✓")
    expect(page.get_by_text("Overall progress: 100%", exact=True)).to_be_visible()


def test_selected_pdf_reuse_action_has_an_icon_not_an_empty_square(page, local_app_url, one_page_pdf):
    """The selected-file controls retain distinct, meaningful compact glyphs."""
    open_local_app(page, local_app_url)
    reuse_actions = page.locator('.pdf-upload-input button[aria-label="Use selected files again"]')
    expect(reuse_actions).to_be_hidden()

    page.locator(".pdf-upload-input input[type='file']").set_input_files(str(one_page_pdf))

    visible_reuse_action = page.locator(
        '.pdf-upload-input button[aria-label="Use selected files again"]:visible'
    )
    expect(visible_reuse_action).not_to_have_count(0, timeout=15000)
    def wait_for_icon(selector, marker):
        # The picker and stylesheet hydrate separately after a file transfer.
        page.wait_for_function(
            """({selector, marker}) => {
              const buttons = Array.from(document.querySelectorAll(selector))
                .filter(button => button.getClientRects().length);
              return buttons.length > 0 && buttons.every(button =>
                getComputedStyle(button, '::after').backgroundImage.includes(marker));
            }""",
            arg={"selector": selector, "marker": marker}, timeout=15000,
        )

    wait_for_icon('.pdf-upload-input button[aria-label="Use selected files again"]', "M3 12a9")
    visible_upload_action = page.locator(
        '.pdf-upload-input button[aria-label="common.upload"]:visible'
    )
    wait_for_icon('.pdf-upload-input button[aria-label="common.upload"]', "M12 19V5")
    with page.expect_file_chooser(timeout=10000) as chooser_info:
        visible_upload_action.click()
    chooser_info.value
    action_orders = page.locator(".pdf-upload-input .icon-button-wrapper.top-panel").evaluate_all(
        "hosts => hosts.map(host => Array.from(host.querySelectorAll(':scope > button'))"
        ".filter(button => getComputedStyle(button).display !== 'none')"
        ".map(button => button.getAttribute('aria-label'))).filter(order => order.length >= 3)"
    )
    assert ["common.upload", "Clear", "Use selected files again"] in action_orders
    action_layouts = page.locator(".pdf-upload-input .icon-button-wrapper.top-panel").evaluate_all(
        "hosts => hosts.map(host => Array.from(host.querySelectorAll(':scope > button'))"
        ".filter(button => getComputedStyle(button).display !== 'none')"
        ".map(button => { const box = button.getBoundingClientRect(); return { label: button.getAttribute('aria-label'), left: box.left, right: box.right, width: box.width }; }))"
        ".filter(layout => layout.length >= 3)"
    )
    matching_layout = next(
        layout
        for layout in action_layouts
        if [item["label"] for item in layout]
        == ["common.upload", "Clear", "Use selected files again"]
    )
    assert matching_layout[0]["width"] >= 18
    assert matching_layout[0]["right"] <= matching_layout[1]["left"]
    assert matching_layout[1]["right"] <= matching_layout[2]["left"]


def test_fresh_page_shows_both_actions_disabled(page, local_app_url):
    """There is no run to cancel before a PDF is selected."""
    open_local_app(page, local_app_url)
    expect(page.get_by_role("button", name="Confirm and start processing")).to_be_disabled()
    expect(page.get_by_role("button", name="Cancel")).to_be_visible()
    expect(page.get_by_role("button", name="Cancel")).to_be_disabled()


def test_metadata_table_stays_folded_and_shows_loading_then_detected_values(
    page, local_app_url, one_page_pdf
):
    open_local_app(page, local_app_url)
    accordion = page.locator("#document-metadata-accordion")
    header = accordion.locator(":scope > button.label-wrap")
    expect(header).to_be_visible()
    expect(page.get_by_label("Override Author")).to_be_hidden()
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files(str(one_page_pdf))
    header.click()
    expect(page.get_by_label("Override Author")).to_be_visible(timeout=30000)
    expect(page.locator(".metadata-table thead th")).to_have_text(["Field", "Detected", "Override (if applied)"])
    page.wait_for_function(
        "document.querySelector('#document-metadata-scan-status .metadata-scan-inline')?.dataset.state === 'ready'",
        timeout=60000,
    )
    expect(page.locator(".metadata-table .metadata-detected-value").first).not_to_have_text("Loading…")
    expect(page.get_by_label("Override Publisher")).to_have_value("")
    expect(accordion.get_by_role("checkbox")).to_have_count(0)
    expect(page.get_by_role("button", name="Refresh detected PDF metadata")).to_have_count(0)


def test_metadata_overrides_update_processing_fields(page, local_app_url, one_page_pdf):
    open_local_app(page, local_app_url)
    page.locator("#document-metadata-accordion > button.label-wrap").click()
    page.locator(".pdf-upload-input input[type='file']").set_input_files(str(one_page_pdf))
    page.wait_for_function(
        "document.querySelector('#document-metadata-scan-status .metadata-scan-inline')?.dataset.state === 'ready'",
        timeout=60000,
    )
    for label, text, transport_id in (
        ("Title used", "Edited title", "automatic-title-override-transport"),
        ("Author", "Edited Author", "automatic-author-override-transport"),
        ("Short citation label", "Edited citation label", "automatic-short-label-override-transport"),
        ("Publisher", "Edited Publisher", "automatic-publisher-override-transport"),
    ):
        editor = page.get_by_label(f"Override {label}")
        expect(editor).to_have_value("")
        editor.fill(text)
        transport = page.locator(f"#{transport_id} input, #{transport_id} textarea")
        expect(transport).to_have_value(text)
    expect(page.get_by_label("Override Publisher")).to_have_value("Edited Publisher", timeout=60000)
    page.get_by_label("Override Publisher").fill("")
    expect(page.locator(
        "#automatic-publisher-override-transport input, #automatic-publisher-override-transport textarea"
    )).to_have_value("")


def test_removing_other_pdfs_preserves_surviving_metadata_overrides(
    page, local_app_url, local_app_home, six_pdf_files
):
    open_local_app(page, local_app_url)
    page.locator("#document-metadata-accordion > button.label-wrap").click()
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    panels = page.locator(".metadata-panel")
    expect(panels).to_have_count(6, timeout=60000)
    for index in (0, 2, 4):
        panels.filter(has_text=six_pdf_files[index].name).get_by_label(
            "Override Author", exact=True
        ).fill(f"Author {index}")
    for index in (5, 3, 1):
        page.locator("#automatic-pdf-upload .file-preview tr").filter(
            has=page.locator(f'td[aria-label="{six_pdf_files[index].name}"]')
        ).get_by_role("button", name="Remove this file").click()
    expect(page.locator(".metadata-panel")).to_have_count(3, timeout=60000)
    page.wait_for_function(
        "document.querySelector('#document-metadata-scan-status .metadata-scan-inline')?.dataset.state === 'ready'",
        timeout=60000,
    )
    for index in (0, 2, 4):
        expect(page.locator(".metadata-panel").filter(
            has_text=six_pdf_files[index].name
        ).get_by_label("Override Author", exact=True)).to_have_value(f"Author {index}")
    edits = json.loads(page.locator(
        "#automatic-document-overrides-transport input, #automatic-document-overrides-transport textarea"
    ).input_value())
    assert len(edits) == 3
    page.locator("#automatic-pdf-upload .file-preview tr").filter(
        has=page.locator(f'td[aria-label="{six_pdf_files[2].name}"]')
    ).get_by_role("button", name="Remove this file").click()
    expect(page.locator(".metadata-panel")).to_have_count(2, timeout=60000)
    for index in (0, 4):
        expect(page.locator(".metadata-panel").filter(
            has_text=six_pdf_files[index].name
        ).get_by_label("Override Author", exact=True)).to_have_value(f"Author {index}")
    page.get_by_role("radio", name="Create local files only").check()
    page.get_by_role("button", name=re.compile(r"^Advanced preparation overrides")).click()
    page.get_by_role("checkbox", name="Retain detailed evidence even if this run is ready").check()
    confirm = page.locator("#confirm-automatic-run-button")
    expect(confirm).to_be_enabled(timeout=30000)
    existing_profiles = set((local_app_home / "run-state").rglob("source-profile.json"))
    confirm.click()
    page.wait_for_function(
        "['successful', 'warning', 'failed', 'cancelled'].includes("
        "document.querySelector('.automatic-run-activity')?.dataset.runState)",
        timeout=120000,
    )
    expect(confirm).to_have_text("Processing successful ✓")
    profiles = [json.loads(path.read_text(encoding="utf-8")) for path in
                (local_app_home / "run-state").rglob("source-profile.json")
                if path not in existing_profiles]
    assert len(profiles) == 2
    assert {profile["detected_author"] for profile in profiles} == {"Author 0", "Author 4"}


def test_preview_all_control_scans_beyond_fifth_pdf(page, local_app_url, six_pdf_files):
    open_local_app(page, local_app_url)
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    preview_all = page.get_by_role("checkbox", name="Process metadata for all files")
    expect(preview_all).to_be_visible(timeout=30000)
    expect(page.get_by_label("Override Author").first).to_be_hidden()
    page.wait_for_function(
        "document.querySelector('#document-metadata-scan-status .metadata-scan-inline')?.dataset.state === 'ready'",
        timeout=60000,
    )
    accordion_header = page.locator("#document-metadata-accordion > button.label-wrap")
    accordion_header.click()
    expect(page.locator(".metadata-panel").filter(
        has_text=six_pdf_files[5].name
    )).to_contain_text("Not previewed")
    expect(preview_all).to_be_visible()
    accordion_header.click()
    preview_all.check()
    expect(page.get_by_label("Override Author").first).to_be_hidden()
    page.wait_for_function(
        "(() => { const s = document.querySelector('#document-metadata-scan-status .metadata-scan-inline'); "
        "return s?.dataset.previewScope === 'all' && s?.dataset.previewComplete === 'true' "
        "&& s?.dataset.state === 'ready'; })()",
        timeout=90000,
    )
    accordion_header.click()
    expect(preview_all).to_be_visible()
    expect(page.locator(".metadata-panel").filter(
        has_text=six_pdf_files[5].name
    )).not_to_contain_text("Not previewed")
    expect(page.locator("#document-metadata-scan-status")).to_contain_text(
        "6 of 6 selected PDFs"
    )


def test_auto_confirm_uses_override_entered_after_arming(page, paused_metadata_app, six_pdf_files):
    url, started, release, app_home = paused_metadata_app
    open_local_app(page, url)
    page.get_by_role("radio", name="Create local files only").check()
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    page.get_by_role("checkbox", name="Process metadata for all files").check()
    deadline = time.monotonic() + 30
    while not started.exists():
        assert time.monotonic() < deadline, "Preview did not reach the paused inference"
        page.wait_for_timeout(50)
    page.get_by_role("checkbox", name="Confirm and start run automatically").check()
    page.locator("#document-metadata-accordion > button.label-wrap").click()
    sixth = page.locator(".metadata-panel").filter(has_text=six_pdf_files[5].name)
    sixth.get_by_label("Override Author", exact=True).fill("Late Override Sixth")
    release.touch()
    page.wait_for_function(
        "['successful', 'warning', 'failed', 'cancelled'].includes("
        "document.querySelector('.automatic-run-activity')?.dataset.runState)",
        timeout=180000,
    )
    expect(page.locator("#confirm-automatic-run-button")).to_have_text("Processing successful ✓")
    profiles = [json.loads(path.read_text(encoding="utf-8")) for path in
                (app_home / "run-state").rglob("source-profile.json")]
    assert len(profiles) == 6
    assert sum(profile["detected_author"] == "Late Override Sixth" for profile in profiles) == 1


def test_cancel_during_metadata_preview_blocks_late_auto_confirm_and_allows_retry(
    page, paused_metadata_app, six_pdf_files
):
    url, started, release, app_home = paused_metadata_app
    open_local_app(page, url)
    page.get_by_role("radio", name="Create local files only").check()
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    page.get_by_role("checkbox", name="Process metadata for all files").check()
    deadline = time.monotonic() + 30
    while not started.exists():
        assert time.monotonic() < deadline, "Preview did not reach paused inference"
        page.wait_for_timeout(50)
    page.get_by_role("checkbox", name="Confirm and start run automatically").check()
    cancel = page.get_by_role("button", name="Cancel")
    expect(cancel).to_be_enabled(timeout=10000)
    cancel.click()
    expect(page.get_by_role("checkbox", name="Confirm and start run automatically")).not_to_be_checked()
    page.wait_for_function(
        "document.querySelector('.automatic-run-activity')?.dataset.runState === 'selection_cancelled'",
        timeout=15000,
    )
    expect(page.locator("#confirm-automatic-run-button")).to_be_disabled()
    release.touch()
    page.wait_for_timeout(1200)
    expect(page.locator("#confirm-automatic-run-button")).to_be_disabled()
    assert not list((app_home / "run-state").rglob("source-profile.json"))
    upload = page.locator(".pdf-upload-input input[type='file']")
    upload.set_input_files([])
    upload.set_input_files([str(path) for path in six_pdf_files])
    expect(page.locator("#confirm-automatic-run-button")).to_be_enabled(timeout=60000)


def test_cancel_after_confirm_stops_preflight_before_run_root(
    page, paused_confirm_preflight_app, one_page_pdf
):
    url, started, release, app_home = paused_confirm_preflight_app
    open_local_app(page, url)
    page.get_by_role("radio", name="Create local files only").check()
    page.locator(".pdf-upload-input input[type='file']").set_input_files(str(one_page_pdf))
    confirm = page.get_by_role("button", name="Confirm and start processing")
    expect(confirm).to_be_enabled(timeout=30000)
    confirm.click()
    deadline = time.monotonic() + 30
    while not started.exists():
        assert time.monotonic() < deadline, "Confirmed preflight did not start"
        page.wait_for_timeout(50)
    expect(page.locator("#confirm-automatic-run-button")).to_have_text("Processing…")
    cancel = page.get_by_role("button", name="Cancel")
    expect(cancel).to_be_enabled(timeout=10000)
    cancel.click()
    release.touch()
    page.wait_for_function(
        "document.querySelector('.automatic-run-activity')?.dataset.runState === 'cancelled'",
        timeout=30000,
    )
    assert not list((app_home / "run-state").rglob("run-progress.json"))
    assert not list((app_home / "run-state").rglob("source-profile.json"))


def test_auto_confirm_visibly_presses_button_before_preflight_finishes(
    page, paused_confirm_preflight_app, six_pdf_files
):
    url, started, release, _app_home = paused_confirm_preflight_app
    open_local_app(page, url)
    page.get_by_role("radio", name="Create local files only").check()
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    page.wait_for_function(
        "(() => { const s = document.querySelector('#document-metadata-scan-status .metadata-scan-inline'); "
        "return s?.dataset.state === 'ready' && s?.dataset.previewScope !== 'all' "
        "&& document.querySelector('.automatic-run-activity')?.dataset.runState === 'ready'; })()",
        timeout=90000,
    )
    page.get_by_role("checkbox", name="Process metadata for all files").check()
    page.get_by_role("checkbox", name="Confirm and start run automatically").check()
    deadline = time.monotonic() + 90
    while not started.exists():
        assert time.monotonic() < deadline, "Automatic Confirm did not start preflight"
        page.wait_for_timeout(100)
    confirm = page.locator("#confirm-automatic-run-button")
    expect(confirm).to_have_text("Processing…")
    expect(confirm).to_be_disabled()
    release.touch()
    page.wait_for_function(
        "['successful', 'warning', 'failed', 'cancelled'].includes("
        "document.querySelector('.automatic-run-activity')?.dataset.runState)",
        timeout=180000,
    )
    expect(confirm).to_have_text("Processing successful ✓")
    expect(page.get_by_role("checkbox", name="Confirm and start run automatically")).to_be_checked()


def test_auto_confirm_starts_without_browser_button_becoming_enabled(
    page, local_app_url, local_app_home, six_pdf_files
):
    open_local_app(page, local_app_url)
    page.get_by_role("radio", name="Create local files only").check()
    page.locator(".pdf-upload-input input[type='file']").set_input_files(
        [str(path) for path in six_pdf_files]
    )
    page.get_by_role("checkbox", name="Process metadata for all files").check()
    page.wait_for_function(
        "(() => { const s = document.querySelector('#document-metadata-scan-status .metadata-scan-inline'); "
        "return s?.dataset.previewScope === 'all' && s?.dataset.previewComplete === 'true' "
        "&& s?.dataset.state === 'ready' "
        "&& document.querySelector('.automatic-run-activity')?.dataset.runState === 'ready'; })()",
        timeout=90000,
    )
    confirm = page.get_by_role("button", name="Confirm and start processing")
    expect(confirm).to_be_enabled()
    confirm.evaluate("button => { button.disabled = true; }")
    page.get_by_role("checkbox", name="Confirm and start run automatically").check()
    expect(confirm).to_be_disabled()
    page.wait_for_function(
        "['successful', 'warning', 'failed', 'cancelled'].includes("
        "document.querySelector('.automatic-run-activity')?.dataset.runState)",
        timeout=180000,
    )
    expect(page.locator("#confirm-automatic-run-button")).to_have_text("Processing successful ✓")
    assert len(list((local_app_home / "run-state").rglob("source-profile.json"))) == 6


def test_overrides_survive_before_during_and_after_detection_and_save_on_confirm(
    page, paused_metadata_app, one_page_pdf
):
    url, started, release, app_home = paused_metadata_app
    open_local_app(page, url)
    page.locator("#document-metadata-accordion > button.label-wrap").click()
    fields = ("Title used", "Author", "Short citation label", "Publisher")
    for field in fields:
        editor = page.get_by_label(f"Override {field}", exact=True)
        expect(editor).to_be_enabled()
        assert editor.get_attribute("placeholder") is None
        editor.fill(f"Before {field}")
    page.locator(".pdf-upload-input input[type='file']").set_input_files(str(one_page_pdf))
    deadline = time.monotonic() + 30
    while not started.exists():
        assert time.monotonic() < deadline, "Preview did not reach the paused inference"
        page.wait_for_timeout(50)
    expect(page.locator(
        ".metadata-detected-value:not([data-metadata-known='filename'])"
    ).first).to_have_text("Loading…")
    for field in fields:
        editor = page.get_by_label(f"Override {field}", exact=True)
        expect(editor).to_be_visible()
        expect(editor).to_be_enabled()
        expect(editor).to_have_value(f"Before {field}")
        editor.fill(f"During {field}")
    release.touch()
    page.wait_for_function(
        "document.querySelector('#document-metadata-scan-status .metadata-scan-inline')?.dataset.state === 'ready'",
        timeout=60000,
    )
    expect(page.get_by_label("Override Publisher", exact=True)).to_be_focused()
    for field in fields:
        editor = page.get_by_label(f"Override {field}", exact=True)
        expect(editor).to_be_enabled()
        expect(editor).to_have_value(f"During {field}")
        editor.fill(f"After {field}")

    page.get_by_role("radio", name="Create local files only").check()
    page.get_by_role("button", name=re.compile(r"^Advanced preparation overrides")).click()
    page.get_by_role("checkbox", name="Retain detailed evidence even if this run is ready").check()
    confirm = page.locator("#confirm-automatic-run-button")
    expect(confirm).to_be_enabled(timeout=30000)
    confirm.click()
    page.wait_for_function(
        "['successful', 'warning', 'failed', 'cancelled'].includes("
        "document.querySelector('.automatic-run-activity')?.dataset.runState)",
        timeout=120000,
    )
    expect(confirm).to_have_text("Processing successful ✓")
    profiles = list((app_home / "run-state").rglob("source-profile.json"))
    assert len(profiles) == 1
    profile = json.loads(profiles[0].read_text(encoding="utf-8"))
    assert profile["detected_title"] == "After Title used"
    assert profile["detected_author"] == "After Author"
    assert profile["source_short_label"] == "After Short citation label"
    assert profile["detected_publisher"] == "After Publisher"


@pytest.mark.parametrize("route", ["direct", "folder"])
def test_independent_pdf_edits_during_later_preview_save_without_cross_file_leakage(
    page, batch_metadata_app, route
):
    url, started, release, app_home, files = batch_metadata_app
    open_local_app(page, url)
    page.locator("#document-metadata-accordion > button.label-wrap").click()
    if route == "folder":
        page.locator("#choose-pdf-folder-button").click()
    else:
        page.locator(".pdf-upload-input input[type='file']").set_input_files([str(p) for p in files])
    deadline = time.monotonic() + 30
    while not started.exists():
        assert time.monotonic() < deadline, "Second preview did not reach inference"
        page.wait_for_timeout(50)
    expect(page.locator(".metadata-panel-heading")).to_have_count(0)
    expect(page.get_by_role("button", name="Refresh detected PDF metadata")).to_have_count(0)
    expect(page.get_by_label("Override Publisher", exact=True)).to_have_count(2)
    first = page.locator(".metadata-panel").filter(has_text=files[0].name)
    second = page.locator(".metadata-panel").filter(has_text=files[1].name)
    edits = {"Title used": "Chosen Alpha", "Author": "Chosen Alpha Author",
             "Short citation label": "AlphaEdit", "Publisher": "Alpha Press"}
    for field, value in edits.items():
        editor = first.get_by_label(f"Override {field}", exact=True)
        expect(editor).to_be_enabled()
        editor.click()
        editor.fill(value)
    second.get_by_label("Override Publisher", exact=True).click()
    second.get_by_label("Override Publisher", exact=True).fill("Beta Press")
    release.touch()
    page.wait_for_function(
        "document.querySelector('#document-metadata-scan-status .metadata-scan-inline')?.dataset.state === 'ready'",
        timeout=60000,
    )
    expect(second.get_by_label("Override Publisher", exact=True)).to_be_focused()
    for field, value in edits.items():
        expect(first.get_by_label(f"Override {field}", exact=True)).to_have_value(value)
    expect(second.get_by_label("Override Author", exact=True)).to_have_value("")
    expect(second.get_by_label("Override Publisher", exact=True)).to_have_value("Beta Press")
    page.get_by_role("radio", name="Create local files only").check()
    page.get_by_role("button", name=re.compile(r"^Advanced preparation overrides")).click()
    page.get_by_role("checkbox", name="Retain detailed evidence even if this run is ready").check()
    confirm = page.locator("#confirm-automatic-run-button")
    expect(confirm).to_be_enabled(timeout=30000)
    confirm.click()
    page.wait_for_function(
        "['successful', 'warning', 'failed', 'cancelled'].includes("
        "document.querySelector('.automatic-run-activity')?.dataset.runState)",
        timeout=120000,
    )
    expect(confirm).to_have_text("Processing successful ✓")
    paths = list((app_home / "run-state").rglob("source-profile.json"))
    assert len(paths) == 2
    profiles = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    by_title = {p["detected_title"]: p for p in profiles}
    assert set(by_title) == {"Chosen Alpha", "Meadow Studies Beta"}
    assert by_title["Chosen Alpha"]["detected_author"] == "Chosen Alpha Author"
    assert by_title["Chosen Alpha"]["detected_publisher"] == "Alpha Press"
    assert by_title["Chosen Alpha"]["source_short_label"] == "AlphaEdit"
    assert by_title["Meadow Studies Beta"]["detected_author"] == "Clara Ellen North"
    assert by_title["Meadow Studies Beta"]["detected_publisher"] == "Beta Press"
    assert by_title["Meadow Studies Beta"]["source_short_label"] == "North"


def test_clear_batch_clears_inflight_preview_and_stale_results_cannot_restore_it(page, batch_metadata_app):
    url, started, release, _home, files = batch_metadata_app
    open_local_app(page, url)
    page.locator("#document-metadata-accordion > button.label-wrap").click()
    page.locator("#choose-pdf-folder-button").click()
    deadline = time.monotonic() + 30
    while not started.exists():
        assert time.monotonic() < deadline
        page.wait_for_timeout(50)
    page.locator("#clear-selected-pdf-batch").click()
    expect(page.locator("#batch-folder-file-selector .token")).to_have_count(0)
    expect(page.locator("#choose-pdf-folder-button")).to_be_visible()
    expect(page.locator("#choose-pdf-folder-button")).to_be_enabled()
    expect(page.locator("#document-metadata-accordion .metadata-file-name")).to_have_count(0)
    expect(page.locator("#confirm-automatic-run-button")).to_be_disabled()
    release.touch()
    page.wait_for_timeout(1000)  # Let the superseded stream attempt its next publication.
    expect(page.locator("#document-metadata-accordion .metadata-file-name")).to_have_count(0)
    expect(page.locator("#batch-folder-file-selector .token")).to_have_count(0)
    page.locator("#choose-pdf-folder-button").click()
    page.wait_for_function(
        "document.querySelector('#document-metadata-scan-status .metadata-scan-inline')?.dataset.state === 'ready'",
        timeout=60000,
    )
    expect(page.locator("#document-metadata-accordion .metadata-file-name")).to_have_count(2)
    page.locator("#clear-selected-pdf-batch").click()
    expect(page.locator("#document-metadata-accordion .metadata-file-name")).to_have_count(0)
    expect(page.locator("#batch-folder-file-selector .token")).to_have_count(0)
    expect(page.locator("#confirm-automatic-run-button")).to_be_disabled()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_metadata_table_compact_inputs_and_alternating_rows(page, local_app_url, one_page_pdf, theme):
    page.emulate_media(color_scheme=theme)
    open_local_app(page, local_app_url)
    page.locator("#document-metadata-accordion > button.label-wrap").click()
    page.locator(".pdf-upload-input input[type='file']").set_input_files(str(one_page_pdf))
    page.wait_for_function(
        "document.querySelector('#document-metadata-scan-status .metadata-scan-inline')?.dataset.state === 'ready'",
        timeout=60000,
    )
    editor = page.get_by_label("Override Publisher", exact=True)
    expect(editor).to_be_enabled()
    assert editor.bounding_box()["height"] == 24
    before = editor.evaluate("e => ({shadow: getComputedStyle(e).boxShadow, background: getComputedStyle(e).backgroundColor})")
    editor.hover()
    assert editor.evaluate("e => ({shadow: getComputedStyle(e).boxShadow, background: getComputedStyle(e).backgroundColor})") == before
    assert before["shadow"] == "none"
    rows = page.locator(".metadata-table tbody tr")
    colors = rows.evaluate_all("rows => rows.map(r => getComputedStyle(r).backgroundColor)")
    assert colors[0] == colors[2] and colors[1] == colors[3] and colors[0] != colors[1]
    stripe_alpha = float(re.findall(r"[\d.]+", colors[1])[-1])
    assert stripe_alpha > 0.5 if theme == "light" else stripe_alpha < 0.1
    assert set(page.locator(".metadata-table tbody th, .metadata-table tbody td").evaluate_all(
        "cells => cells.map(c => getComputedStyle(c).verticalAlign)"
    )) == {"middle"}
    info = page.get_by_role("button", name="More detected PDF metadata", exact=True)
    geometry = info.evaluate("""button => {
        const b = button.getBoundingClientRect();
        const range = document.createRange(); range.selectNodeContents(button);
        const glyph = range.getBoundingClientRect();
        return {width: b.width, height: b.height, radius: getComputedStyle(button).borderRadius,
            dx: (glyph.left + glyph.width / 2) - (b.left + b.width / 2),
            dy: (glyph.top + glyph.height / 2) - (b.top + b.height / 2)};
    }""")
    assert geometry["width"] == geometry["height"] == 24
    assert geometry["radius"] == "50%"
    assert abs(geometry["dx"]) <= 1 and abs(geometry["dy"]) <= 1.5
    info.click()
    expect(info).to_have_attribute("aria-expanded", "true")
    expect(page.locator(".metadata-extra-row").first).to_be_visible()
    info.click()

    widths = page.locator(".metadata-table tbody tr").first.locator("th,td").evaluate_all(
        "cells => cells.map(c => c.getBoundingClientRect().width)"
    )
    assert widths[1] > 3.5 * widths[0] and widths[1] > 3.5 * widths[2]
    table_width = page.locator(".metadata-table").bounding_box()["width"]
    assert editor.bounding_box()["width"] < 0.17 * table_width
    editor.fill("A long override that continues horizontally inside the compact field. " * 8)
    assert editor.evaluate("e => e.scrollWidth > e.clientWidth")

    batch = page.locator(".batch-folder-panel").first.bounding_box()
    metadata = page.locator("#document-metadata-accordion").bounding_box()
    assert batch["height"] == 88  # 80% of the previous 110px chooser surface.
    assert 4 <= metadata["y"] - (batch["y"] + batch["height"]) <= 8.5


def test_document_metadata_has_one_outer_accordion(page, local_app_url, one_page_pdf):
    open_local_app(page, local_app_url)
    page.locator(".pdf-upload-input input[type='file']").set_input_files(str(one_page_pdf))
    expect(page.locator("#document-metadata-accordion")).to_be_visible()
    expect(page.locator(".document-metadata-details")).to_have_count(0)
    page.locator("#document-metadata-accordion > button.label-wrap").click()
    expect(page.locator("#document-metadata-accordion .metadata-table")).to_have_count(1, timeout=30000)


def test_dark_theme_confirmation_action_row_does_not_overlap(page, local_app_url, one_page_pdf):
    """The confirmation row must keep two separately clickable actions in dark mode."""
    page.emulate_media(color_scheme="dark")
    open_local_app(page, local_app_url)
    expect(page.locator("body")).to_have_class(re.compile(r"(?:^|\\s)dark(?:\\s|$)"))
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files(str(one_page_pdf))

    confirm = page.get_by_role("button", name="Confirm and start processing")
    cancel = page.get_by_role("button", name="Cancel")
    expect(confirm).to_be_visible(timeout=15000)
    expect(confirm).to_be_enabled(timeout=20000)
    expect(cancel).to_be_visible()
    confirm_box = confirm.bounding_box()
    cancel_box = cancel.bounding_box()
    assert confirm_box is not None and cancel_box is not None
    horizontal_separation = confirm_box["x"] + confirm_box["width"] <= cancel_box["x"]
    vertical_separation = confirm_box["y"] + confirm_box["height"] <= cancel_box["y"]
    assert horizontal_separation or vertical_separation


def test_narrow_confirmation_action_row_wraps_without_overlap(page, local_app_url, one_page_pdf):
    """Small displays may wrap the actions, but may never stack click targets."""
    page.set_viewport_size({"width": 640, "height": 900})
    open_local_app(page, local_app_url)
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files(str(one_page_pdf))

    confirm = page.get_by_role("button", name="Confirm and start processing")
    cancel = page.get_by_role("button", name="Cancel")
    expect(confirm).to_be_visible(timeout=15000)
    expect(confirm).to_be_enabled(timeout=20000)
    expect(cancel).to_be_visible()
    confirm_box = confirm.bounding_box()
    cancel_box = cancel.bounding_box()
    assert confirm_box is not None and cancel_box is not None
    horizontal_separation = confirm_box["x"] + confirm_box["width"] <= cancel_box["x"]
    vertical_separation = confirm_box["y"] + confirm_box["height"] <= cancel_box["y"]
    assert horizontal_separation or vertical_separation


def test_system_theme_switch_preserves_the_fresh_run_controls(page, local_app_url, one_page_pdf):
    page.emulate_media(color_scheme="dark")
    open_local_app(page, local_app_url)
    expect(page.locator("body")).to_have_class(re.compile(r"(?:^|\\s)dark(?:\\s|$)"))
    assert "__theme=" not in page.url
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files(str(one_page_pdf))
    expect(page.get_by_role("button", name="Confirm and start processing")).to_be_enabled(timeout=15000)

    page.emulate_media(color_scheme="light")
    expect(page.locator("body")).not_to_have_class(
        re.compile(r"(?:^|\\s)dark(?:\\s|$)"), timeout=5000
    )

    advanced_tab = page.get_by_role("tab", name="Advanced")
    advanced_tab.click()
    follow_system = page.locator("#follow-windows-theme input[type='checkbox']")
    expect(follow_system).to_be_checked()
    theme_toggle = page.get_by_role("button", name="Light / Dark")
    expect(theme_toggle).to_have_count(1)
    theme_toggle.click()
    expect(follow_system).not_to_be_checked()
    expect(page.locator("body")).to_have_class(re.compile(r"(?:^|\\s)dark(?:\\s|$)"))

    theme_toggle.click()
    expect(follow_system).to_be_checked()
    expect(page.locator("body")).not_to_have_class(
        re.compile(r"(?:^|\\s)dark(?:\\s|$)"), timeout=5000
    )

    automatic_tab = page.get_by_role("tab", name="Automatic")
    automatic_tab.click()
    expect(page.get_by_role("button", name="Confirm and start processing")).to_be_enabled(timeout=5000)
    expect(page.get_by_text("Processing successful", exact=False)).to_have_count(0)
