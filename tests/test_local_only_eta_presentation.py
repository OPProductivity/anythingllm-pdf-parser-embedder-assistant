import ast
from pathlib import Path

import pytest

import rag_pdf_gradio_app as app


@pytest.mark.parametrize("seconds", [0, 1, 20, 149, 900, 18000])
@pytest.mark.parametrize("cached", [0, 1])
def test_only_plain_local_mode_bypasses_opening_discount(seconds, cached):
    for mode in (app.MODE_LOCAL_ONLY_LABEL, app.MODE_LOCAL_WITH_LOGS_LABEL,
                 app.MODE_NATIVE_UPLOAD_LABEL):
        actual = app.opening_eta_presentation_seconds(
            seconds, exact_cache_reuse_records=cached, mode=mode)
        expected = seconds if mode == app.MODE_LOCAL_ONLY_LABEL else app.opening_eta_presentation_seconds(
            seconds, exact_cache_reuse_records=cached)
        assert actual == expected


def test_every_production_opening_eta_call_provides_mode():
    tree = ast.parse(Path(app.__file__).read_text(encoding="utf-8-sig"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "opening_eta_presentation_seconds"]
    assert len(calls) == 7
    assert all(any(k.arg == "mode" for k in call.keywords) for call in calls)


def test_local_reprice_does_not_invent_a_discount():
    assert app.reprice_presentation_expected_seconds(
        previous_expected_seconds=149, previous_presentation_expected_seconds=149,
        new_expected_seconds=200, is_material_reprice=True,
        preserve_existing_remaining_discount=True, elapsed_seconds=30) == 200
