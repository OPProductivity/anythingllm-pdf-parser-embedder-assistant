"""Reversible metadata-region proposals and bounded OCR reconsideration."""

from pathlib import Path

import fitz
import pytest

from author_metadata.field_map import (
    _check_ocr, _line_compositions, map_atoms, map_pdf_opening, review_author_field_map,
)


pytestmark = pytest.mark.offline_deterministic


def atom(index, text, x0, y0, x1, y1, *, block=0, line=0, page=1):
    return {"id": f"p{page}-n{index}", "source": "native_word", "text": text,
            "page": page, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "source_block": block, "source_line": line, "source_word": index,
            "font_size": 12, "font": "Times"}


def test_mixed_extractor_line_keeps_overlapping_title_and_byline_compositions():
    atoms = [atom(0, "Racial", 50, 80, 100, 92),
             atom(1, "Politics", 103, 80, 158, 92),
             atom(2, "By", 165, 80, 180, 92),
             atom(3, "Laura", 183, 80, 220, 92),
             atom(4, "Pulido", 223, 80, 270, 92)]
    groups = _line_compositions(atoms, 600)
    assert any(group["kind"] == "extractor_line" and len(group["atom_ids"]) == 5
               for group in groups)
    assert any(group["kind"] == "before_by" and group["text"] == "Racial Politics"
               for group in groups)
    assert any(group["kind"] == "byline_tail" and group["text"] == "By Laura Pulido"
               for group in groups)
    assert [item["text"] for item in atoms] == ["Racial", "Politics", "By", "Laura", "Pulido"]


def test_adjacent_columns_same_height_remain_alternative_regions():
    atoms = [atom(0, "Abstract", 40, 180, 100, 193),
             atom(1, "Introduction", 105, 180, 180, 193),
             atom(2, "By", 340, 180, 355, 193),
             atom(3, "Laura", 360, 180, 395, 193),
             atom(4, "Pulido", 400, 180, 440, 193)]
    groups = _line_compositions(atoms, 600)
    assert any(group["text"] == "By Laura Pulido" for group in groups)
    assert not any(group["kind"] == "aligned_line" and
                   "Abstract" in group["text"] and "Pulido" in group["text"]
                   for group in groups)
    mapped = map_atoms(atoms, 1, 600, 800, "Racial Politics")
    assert "abstract" in {row["role"] for row in mapped["field_candidates"]}
    assert "author_credit" in {row["role"] for row in mapped["field_candidates"]}


def test_later_extractor_line_uses_its_own_word_indexes_for_byline_split():
    atoms = [atom(0, "Journal", 50, 20, 100, 32, line=0),
             atom(1, "Issue", 103, 20, 150, 32, line=0),
             atom(2, "Racial", 50, 80, 100, 92, line=1),
             atom(3, "Politics", 103, 80, 158, 92, line=1),
             atom(4, "By", 165, 80, 180, 92, line=1),
             atom(5, "Laura", 183, 80, 220, 92, line=1),
             atom(6, "Pulido", 223, 80, 270, 92, line=1)]
    groups = _line_compositions(atoms, 600)
    assert any(g["kind"] == "before_by" and g["text"] == "Racial Politics" for g in groups)
    assert any(g["kind"] == "byline_tail" and g["text"] == "By Laura Pulido" for g in groups)


def test_by_in_another_line_does_not_split_an_unrelated_title():
    atoms = [atom(0, "Published", 50, 20, 100, 32, line=0),
             atom(1, "By", 103, 20, 120, 32, line=0),
             atom(2, "Journal", 125, 20, 175, 32, line=0),
             atom(3, "A", 50, 80, 65, 92, line=1),
             atom(4, "Separate", 70, 80, 120, 92, line=1),
             atom(5, "Title", 125, 80, 160, 92, line=1)]
    groups = _line_compositions(atoms, 600)
    assert not any(g["kind"] == "byline_tail" and g["text"] == "Separate Title" for g in groups)


def _sample_pdf(path: Path):
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((60, 100), "Racial Politics", fontsize=20)
    page.insert_text((60, 130), "By Laura Pulido", fontsize=13)
    page.insert_text((60, 200), "Abstract", fontsize=11)
    doc.save(path)
    doc.close()


def test_ocr_empty_first_crop_is_retried_without_changing_author(tmp_path):
    path = tmp_path / "sample.pdf"
    _sample_pdf(path)
    calls = []

    def fake_ocr(pdf_path, page, fraction, psm, *, tesseract):
        calls.append((page, fraction, psm))
        if len(calls) == 1:
            return {"status": "empty", "text": "", "atoms": []}
        return {"status": "ok", "text": "Racial Politics\nBy Laura Pulido\nAbstract",
                "atoms": [atom(0, "By", 60, 117, 75, 130),
                          atom(1, "Laura", 78, 117, 110, 130),
                          atom(2, "Pulido", 114, 117, 156, 130)],
                "recognition": {"psm": psm}}

    result = review_author_field_map(path, title_hint="Racial Politics",
                                     author="Laura Pulido", author_source="pdf_metadata",
                                     ocr_runner=fake_ocr)
    assert len(calls) == 2
    assert [row["status"] for row in result["checks"]] == ["empty", "agrees"]
    assert result["checks"][1]["independent_ocr_profile"]
    assert "independent_author_strategy_source" in result["checks"][1]
    assert result["current_author"] == "Laura Pulido"
    assert result["changes_final_metadata"] is False


def test_conflicting_ocr_compositions_remain_in_ledger(tmp_path):
    path = tmp_path / "sample.pdf"
    _sample_pdf(path)
    replies = iter(("Racial Politics\nBy John Smith", "Racial Politics\nBy Laura Pulido"))

    def fake_ocr(pdf_path, page, fraction, psm, *, tesseract):
        text = next(replies)
        words = ("John", "Smith") if "John" in text else ("Laura", "Pulido")
        return {"status": "ok", "text": text,
                "atoms": [atom(0, "By", 60, 117, 75, 130),
                          atom(1, words[0], 78, 117, 110, 130),
                          atom(2, words[1], 114, 117, 156, 130)],
                "recognition": {}}

    result = review_author_field_map(path, title_hint="Racial Politics",
                                     author="Laura Pulido", ocr_runner=fake_ocr)
    assert [row["status"] for row in result["checks"]] == ["disagrees", "agrees"]
    assert result["final_review"] == "conflicting_ocr_compositions"
    assert any(row["reason"] == "ocr_explicit_credit_conflicts_with_native"
               for row in result["reconsiderations"])


def test_frontmatter_expansion_finds_later_title_page(tmp_path):
    path = tmp_path / "frontmatter.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((60, 100), "Copyright 2026 Example Press")
    doc.new_page().insert_text((60, 100), "Dedicated to my family")
    doc.new_page().insert_text((60, 100), "Contents")
    doc.new_page().insert_text((60, 100), "Preface")
    doc.new_page().insert_text((60, 100), "Racial Politics")
    doc.save(path)
    doc.close()
    pages = map_pdf_opening(path, "Racial Politics")
    assert [page["page"] for page in pages] == [1, 2, 3, 4, 5]
    assert "visible_work_title" in pages[-1]["page_roles"]


@pytest.mark.parametrize("effort", ["thorough", "extremely_thorough"])
def test_shadow_review_peer_and_both_crop_experts_receive_the_selected_mode(tmp_path, monkeypatch, effort):
    from author_metadata import dispatcher

    path = tmp_path / "sample.pdf"
    _sample_pdf(path)
    observed = []
    monkeypatch.setattr(dispatcher, "infer_author", lambda context: observed.append(context.recognition_effort) or {})

    def fake_ocr(_pdf_path, _page, _fraction, _psm, **_kwargs):
        return {"status": "ok", "text": "Racial Politics\nBy Laura Pulido",
                "atoms": [atom(0, "By", 60, 117, 75, 130),
                          atom(1, "Laura", 78, 117, 110, 130),
                          atom(2, "Pulido", 114, 117, 156, 130)], "recognition": {}}

    report = review_author_field_map(path, title_hint="Racial Politics", author="Laura Pulido",
        ocr_peer_pages=[{"page": 1, "text": "By Laura Pulido"}], ocr_runner=fake_ocr,
        recognition_effort=effort)
    assert len(observed) >= 3 and set(observed) == {effort}
    assert report["recognition_effort"] == effort
    assert report["changes_final_metadata"] is False


def test_ocr_affiliation_name_shape_does_not_override_title_linked_author():
    native = map_atoms([
        atom(0, "The", 50, 80, 70, 95), atom(1, "Contours", 75, 80, 150, 95),
        atom(2, "Adam", 50, 120, 80, 135, line=1),
        atom(3, "Anzel", 84, 120, 125, 135, line=1),
        atom(4, "Aalborg,", 50, 140, 105, 155, line=2),
        atom(5, "Denmark", 109, 140, 160, 155, line=2),
    ], 1, 600, 800, "The Contours")
    ocr = map_atoms([
        atom(0, "The", 50, 80, 70, 95), atom(1, "Contours", 75, 80, 150, 95),
        atom(2, "Adam", 50, 120, 80, 135, line=1),
        atom(3, "Anzel", 84, 120, 125, 135, line=1),
        atom(4, "Aalborg,", 50, 140, 105, 155, line=2),
        atom(5, "Denmark", 109, 140, 160, 155, line=2),
    ], 1, 600, 800, "The Contours")
    result = _check_ocr("Adam Anzel", "The Contours\nAdam Anzel\nAalborg, Denmark",
                        ocr, "The Contours", native)
    assert result["status"] == "agrees"


def test_ocr_near_miss_is_recognition_uncertainty_not_different_author():
    native = map_atoms([
        atom(0, "Populism", 50, 80, 120, 95),
        atom(1, "as", 124, 80, 140, 95), atom(2, "Truth", 144, 80, 185, 95),
        atom(3, "Gunn", 50, 120, 82, 135, line=1),
        atom(4, "Enli1", 86, 120, 120, 135, line=1),
    ], 1, 600, 800, "Populism as Truth")
    ocr = map_atoms([
        atom(0, "Populism", 50, 80, 120, 95),
        atom(1, "as", 124, 80, 140, 95), atom(2, "Truth", 144, 80, 185, 95),
        atom(3, "Gunn", 50, 120, 82, 135, line=1),
        atom(4, "Enii!", 86, 120, 120, 135, line=1),
    ], 1, 600, 800, "Populism as Truth")
    result = _check_ocr("Gunn Enli", "Populism\nGunn Enii!", ocr,
                        "Populism as Truth", native)
    assert result["status"] == "inconclusive"
    assert result["reason"] == "ocr_spelling_variant_at_credit"


def test_multiple_ocr_bylines_need_work_scope():
    result = _check_ocr("", "By Denys Dmytruk\nBy Justin Tomczyk", None, "Issue 1")
    assert result["status"] == "inconclusive"
    assert result["reason"] == "multiple_ocr_credits_work_scope_ambiguous"


def test_flattened_ocr_byline_keeps_field_boundary_unresolved():
    result = _check_ocr("Laura Pulido", "Racial Politics\nBy Laura Pulido",
                        None, "Racial Politics")
    assert result["status"] == "inconclusive"
    assert result["reason"] == "field_boundary_unresolved_flattened_ocr"
    assert result["textual_surname_match"] is True
    assert result["ocr_credit_candidates"] == ["Laura Pulido"]


def test_one_of_two_authors_is_not_full_ocr_agreement():
    result = _check_ocr("Anthony Harkins, Meredith McCarroll",
                        "By Meredith McCarroll", None, "Appalachian Reckoning")
    assert result["status"] == "inconclusive"
    assert result["reason"] == "only_some_author_surnames_corroborated"


def test_contents_entry_is_not_a_work_title():
    mapped = map_atoms([
        atom(0, "Contents", 50, 50, 120, 65),
        atom(1, "Navigating", 50, 140, 120, 155, line=1),
        atom(2, "Neoreaction", 125, 140, 230, 155, line=1),
    ], 1, 600, 800, "Navigating Neoreaction")
    assert "contents" in mapped["page_roles"]
    assert "visible_work_title" not in mapped["page_roles"]
    assert any(field["role"] == "contents_entry_title"
               for field in mapped["field_candidates"])


def test_right_aligned_article_byline_can_be_rechecked_after_contents():
    native = map_atoms([
        atom(0, "Navigating", 50, 80, 145, 100),
        atom(1, "Neoreaction", 150, 80, 260, 100),
        atom(2, "Justin", 350, 108, 385, 120, line=1),
        atom(3, "Tomczyk", 390, 108, 440, 120, line=1),
    ], 5, 480, 800, "Navigating Neoreaction")
    assert any(field.get("work_link") == "right_of_title_same_opening"
               for field in native["field_candidates"])
    ocr = map_atoms([
        atom(0, "Navigating", 50, 80, 145, 100),
        atom(1, "Neoreaction", 150, 80, 260, 100),
        atom(2, "Justin", 350, 108, 385, 120, line=1),
        atom(3, "Tomezyk", 390, 108, 440, 120, line=1),
    ], 5, 480, 800, "Navigating Neoreaction")
    result = _check_ocr("", "Navigating Neoreaction\nJustin Tomezyk",
                        ocr, "Navigating Neoreaction", native)
    assert result["status"] == "candidate_for_review"
    assert result["ocr_credit_candidates"] == ["Justin Tomczyk"]
