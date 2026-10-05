from copy import deepcopy
import pytest
import pdf_running_furniture as f

pytestmark = pytest.mark.offline_deterministic


def row(text, x, y, w=10, h=10):
    return {
        "text": text,
        "normalized": text,
        "x0": x,
        "y0": y,
        "x1": x + w,
        "y1": y + h,
    }


def layout(number, fragments=7):
    rows = [
        row(
            "A substantial body sentence establishes ordinary printed prose.",
            100,
            70 + i * 25,
            400,
        )
        for i in range(16)
    ]
    rows += [row("~x", 30, 160 + i * 16) for i in range(fragments)]
    rows += [row(str(number + 2), 45, 620)]
    return {"width": 600, "height": 800, "rows": rows}


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(
        f, "ensure_tesseract_runtime", lambda: {"available": True, "executable": "test"}
    )
    monkeypatch.setattr(
        f,
        "recognize_margin",
        lambda *a: (
            [{"text": "A running author", "confidence": 95, "angle": 270}],
            "observed",
        ),
    )


def test_confirmed_fragments_and_number_sequence_preserve_input(runtime):
    layouts = {n: layout(n) for n in (1, 2, 3)}
    before = deepcopy(layouts)
    plans = f.plan_rotated_running_furniture("unused", layouts)
    assert layouts == before
    assert all(p["excluded_row_indices"] == list(range(16, 24)) for p in plans.values())
    assert all(
        p["page_number_sequences"]["offset-2"]["printed_page_offset"] == 2
        for p in plans.values()
    )
    assert all(
        "supporting_pages" not in r for p in plans.values() for r in p["removed"]
    )


def test_short_low_confidence_candidate_needs_independent_seeds(runtime, monkeypatch):
    layouts = {n: layout(n, 5 if n == 3 else 7) for n in (1, 2, 3)}
    for i, r in enumerate(layouts[3]["rows"][16:21]):
        r.update(y0=160 + i * 24, y1=170 + i * 24)
    monkeypatch.setattr(
        f,
        "recognize_margin",
        lambda path, n, *a: (
            [
                {
                    "text": "A running author",
                    "confidence": 65 if n == 3 else 95,
                    "angle": 270,
                }
            ],
            "observed",
        ),
    )
    plans = f.plan_rotated_running_furniture("unused", layouts)
    assert plans[3]["excluded_row_indices"] == list(range(16, 22))


@pytest.mark.parametrize(
    "case",
    [
        "missing_runtime",
        "ocr_failure",
        "no_repeat",
        "low_seed",
        "different_rotation",
        "different_label",
        "different_side",
        "different_position",
        "same_page_duplicate",
    ],
)
def test_no_unverified_deletion(runtime, monkeypatch, case):
    layouts = {n: layout(n) for n in (1, 2)}
    if case == "missing_runtime":
        monkeypatch.setattr(f, "ensure_tesseract_runtime", lambda: {"available": False})
    elif case == "ocr_failure":
        monkeypatch.setattr(f, "recognize_margin", lambda *a: ([], "ocr_failed"))
    elif case == "no_repeat":
        layouts = {1: layout(1)}
    elif case == "low_seed":
        monkeypatch.setattr(
            f,
            "recognize_margin",
            lambda *a: (
                [{"text": "A running author", "confidence": 65, "angle": 270}],
                "observed",
            ),
        )
    elif case == "different_rotation":
        monkeypatch.setattr(
            f,
            "recognize_margin",
            lambda path, n, *a: (
                [
                    {
                        "text": "A running author",
                        "confidence": 95,
                        "angle": 90 if n == 1 else 270,
                    }
                ],
                "observed",
            ),
        )
    elif case == "different_label":
        monkeypatch.setattr(
            f,
            "recognize_margin",
            lambda path, n, *a: (
                [
                    {
                        "text": "A running author"
                        if n == 1
                        else "Different running title",
                        "confidence": 95,
                        "angle": 270,
                    }
                ],
                "observed",
            ),
        )
    elif case == "different_side":
        for r in layouts[2]["rows"][16:23]:
            r.update(x0=560, x1=570)
    elif case == "different_position":
        for r in layouts[2]["rows"][16:23]:
            r["y0"] += 100
            r["y1"] += 100
    else:
        layouts = {1: layout(1)}
        layouts[1]["rows"] += [row("~x", 30, 360 + i * 16) for i in range(7)]
    plans = f.plan_rotated_running_furniture("unused", layouts)
    assert all(not p["excluded_row_indices"] for p in plans.values())
    assert all(p["unresolved_candidate_count"] for p in plans.values())


@pytest.mark.parametrize("case", ["inside_body", "irregular_values", "unstable_y"])
def test_number_lookalikes_retained(runtime, case):
    layouts = {n: layout(n) for n in (1, 2, 3)}
    for n, part in layouts.items():
        number = part["rows"][-1]
        if case == "inside_body":
            number.update(x0=110, x1=120)
        elif case == "irregular_values":
            number.update(text=str(n * 10), normalized=str(n * 10))
        else:
            number.update(y0=400 + n * 60, y1=410 + n * 60)
    plans = f.plan_rotated_running_furniture("unused", layouts)
    assert all(23 not in p["excluded_row_indices"] for p in plans.values())


def test_body_only_and_readable_sidebar_do_not_call_ocr(runtime, monkeypatch):
    monkeypatch.setattr(f, "recognize_margin", lambda *a: pytest.fail("unexpected OCR"))
    part = layout(1)
    part["rows"] = part["rows"][:16]
    assert f.plan_rotated_running_furniture("unused", {1: part}) == {}
    part["rows"] += [
        row("A useful longer sidebar sentence.", 20, 160 + i * 16, 65) for i in range(7)
    ]
    assert f.plan_rotated_running_furniture("unused", {1: part}) == {}


def test_failed_runtime_stops_further_calls(runtime, monkeypatch):
    calls = []

    def fail(*args):
        calls.append(args)
        return [], "ocr_failed"

    monkeypatch.setattr(f, "recognize_margin", fail)
    plans = f.plan_rotated_running_furniture(
        "unused", {n: layout(n) for n in (1, 2, 3)}
    )
    assert len(calls) == 1
    assert all(not p["excluded_row_indices"] for p in plans.values())


def test_bounded_candidate_pages_are_explicit_and_preserved(runtime):
    plans = f.plan_rotated_running_furniture(
        "unused", {n: layout(n) for n in (1, 2, 3)}, max_candidate_pages=2
    )
    assert plans[1]["excluded_row_indices"]
    assert plans[2]["excluded_row_indices"]
    assert plans[3]["status"] == "candidate_page_budget_reached"
    assert not plans[3]["excluded_row_indices"]
    assert all(23 not in plan["excluded_row_indices"] for plan in plans.values())
    assert plans[1]["unresolved_page_number_count"] == 1
    assert plans[1]["status"] == "partial_confirmation"
