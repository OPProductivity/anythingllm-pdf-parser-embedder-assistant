import fitz
import pytest
from author_metadata.context import AuthorEvidenceContext
from author_metadata.dispatcher import infer_author
from author_metadata.native_article import mixed_image_line_views
from author_metadata.profile import DocumentProfile

pytestmark = pytest.mark.offline_deterministic


def test_readable_native_name_cannot_be_overwritten_by_overlapping_ocr():
    page = {
        "page": 1,
        "width": 595,
        "height": 842,
        "horizontal_lines": [
            {
                "text": "Amelia Hart",
                "box": [50, 110, 180, 130],
                "size": 14,
                "baseline": 125,
            }
        ],
    }
    region = {
        "page": 1,
        "clip": [45, 105, 200, 140],
        "scale": 1,
        "lines": [{"text": "Benedict Vale", "box": [5, 5, 135, 25]}],
    }
    assert mixed_image_line_views(page, [region]) == []


def test_small_raster_name_band_stays_intact_and_combines_with_native_title(tmp_path):
    strip = fitz.open()
    p = strip.new_page(width=300, height=32)
    p.insert_text((5, 24), "Amelia Hart", fontsize=18)
    png = p.get_pixmap(matrix=fitz.Matrix(3, 3)).tobytes("png")
    strip.close()
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 70), "Collective Memory and Public Life", fontsize=24)
    page.insert_image(fitz.Rect(50, 110, 350, 142), stream=png)
    page.insert_text((50, 170), "University of Westport", fontsize=10)
    page.insert_text((50, 205), "Abstract", fontsize=12)
    for i in range(5):
        page.insert_text(
            (50, 240 + i * 18),
            "This study investigates historical records and the shared life of public communities.",
            fontsize=10,
        )
    path = tmp_path / "document-100.pdf"
    doc.save(path)
    doc.close()
    context = AuthorEvidenceContext.from_samples(
        [],
        path=path,
        title_hint="document-100",
        profile=DocumentProfile("generic", (), (), work_scope="single_work"),
    )
    result = infer_author(context)
    trace = result["evidence_review"]["collection"]
    bands = [t for t in trace["ocr_plan"] if t["reason"] == "narrow_text_image"]
    assert len(bands) == 1 and bands[0]["clip"] == [50, 110, 350, 142]
    assert result["author"] == "Amelia Hart"
    assert any(
        c["family"] == "physical_complete_mixed_roster" and c["author"] == "Amelia Hart"
        for c in result["evidence_review"]["claims"]
    )


def test_visible_byline_reread_can_correct_an_invisible_ocr_transcription(tmp_path):
    rendered = fitz.open()
    p = rendered.new_page()
    p.insert_text((50, 70), "Collective Memory and Public Life", fontsize=24)
    p.insert_text((50, 130), "By Amelia Hart", fontsize=14)
    p.insert_text((50, 180), "Abstract", fontsize=12)
    for i in range(7):
        p.insert_text(
            (50, 220 + i * 18),
            "This study investigates records, communities, and the preservation of public memory.",
            fontsize=10,
        )
    png = p.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
    rendered.close()
    doc = fitz.open()
    p = doc.new_page()
    p.insert_image(p.rect, stream=png)
    p.insert_text(
        (50, 70), "Collective Memory and Public Life", fontsize=24, render_mode=3
    )
    p.insert_text((50, 130), "By Amelia Hert", fontsize=14, render_mode=3)
    p.insert_text((50, 180), "Abstract", fontsize=12, render_mode=3)
    for i in range(7):
        p.insert_text(
            (50, 220 + i * 18),
            "This study investigates records, communities, and the preservation of public memory.",
            fontsize=10,
            render_mode=3,
        )
    path = tmp_path / "document-101.pdf"
    doc.save(path)
    doc.close()
    result = infer_author(
        AuthorEvidenceContext.from_samples(
            [],
            path=path,
            title_hint="Collective Memory and Public Life",
            profile=DocumentProfile("generic", (), (), work_scope="single_work"),
        )
    )
    assert result["author"] == "Amelia Hart"
    assert any(
        t["reason"] == "invisible_native_text_overlay"
        for t in result["evidence_review"]["collection"]["ocr_plan"]
    )
    assert any(
        c.get("reread_basis") and c["author"] == "Amelia Hart"
        for c in result["evidence_review"]["claims"]
    )


def test_visible_native_byline_has_priority_over_disagreeing_overlay_reread():
    from author_metadata.text_responsibility import claims
    from author_metadata.publishing_relations import select

    common = {"page": 1, "visible_byline_texts": ["By Amelia Hart"]}
    native = {
        **common,
        "text": "Collective Memory and Public Life\nBy Amelia Hart\nAbstract\nA discussion of public memory and records.",
    }
    reread = {
        **common,
        "text": "Collective Memory and Public Life\nBy Benedict Vale\nAbstract\nA discussion of public memory and records.",
        "direct_byline_rereads": [{"text": "By Benedict Vale", "confidence": 95}],
    }
    found = claims(
        [native, reread],
        kind="generic",
        work_scope="single_work",
        title_hint="Collective Memory and Public Life",
    )
    assert (
        select({}, found, kind="generic", work_scope="single_work")[0]["author"]
        == "Amelia Hart"
    )
