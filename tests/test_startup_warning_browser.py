"""Visual timing of the top AnythingLLM availability warning."""

import pytest

pytestmark = pytest.mark.ui_local


def test_warning_uses_red_background_without_a_white_overlay(page):
    from rag_pdf_gradio_app import APP_CSS, APP_THEME_FOUNDATION_HEAD, anythingllm_startup_status_html, demo

    config = demo.config
    close = next(component for component in config["components"]
                 if component.get("props", {}).get("elem_id") == "close-anythingllm-startup-status")
    close_click = next(dependency for dependency in config["dependencies"]
                       if (close["id"], "click") in dependency.get("targets", []))
    assert 'anythingllm-warning-dismissed' in close_click["js"]
    assert close_click["backend_fn"] is True
    timer = next(component for component in config["components"]
                 if component.get("type") == "timer" and component["id"] in close_click["outputs"])
    assert timer["props"]["active"] is True

    warning = anythingllm_startup_status_html(health={"reachable": False})
    page.set_content(
        APP_THEME_FOUNDATION_HEAD + f'<style>{APP_CSS}</style>'
        '<div id="anythingllm-startup-status-module" class="anythingllm-startup-status-module"></div>'
    )
    assert page.locator("#anythingllm-startup-status-module").evaluate(
        "node => getComputedStyle(node).backgroundColor"
    ) == "rgb(254, 215, 215)"
    page.locator("#anythingllm-startup-status-module").evaluate(
        "(node, html) => { node.innerHTML = html; }", warning
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
        "background": "rgb(254, 215, 215)",
        "overlayContent": "none",
    }
    page.evaluate("document.documentElement.classList.add('dark')")
    assert page.locator("#anythingllm-startup-status-module").evaluate(
        "node => getComputedStyle(node).backgroundColor"
    ) == "rgb(127, 29, 29)"
    page.evaluate("document.documentElement.classList.remove('dark')")
    assert page.locator(".anythingllm-local-mode-note").inner_text().startswith(
        "PDF processing and parsing locally is still available"
    )
    page.evaluate("document.documentElement.classList.add('anythingllm-warning-dismissed')")
    assert page.locator("#anythingllm-startup-status-module").is_hidden()
    page.evaluate("""() => {
      document.querySelector('#anythingllm-startup-status-module').innerHTML += '<span>timer update</span>';
    }""")
    assert page.locator("#anythingllm-startup-status-module").is_hidden()
