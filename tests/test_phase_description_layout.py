"""Browser geometry regression for the shared, bounded progress description."""
import ast
from pathlib import Path

import pytest


def test_phase_description_wraps_without_displacing_timer():
    playwright = pytest.importorskip("playwright.sync_api")
    source = Path(__file__).resolve().parents[1] / "rag_pdf_gradio_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    css = next(ast.literal_eval(n.value) for n in tree.body
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "APP_CSS" for t in n.targets))
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            for width in (320, 720):
                for state in ("running", "successful", "warning", "failed", "cancelled"):
                    positions = []
                    for length in (1, 100):
                        phase = "Extracting and evaluating with unstructured quality comparison. " * length
                        page.set_content(f'''<style>{css}</style>
                        <div style="width:{width}px"><div class="automatic-run-activity {state}">
                        <div class="automatic-run-progress-label">
                        <div class="automatic-run-progress-description">
                        <strong class="automatic-run-progress-overall">Overall progress: 13%</strong>
                        <span class="automatic-run-progress-phase">{phase}</span>
                        <span class="automatic-run-progress-details">— PDF 3/43</span></div>
                        <span class="automatic-run-batch-timing">Elapsed 00m41s<br>Est 01m04s</span>
                        </div></div><button id="below">Next control</button></div>''')
                        metrics = page.evaluate('''() => {
                            const d = document.querySelector('.automatic-run-progress-description');
                            const t = document.querySelector('.automatic-run-batch-timing');
                            return [t.getBoundingClientRect().y,
                                document.querySelector('#below').getBoundingClientRect().y,
                                d.scrollHeight > d.clientHeight,
                                d.scrollWidth <= d.clientWidth + 1,
                                getComputedStyle(document.querySelector('.automatic-run-progress-phase')).whiteSpace];
                        }''')
                        assert metrics[3]
                        assert metrics[4] == "normal"
                        if length == 100:
                            assert metrics[2]
                        positions.append(metrics[:2])
                    assert positions[0] == positions[1]
        finally:
            browser.close()
