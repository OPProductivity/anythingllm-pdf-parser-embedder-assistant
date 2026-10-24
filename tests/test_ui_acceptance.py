r"""Opt-in browser acceptance tests for the localhost Gradio application.

Run explicitly with:
  .\.venv\Scripts\python.exe -m pytest -m ui_local --browser chromium tests/test_ui_acceptance.py

The suite starts a separate app instance, never contacts AnythingLLM Desktop,
and uses only a disposable one-page PDF created in pytest's temporary folder.
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


def test_selected_pdf_exposes_one_confirm_action_and_safe_prestart_cancel(page, local_app_url, one_page_pdf):
    """Selection exposes one action; Cancel before Confirm does not fake a stop."""
    open_local_app(page, local_app_url)
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files(str(one_page_pdf))

    confirm = page.get_by_role("button", name="Confirm and start processing")
    cancel = page.get_by_role("button", name="Cancel")
    expect(confirm).to_be_visible(timeout=15000)
    expect(confirm).to_be_enabled(timeout=20000)
    expect(cancel).to_be_visible()
    expect(cancel).to_be_disabled()
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
    expect(page.locator(".metadata-detected-value").first).to_have_text("Loading…")
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
