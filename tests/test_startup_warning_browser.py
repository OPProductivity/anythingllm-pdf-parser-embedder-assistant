"""Visual timing of the top AnythingLLM availability warning."""

import pytest

pytestmark = pytest.mark.ui_local


def test_warning_starts_pale_then_unveils_red_after_five_seconds(page):
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
        overlay: overlay.backgroundColor,
        duration: overlay.animationDuration,
        opacity: overlay.opacity,
      };
    }""")
    assert style == {
        "background": "rgb(254, 226, 226)",
        "overlay": "rgba(255, 255, 255, 0.58)",
        "duration": "5s",
        "opacity": "1",
    }
    page.wait_for_function(
        "getComputedStyle(document.querySelector('.anythingllm-startup-status-module'), '::before').opacity === '0'",
        timeout=6500,
    )
