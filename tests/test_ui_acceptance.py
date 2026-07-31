r"""Opt-in browser acceptance tests for the localhost Gradio application.

Run explicitly with:
  .\.venv\Scripts\python.exe -m pytest -m ui_local --browser chromium tests/test_ui_acceptance.py

The suite starts a separate app instance, never contacts AnythingLLM Desktop,
and uses only a disposable one-page PDF created in pytest's temporary folder.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import fitz
import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "rag_pdf_gradio_app.py"

pytestmark = pytest.mark.ui_local


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
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"Local Gradio app did not listen at {url} within {timeout_seconds}s.")


@pytest.fixture(scope="module")
def local_app_url(tmp_path_factory):
    port = free_local_port()
    url = f"http://127.0.0.1:{port}"
    environment = dict(os.environ)
    environment["GRADIO_SERVER_PORT"] = str(port)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["ANYTHINGLLM_PDF_ASSISTANT_HOME"] = str(tmp_path_factory.mktemp("gradio-local-home"))
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
            wait_for_http(url, process)
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
def two_pdf_files(tmp_path: Path) -> list[Path]:
    paths = [tmp_path / "browser-first.pdf", tmp_path / "browser-second.pdf"]
    for index, path in enumerate(paths, start=1):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 72), f"Browser multi-file acceptance PDF {index}.")
            document.save(path)
        finally:
            document.close()
    return paths


def test_fresh_file_selection_never_displays_prior_success(page, local_app_url, one_page_pdf):
    page.goto(local_app_url)
    expect(page.get_by_text("Processing successful", exact=False)).to_have_count(0)
    expect(page.get_by_text("Overall progress: 100%", exact=False)).to_have_count(0)
    cancel = page.locator("#cancel-automatic-run-button")
    expect(cancel).to_be_visible()
    expect(cancel).to_be_disabled()


def test_local_only_mode_hides_upload_controls_even_when_advanced_is_open(page, local_app_url):
    """Mode changes must survive Gradio's lazy accordion mounting."""
    page.goto(local_app_url)

    local_only = page.get_by_role("radio", name="Create local files only")
    expect(local_only).to_have_count(1)
    local_only.check()

    expect(page.get_by_text("Local-only run:", exact=True)).to_have_count(0)
    expect(page.locator("#native-metadata-upload-section")).to_be_hidden()
    expect(page.locator("#anythingllm-output-folder")).to_have_count(0)

    advanced = page.get_by_role("button", name="Advanced preparation overrides ▼")
    expect(advanced).to_have_count(1)
    advanced.click()
    expect(page.get_by_label("Extraction backend", exact=True)).to_have_count(0)
    expect(page.get_by_label("AnythingLLM API URL", exact=True)).to_have_count(0)
    expect(page.get_by_label("AnythingLLM chunk size", exact=True)).to_have_count(0)
    expect(page.get_by_role("checkbox", name="Auto-apply before upload run")).to_have_count(0)

    upload_mode = page.get_by_role("radio", name="Create local files and upload to AnythingLLM")
    expect(upload_mode).to_have_count(1)
    upload_mode.check()

    expect(page.locator("#native-metadata-upload-section")).to_be_visible(timeout=5000)
    expect(page.get_by_label("AnythingLLM API URL", exact=True)).to_be_visible(timeout=5000)
    expect(page.get_by_label("AnythingLLM chunk size", exact=True)).to_be_visible()
    expect(page.get_by_role("checkbox", name="Auto-apply before upload run")).to_be_visible()


def test_no_logs_mode_is_local_only_and_remains_a_visible_choice(page, local_app_url):
    page.goto(local_app_url)

    no_logs = page.get_by_role("radio", name="Create local files without logs")
    expect(no_logs).to_have_count(1)
    no_logs.check()
    expect(no_logs).to_be_checked()
    expect(page.locator("#native-metadata-upload-section")).to_be_hidden()
    expect(page.get_by_label("AnythingLLM API URL", exact=True)).to_have_count(0)


def test_selecting_a_pdf_resets_a_prior_local_only_choice_to_the_new_run_defaults(
    page, local_app_url, one_page_pdf
):
    """A new selection is a new run, rather than a partially inherited run."""
    page.goto(local_app_url)
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
    page.goto(local_app_url)
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
    page.goto(local_app_url)
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files(str(one_page_pdf))

    confirm = page.get_by_role("button", name="Confirm and start processing")
    cancel = page.get_by_role("button", name="Cancel")
    expect(confirm).to_be_visible(timeout=15000)
    expect(confirm).to_be_enabled()
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
    page.goto(local_app_url)
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files([str(path) for path in two_pdf_files])

    confirm = page.get_by_role("button", name="Confirm and start processing")
    expect(confirm).to_be_enabled(timeout=15000)
    expect(page.get_by_role("button", name="Remove this file")).to_have_count(2)


def test_selected_pdf_reuse_action_has_an_icon_not_an_empty_square(page, local_app_url, one_page_pdf):
    """The selected-file controls retain distinct, meaningful compact glyphs."""
    page.goto(local_app_url)
    reuse_actions = page.locator('.pdf-upload-input button[aria-label="Use selected files again"]')
    expect(reuse_actions).to_be_hidden()

    page.locator(".pdf-upload-input input[type='file']").set_input_files(str(one_page_pdf))

    visible_reuse_action = page.locator(
        '.pdf-upload-input button[aria-label="Use selected files again"]:visible'
    )
    expect(visible_reuse_action).not_to_have_count(0, timeout=15000)
    visible_icon_styles = visible_reuse_action.evaluate_all(
        "buttons => buttons.map(button => getComputedStyle(button, '::after').backgroundImage)"
    )
    assert visible_icon_styles and all("M3 12a9" in style for style in visible_icon_styles)
    visible_upload_action = page.locator(
        '.pdf-upload-input button[aria-label="common.upload"]:visible'
    )
    visible_upload_icon_styles = visible_upload_action.evaluate_all(
        "buttons => buttons.map(button => getComputedStyle(button, '::after').backgroundImage)"
    )
    assert visible_upload_icon_styles and all("M12 19V5" in style for style in visible_upload_icon_styles)
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
    page.goto(local_app_url)
    expect(page.get_by_role("button", name="Confirm and start processing")).to_be_disabled()
    expect(page.get_by_role("button", name="Cancel")).to_be_visible()
    expect(page.get_by_role("button", name="Cancel")).to_be_disabled()


def test_file_selection_opens_identity_fields_but_keeps_technical_metadata_collapsed(
    page, local_app_url, one_page_pdf
):
    """A new file should invite metadata editing without expanding the long report."""
    page.goto(local_app_url)
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files(str(one_page_pdf))

    expect(page.get_by_label("Document title", exact=True)).to_be_visible(timeout=15000)
    expect(page.get_by_label("Author", exact=True)).to_be_visible()
    expect(page.get_by_role("checkbox", name="Use the file title as a fallback")).to_be_visible()

    details = page.get_by_role("button", name="Citation label and detected PDF metadata ▼")
    expect(details).to_be_visible()
    expect(page.get_by_label("Short citation label", exact=True)).to_be_hidden()
    expect(page.get_by_role("button", name="Refresh detected PDF metadata")).to_be_hidden()

    details.click()
    expect(page.get_by_label("Short citation label", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Refresh detected PDF metadata")).to_be_visible()


def test_folded_metadata_detail_header_matches_its_parent_height(page, local_app_url, one_page_pdf):
    """The nested folded control must not be a taller-looking field."""
    page.goto(local_app_url)
    page.locator(".pdf-upload-input input[type='file']").set_input_files(str(one_page_pdf))

    parent = page.locator(".top-level-accordion").filter(has_text="Document metadata").first
    child = page.locator(".document-metadata-details > button.label-wrap")
    expect(parent).to_be_visible()
    expect(child).to_be_visible()

    parent_header = parent.locator(":scope > button.label-wrap")
    parent_box = parent_header.bounding_box()
    child_box = child.bounding_box()
    assert parent_box is not None and child_box is not None
    assert abs(parent_box["height"] - child_box["height"]) <= 1


def test_dark_theme_confirmation_action_row_does_not_overlap(page, local_app_url, one_page_pdf):
    """The confirmation row must keep two separately clickable actions in dark mode."""
    page.emulate_media(color_scheme="dark")
    page.goto(local_app_url)
    expect(page.locator("body")).to_have_class(re.compile(r"(?:^|\\s)dark(?:\\s|$)"))
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files(str(one_page_pdf))

    confirm = page.get_by_role("button", name="Confirm and start processing")
    cancel = page.get_by_role("button", name="Cancel")
    expect(confirm).to_be_visible(timeout=15000)
    expect(confirm).to_be_enabled()
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
    page.goto(local_app_url)
    upload = page.locator(".pdf-upload-input input[type='file']")
    expect(upload).to_have_count(1)
    upload.set_input_files(str(one_page_pdf))

    confirm = page.get_by_role("button", name="Confirm and start processing")
    cancel = page.get_by_role("button", name="Cancel")
    expect(confirm).to_be_visible(timeout=15000)
    expect(confirm).to_be_enabled()
    expect(cancel).to_be_visible()
    confirm_box = confirm.bounding_box()
    cancel_box = cancel.bounding_box()
    assert confirm_box is not None and cancel_box is not None
    horizontal_separation = confirm_box["x"] + confirm_box["width"] <= cancel_box["x"]
    vertical_separation = confirm_box["y"] + confirm_box["height"] <= cancel_box["y"]
    assert horizontal_separation or vertical_separation


def test_system_theme_switch_preserves_the_fresh_run_controls(page, local_app_url, one_page_pdf):
    page.emulate_media(color_scheme="dark")
    page.goto(local_app_url)
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
