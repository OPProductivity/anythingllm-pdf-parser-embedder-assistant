from unittest.mock import patch
import pytest
import auto_anythingllm_pipeline as a

pytestmark = pytest.mark.offline_deterministic


def test_native_page_cannot_enter_scan_margin_recovery(tmp_path):
    pdf = tmp_path / "native.pdf"
    with a.fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((70, 80), "Native content must remain intact.")
        doc.save(pdf)
    with (
        patch.object(
            a,
            "_layout_marginal_annotation_plan",
            return_value={"applied": True, "body_bounds": [60, 200]},
        ),
        patch.object(a, "_reocr_confirmed_native_body_region") as ocr,
    ):
        pages, evidence = a.apply_region_aware_native_layout(
            pdf, [{"page": 1, "text": "Native content must remain intact."}]
        )
    ocr.assert_not_called()
    assert "Native content must remain intact." in pages[0]["text"]
    assert (
        evidence["pages"][0]["outer_margin_annotation"]["reason"]
        == "no_page_sized_scan_background"
    )


def test_noisy_scan_path_stays_available_but_zero_noise_never_wins(tmp_path):
    pdf = tmp_path / "native.pdf"
    with a.fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((70, 80), "Native content remains.")
        doc.save(pdf)
    with (
        patch.object(a, "_layout_has_scan_background", return_value=True),
        patch.object(
            a,
            "_layout_marginal_annotation_plan",
            return_value={"applied": True, "body_bounds": [60, 500]},
        ),
        patch.object(
            a,
            "_reocr_confirmed_native_body_region",
            return_value="Replacement text. " * 100,
        ) as ocr,
    ):
        pages, evidence = a.apply_region_aware_native_layout(
            pdf, [{"page": 1, "text": "Native content remains."}]
        )
    ocr.assert_called_once()
    assert pages[0]["text"] == "Native content remains."
    ledger = a.native_layout_ocr_page_evidence(evidence)
    assert (
        ledger["ocr_observed_page_count"] == 1
        and ledger["selected_ocr_page_count"] == 0
    )


def test_native_ocr_ledger_respects_selection_and_excluded_pages():
    evidence = {
        "pages": [
            {
                "pdf_page": p,
                "outer_margin_annotation": {
                    "body_reocr": {
                        "attempted": True,
                        "selected": True,
                        "word_count": 30,
                    }
                },
            }
            for p in [1, 2]
        ]
    }
    ledger = a.native_layout_ocr_page_evidence(evidence, start_page=2)
    assert (
        ledger["ocr_observed_page_count"] == 2
        and ledger["selected_ocr_page_count"] == 1
    )


def test_scan_background_gate_distinguishes_full_scan_from_small_figure(tmp_path):
    pdf = tmp_path / "images.pdf"
    pix = a.fitz.Pixmap(a.fitz.csRGB, a.fitz.IRect(0, 0, 100, 100), False)
    pix.clear_with(255)
    with a.fitz.open() as doc:
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pix, keep_proportion=False)
        page = doc.new_page()
        page.insert_image(a.fitz.Rect(50, 50, 150, 150), pixmap=pix)
        doc.save(pdf)
    assert a._layout_has_scan_background(pdf, 1)
    assert not a._layout_has_scan_background(pdf, 2)


def test_citation_furniture_not_tokenized_into_people():
    assert (
        a.extract_adjacent_affiliated_name_pairs(
            "Educational Researcher, Vol. 50 No. 3, pp. 176–186 University of Florida"
        )
        == []
    )
    assert a.extract_adjacent_affiliated_name_pairs(
        "Jane Doe John Roe University of Florida"
    ) == ["Jane Doe", "John Roe"]


def test_encoded_catalog_credit_requires_visible_complete_name(tmp_path):
    path = tmp_path / "Borderlands_20--_20Gloria_20Anzald_C3_BAa_20--_201987.pdf"
    sample = [
        {
            "page": 1,
            "text": "Gloria Anzaldua\nBorderlands\nLa Frontera\nThe New Mestiza",
        }
    ]
    assert (
        a.infer_author_from_samples_or_filename(sample, path, title_hint=path.stem)[
            "author"
        ]
        == "Gloria Anzaldua"
    )


def test_inline_citation_year_is_not_document_byline():
    result = a.infer_author_from_text_samples(
        [
            {
                "page": 1,
                "text": "Some prose discusses a book\nby Otto Santa Ana, 2002, and other books.",
            }
        ],
        title_hint="Some other title",
    )
    assert result.get("author") != "Otto Santa Ana"


def test_strict_title_credit_survives_pdf_object_order_displacement(tmp_path):
    title = "Troubling the Essentialist Discourse of Brown in Education"
    text = "\n".join(
        ["Ordinary body text continues here."] * 49
        + [
            title,
            "Christopher L. Busey1",
            "and Carolyn Silva1",
            "This conceptual essay discusses racial formation.",
        ]
    )
    result = a.infer_author_from_strict_credit_blocks(
        [{"page": 1, "text": text}], tmp_path / "busey-silva.pdf", title
    )
    assert result["author"] == "Christopher L. Busey, Carolyn Silva"


def test_printed_marginal_references_are_not_handwriting():
    rows = []
    for index in range(12):
        y = 70 + index * 45
        rows.append(
            {
                "text": "Ordinary printed body prose contains many useful letters.",
                "x0": 180,
                "x1": 570,
                "y0": y,
                "y1": y + 12,
                "spans": [],
            }
        )
        rows.append(
            {
                "text": "Author, Book (1987).",
                "x0": 20,
                "x1": 145,
                "y0": y,
                "y1": y + 12,
                "spans": [
                    {
                        "text": "Author, Book (1987).",
                        "x0": 20,
                        "x1": 145,
                        "y0": y,
                        "font": "HiddenHorzOCR",
                    }
                ],
            }
        )
    plan = a._layout_marginal_annotation_plan(rows, 612, 792)
    assert not plan["applied"]
    assert plan["reason"] == "readable_margin_content_preserved"


def test_subject_heading_is_not_person():
    assert not a.looks_like_person_name("FEMINIST FILM THEORY")
    assert a.looks_like_person_name("JANE GAINES")


@pytest.mark.parametrize("scan", [True, False])
def test_margin_note_order_is_scanned_page_only(tmp_path, scan):
    pdf = tmp_path / "notes.pdf"
    with a.fitz.open() as doc:
        page = doc.new_page(width=612, height=792)
        page.insert_text((180, 180), "Body first line")
        page.insert_text((180, 220), "Body second line")
        page.insert_text((40, 200), "Reference text")
        doc.save(pdf)
    plan = {
        "applied": False,
        "reason": "readable_margin_content_preserved",
        "body_bounds": [170, 590],
    }
    with (
        patch.object(a, "_layout_marginal_annotation_plan", return_value=plan),
        patch.object(a, "_layout_has_scan_background", return_value=scan),
    ):
        pages, evidence = a.apply_region_aware_native_layout(
            pdf,
            [{"page": 1, "text": "Body first line\nReference text\nBody second line"}],
        )
    text = pages[0]["text"]
    assert "Reference text" in text
    if scan:
        assert text.index("Body second line") < text.index("Reference text")
    else:
        assert (
            evidence["pages"][0]["outer_margin_annotation"]["reason"]
            == "no_page_sized_scan_background"
        )


def test_blank_landscape_still_has_no_fold():
    from PIL import Image, ImageStat
    import rag_pdf_tools as tools

    assert (
        tools.photographed_fold_gutter_fraction(
            Image.new("RGB", (1200, 800), "white"), ImageStat
        )
        is None
    )
