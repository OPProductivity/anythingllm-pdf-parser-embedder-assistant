"""Visible credit rereads supplement, rather than replace, complete acquisition."""

import fitz
import pytest
from pathlib import Path

from author_metadata.context import AuthorEvidenceContext
from author_metadata.evidence_collection import (
    _visible_byline_projections,
    collect,
)

pytestmark = pytest.mark.offline_deterministic


def native():
    return {
        "page": 1,
        "text": "Public Memory and Shared History\nBy Benedict Vale\n"
        + "The ordinary scholarly discussion continues with evidence about shared public memories. "
        * 4,
    }


def reading(text="By Amelia Hart", confidence=95, **changes):
    return {
        "page": 1,
        "reason": "invisible_explicit_byline",
        "clip": [30, 80, 210, 110],
        "native_byline_text": "By Benedict Vale",
        "lines": [{"text": text, "confidence": confidence}],
        **changes,
    }


def test_visible_read_is_a_separate_bound_interpretation_and_preserves_bottom_prose():
    original = native()
    projected = _visible_byline_projections(original, [reading()])
    assert len(projected) == 1
    assert "By Amelia Hart" in projected[0]["text"]
    assert "By Benedict Vale" in original["text"]
    assert projected[0]["text"].splitlines()[-1] == original["text"].splitlines()[-1]
    assert projected[0]["extraction"] == "native_with_ocr_byline_projection"
    assert projected[0]["byline_projection"][0]["native"] == "By Benedict Vale"


@pytest.mark.parametrize(
    "region",
    [
        reading(confidence=60),
        reading(text="An unrelated image caption"),
        reading(native_byline_text="By Caroline North"),
        reading(reason="ordinary_full_page_ocr"),
        reading(text="By Amelia Hart,"),
    ],
)
def test_missing_role_or_physical_correspondence_does_not_rewrite_native_credit(region):
    assert not _visible_byline_projections(native(), [region])


def test_image_alt_text_cannot_hide_a_smaller_physical_byline_or_enlarge_its_crop():
    from types import SimpleNamespace
    from author_metadata.evidence_collection import _hidden_explicit_byline_lines

    spans = [
        {
            "text": "By",
            "size": 10,
            "origin": [30, 100],
            "bbox": [30, 90, 45, 102],
            "alpha": 0,
        },
        {
            "text": "Amelia Hart",
            "size": 10,
            "origin": [45, 100],
            "bbox": [45, 90, 130, 102],
            "alpha": 0,
        },
        {
            "text": "Ornately drawn illustration",
            "size": 90,
            "origin": [130, 120],
            "bbox": [130, 20, 400, 140],
            "alpha": 0,
        },
    ]
    page = SimpleNamespace(
        get_text=lambda *args, **kwargs: {"blocks": [{"lines": [{"spans": spans}]}]}
    )
    assert _hidden_explicit_byline_lines(page) == [
        {"text": "By Amelia Hart", "box": [30, 90, 130, 102]}
    ]


def test_prepositional_by_phrase_is_not_an_extra_production_crop_but_lab_can_explore_it():
    from types import SimpleNamespace
    from author_metadata.evidence_collection import _hidden_explicit_byline_lines

    span = {
        "text": "by the window now",
        "size": 10,
        "origin": [30, 100],
        "bbox": [30, 90, 130, 102],
        "alpha": 0,
    }
    page = SimpleNamespace(
        get_text=lambda *args, **kwargs: {"blocks": [{"lines": [{"spans": [span]}]}]}
    )
    assert not _hidden_explicit_byline_lines(page)
    assert _hidden_explicit_byline_lines(page, require_person=False)


def test_alternate_visible_readings_are_both_assessed_and_can_produce_a_conflict():
    from author_metadata.text_responsibility import claims
    from author_metadata.publishing_relations import select

    region = reading()
    region["alternate"] = reading(text="By Caroline North")
    projected = _visible_byline_projections(native(), [region])
    assert len(projected) == 2
    found = [
        claim
        for sample in projected
        for claim in claims(
            [sample],
            kind="generic",
            work_scope="single_work",
            title_hint="Public Memory and Shared History",
        )
    ]
    verdict, reason = select({}, found, kind="generic", work_scope="single_work")
    assert not verdict["author"]
    assert "conflict" in reason


def scan(path, *, hidden=True):
    body = "We examine the role of archives and the public interpretation of shared memories."
    with fitz.open() as picture, fitz.open() as document:
        image_page = picture.new_page()
        image_page.insert_text(
            (40, 55), "Public Memory and Shared History", fontsize=20
        )
        image_page.insert_text((40, 95), "By Amelia Hart", fontsize=16)
        for n in range(4):
            image_page.insert_text((40, 130 + n * 20), body, fontsize=10)
        page = document.new_page()
        page.insert_image(
            page.rect,
            stream=image_page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png"),
        )
        mode = 3 if hidden else 0
        page.insert_text(
            (40, 55), "Public Memory and Shared History", fontsize=20, render_mode=mode
        )
        page.insert_text((40, 95), "By Benedict Vale", fontsize=16, render_mode=mode)
        for n in range(4):
            page.insert_text((40, 130 + n * 20), body, fontsize=10, render_mode=mode)
        page = document.new_page()
        page.insert_text(
            (40, 45),
            "The later page remains part of the acquired evidence.",
            fontsize=12,
        )
        document.save(path)


def test_hidden_explicit_byline_adds_a_crop_without_skipping_full_page_or_later_evidence(
    tmp_path, monkeypatch
):
    from author_metadata import evidence_collection as module

    path = tmp_path / "document.pdf"
    scan(path)

    def read(prepared, task, executable):
        return [{**task, "text": "Read requested image", "lines": []}], [
            {"kind": "ocr", **task, "status": "read"}
        ]

    monkeypatch.setattr(module, "_read_region", read)
    acquired = collect(AuthorEvidenceContext.from_samples([], path=path, ocr_workers=1))
    trace = acquired.evidence_bundle["trace"]
    assert trace["native_plan"] == [1, 2]
    assert any(t["reason"] == "invisible_explicit_byline" for t in trace["ocr_plan"])
    assert any(
        t["reason"] == "invisible_native_text_overlay" for t in trace["ocr_plan"]
    )
    assert trace["complete"]


def test_visible_native_credit_does_not_trigger_a_hidden_transcription_reread(
    tmp_path, monkeypatch
):
    from author_metadata import evidence_collection as module

    path = tmp_path / "document.pdf"
    scan(path, hidden=False)
    monkeypatch.setattr(
        module,
        "_read_region",
        lambda doc, task, exe: (
            [{**task, "text": "Read requested image", "lines": []}],
            [{"kind": "ocr", **task, "status": "read"}],
        ),
    )
    acquired = collect(AuthorEvidenceContext.from_samples([], path=path, ocr_workers=1))
    assert not any(
        t["reason"] == "invisible_explicit_byline"
        for t in acquired.evidence_bundle["trace"]["ocr_plan"]
    )


def test_actual_ocr_reads_the_visible_byline_without_expected_author_input(tmp_path):
    import shutil

    if not (
        shutil.which("tesseract")
        or Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe").is_file()
    ):
        pytest.skip("Tesseract is not installed")
    from author_metadata.dispatcher import infer_author

    path = tmp_path / "document.pdf"
    scan(path)
    with fitz.open(path) as document:
        samples = [
            {"page": n + 1, "text": page.get_text("text", sort=True)}
            for n, page in enumerate(document)
        ]
    result = infer_author(
        AuthorEvidenceContext.from_samples(
            samples, path=path, title_hint="Public Memory and Shared History"
        )
    )
    assert result["author"] == "Amelia Hart"
    review = result["evidence_review"]
    assert review["collection"]["complete"]
    assert any(
        t["reason"] == "invisible_explicit_byline"
        for t in review["collection"]["ocr_plan"]
    )
    assert any(c["author"] == "Benedict Vale" for c in review["claims"])
    assert any(
        c["author"] == "Amelia Hart" and c["priority"] == 108 for c in review["claims"]
    )
