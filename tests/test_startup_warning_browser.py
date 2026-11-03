"""Visual timing of the top AnythingLLM availability warning."""

import pytest

pytestmark = pytest.mark.ui_local


def test_warning_uses_red_background_without_a_white_overlay(page):
    from rag_pdf_gradio_app import APP_CSS, anythingllm_startup_status_html

    warning = anythingllm_startup_status_html(health={"reachable": False})
    page.set_content(
        f'<style>{APP_CSS}</style>'
        f'<div class="anythingllm-startup-status-module">{warning}</div>'
    )
    style = page.evaluate("""() => {
      const panel = document.querySelector('.anythingllm-startup-status-module');
      const overlay = getComputedStyle(panel, '::before');
      return {
        background: getComputedStyle(panel).backgroundColor,
        overlayContent: overlay.content,
      };
    }""")
    assert style == {
        "background": "rgb(254, 226, 226)",
        "overlayContent": "none",
    }
