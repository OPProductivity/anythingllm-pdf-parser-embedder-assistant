"""Physical credit evidence must survive neutral names without role guessing."""

from unittest.mock import patch
import subprocess

import fitz
import pytest

from author_metadata.context import AuthorEvidenceContext
from author_metadata.profile import DocumentProfile
from author_metadata.portable_layout import (
    infer,
    layout_credit,
    publisher_credit,
    read_tsv,
    untrack,
    _collapsed_byline,
)
from author_metadata.structured_credit import _words

pytestmark = pytest.mark.offline_deterministic


def line(text, y, size=12, x=60, right=450, confidence=95):
    return {
        "text": text,
        "box": [x, y, right, y + size],
        "size": size,
        "confidence": confidence,
    }


def body(y):
    return [
        line("This chapter studies the history of public space and social memory.", y),
        line(
            "The argument concerns many changes in the cultural life of cities.", y + 16
        ),
    ]


def test_indented_abstract_remains_inside_title_column():
    lines = [
        line("Collective Memory and Public Life", 100, 20),
        line("Amelia Hart", 145, 12, right=180),
        line(
            "Abstract: This study investigates collective memory and public life.",
            180,
            11,
            x=125,
            right=450,
        ),
        line(
            "Our findings describe how different communities preserve and interpret their shared historical records.",
            196,
            11,
            x=125,
            right=450,
        ),
    ]
    assert layout_credit(lines, 600, 850)["author"] == "Amelia Hart"


def test_centered_capital_title_does_not_need_twenty_percent_font_difference():
    lines = [
        line("COLLECTIVE MEMORY:", 100, 15, x=140, right=360),
        line("PUBLIC LIFE AND HISTORICAL CHANGE", 118, 15, x=70, right=430),
        line("AMELIA HART", 150, 14, x=190, right=310),
    ]
    lines += [
        line(
            "The study investigates how communities preserve shared historical records.",
            190 + i * 15,
            11,
        )
        for i in range(10)
    ]
    assert layout_credit(lines, 500, 850)["author"] == "AMELIA HART"


def test_flowing_prose_cannot_be_a_work_title_for_a_section_heading():
    lines = [
        line(
            "Ordinary paragraph text describes historical change in public memory and collective life.",
            100 + i * 14,
            12,
        )
        for i in range(6)
    ]
    lines += [line("PUBLIC MEMORY AS A METHOD", 205, 9), *body(235)]
    assert not layout_credit(lines, 600, 850)["author"]


def test_right_aligned_credit_can_follow_a_mid_page_work_heading():
    lines = [
        line("The previous chapter ends here.", 50),
        line("Cities and Public", 280, 24, right=320),
        line("Memory", 306, 24, right=200),
        line("Amina Rao", 350, x=370, right=450),
        *body(390),
    ]
    assert layout_credit(lines, 600, 850)["author"] == "Amina Rao"


def test_product_tier_in_adjacent_column_is_not_author_of_left_panel():
    lines = [
        line("What you can do:", 172, 12, x=57, right=161),
        line("Marketing Hub Enterprise", 193, 12, x=447, right=574),
        line("With Marketing Hub Enterprise, you can do all of that and:", 213, 10, x=447, right=724),
        line("These features help teams manage customer journeys and campaigns.", 230, 10, x=447, right=724),
        line("Reports and analytics show how campaigns contribute to revenue.", 245, 10, x=447, right=724),
    ]
    assert not layout_credit(lines, 800, 850)["author"]


def test_quoted_endorsement_cannot_be_a_heading_for_its_speaker():
    lines = [
        line("“An excellent history of cities”", 100, 24),
        line("Amina Rao", 150),
        *body(190),
    ]
    assert not layout_credit(lines, 600, 850)["author"]


@pytest.mark.parametrize(
    "credit",
    ["Reviewed by Amina Rao", "Translated by Amina Rao", "Edited by Amina Rao"],
)
def test_role_credit_does_not_become_work_author(credit):
    lines = [line("Cities and Public Memory", 100, 24), line(credit, 150), *body(190)]
    assert not layout_credit(lines, 600, 850)["author"]


def test_byline_above_title_requires_independent_repeated_header():
    lines = [
        line("Amina Rao", 45, x=350),
        line("Cities and Public Memory", 90, 24),
        line("A History of Social Change", 118, 24),
        *body(190),
    ]
    assert not layout_credit(lines, 600, 850)["author"]
    report = layout_credit(lines, 600, 850, repeated_headers={_words("Amina Rao")})
    assert report["author"] == "Amina Rao"


def test_letter_spacing_preserves_word_and_person_boundaries():
    assert (
        untrack("A M I N A  R A O  A N D  E L E N A  C H E N")
        == "AMINA RAO AND ELENA CHEN"
    )
    assert untrack("An ordinary title") == "An ordinary title"


def test_title_page_keeps_both_authors_before_editor_and_translator():
    lines = [
        line("Cities and Public Memory", 50, 24),
        line("Amina Rao and", 160, 20),
        line("Elena Chen", 190, 20),
        line("Edited by Gabriel Santos", 250, 12),
        line("Translated by Nadia Okafor", 280, 12),
        line("Example University Press", 440, 11),
    ]
    assert publisher_credit(lines)["author"] == "Amina Rao, Elena Chen"


def test_editor_only_titlepage_does_not_synthesize_a_work_author():
    lines = [
        line("Cities and Public Memory", 50, 24),
        line("Edited by Gabriel Santos", 250, 12),
        line("Example University Press", 440, 11),
    ]
    assert not publisher_credit(lines)["author"]


def test_compact_publisher_titlepage_has_a_font_hierarchy_without_filename():
    lines = [
        line("Cities and Public Memory", 50, 40),
        line("Amina Rao", 300, 22),
        line("Example University Press", 600, 11),
    ]
    assert publisher_credit(lines)["author"] == "Amina Rao"
    lines[1]["confidence"] = 30
    assert not publisher_credit(lines)["author"]


def test_publisher_label_survives_ocr_word_boundary_noise():
    lines = [
        line("Cities and Public Memory", 50, 40),
        line("Amina Rao", 300, 22),
        line("Example Uni versity Pr ess", 600, 11),
    ]
    assert publisher_credit(lines)["author"] == "Amina Rao"


def test_edited_book_subtitle_cannot_be_promoted_as_two_creators():
    lines = [
        line("Historical Studies of Public Culture", 50, 40),
        line("Urban Memory and Social Change", 110, 22),
        line("Edited by Amina Rao", 300, 18),
        line("Example University Press", 600, 11),
    ]
    assert not publisher_credit(lines)["author"]


def test_et_al_header_is_not_a_complete_author_list():
    lines = [
        line("RAO ET AL.", 45),
        line("Cities and Public Memory", 90, 24),
        line("A History of Social Change", 118, 24),
        *body(190),
    ]
    assert not layout_credit(lines, 600, 850, repeated_headers={_words("RAO ET AL.")})[
        "author"
    ]


def test_running_work_title_above_prose_is_not_a_person_byline():
    lines = [line("Public Memory", 45, 8), *body(90), *body(130), *body(170)]
    assert not layout_credit(
        lines, 600, 850, repeated_headers={_words("Public Memory")}
    )["author"]


def test_figure_caption_cannot_supply_a_work_author():
    lines = [
        line("Figure 1 Public Space in Cities", 100, 24),
        line("Amina Rao", 150),
        *body(190),
    ]
    assert not layout_credit(lines, 600, 850)["author"]


def test_tsv_quotes_are_literal_text_instead_of_csv_quote_delimiters():
    payload = (
        b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        b'5\t1\t1\t1\t1\t1\t10\t20\t40\t12\t98\t"\n'
        b"5\t1\t1\t1\t2\t1\t10\t40\t40\t12\t98\tAmina\n"
    )
    text, lines = read_tsv(payload)
    assert text == '"\nAmina'
    assert len(lines) == 2


def test_one_ocr_tsv_pass_supplies_plain_text_and_positioned_lines():
    payload = (
        b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        b"5\t1\t1\t1\t1\t1\t10\t20\t40\t12\t98\tAmina\n"
        b"5\t1\t1\t1\t1\t2\t55\t20\t30\t12\t96\tRao\n"
    )
    text, lines = read_tsv(payload)
    assert text == "Amina Rao"
    assert lines[0]["box"] == [10, 20, 85, 32]
    assert lines[0]["confidence"] == 97
    assert read_tsv(b"plain test output") == ("plain test output", [])


@pytest.mark.parametrize(
    "ocr_text,expected",
    [("BY AMINA RAO", "AMINA RAO"), ("BY ELENA CHEN", ""), ("AMINA RAO", "")],
)
def test_collapsed_byline_ocr_restores_spaces_but_cannot_change_letters(
    ocr_text, expected
):
    doc = fitz.open()
    page = doc.new_page()
    credit = line("BYAMINARAO", 100)
    completed = subprocess.CompletedProcess([], 0, stdout=ocr_text.encode(), stderr=b"")
    with patch(
        "author_metadata.portable_layout.subprocess.run", return_value=completed
    ) as run:
        assert ", ".join(_collapsed_byline(page, credit)) == expected
        assert run.call_args.kwargs["timeout"] == 2
    doc.close()


def test_layout_correction_cannot_remove_a_valid_coauthor(monkeypatch):
    import author_metadata.dispatcher as dispatcher

    sample = AuthorEvidenceContext.from_samples(
        [], profile=DocumentProfile("generic", (), ())
    )
    monkeypatch.setattr(
        dispatcher,
        "_infer_author",
        lambda _: {
            "author": "Amina Rao, Elena Chen",
            "source": "text_structured_opening_credit",
        },
    )
    monkeypatch.setattr(
        dispatcher,
        "infer_portable_layout",
        lambda _: {
            "author": "Amina Rao",
            "source": "text_structured_opening_credit",
            "evidence": "Cities and Public Memory / Amina Rao",
        },
    )
    assert dispatcher.infer_author(sample)["author"] == "Amina Rao, Elena Chen"


def test_native_layout_ignores_neutral_filename_and_false_title(tmp_path):
    source = tmp_path / "document-0042.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 300), "Cities and Public Memory", fontsize=24)
    page.insert_text((370, 350), "Amina Rao", fontsize=12)
    page.insert_textbox(
        fitz.Rect(60, 390, 470, 700),
        "This study explains the social history of public space and urban culture. "
        * 10,
        fontsize=12,
    )
    doc.save(source)
    doc.close()
    sample = AuthorEvidenceContext.from_samples(
        [],
        path=source,
        title_hint="Unrelated Export Packet",
        profile=DocumentProfile("generic", (), ()),
    )
    with patch("author_metadata.portable_layout.subprocess.run") as run:
        assert infer(sample)["author"] == "Amina Rao"
    run.assert_not_called()


def test_failed_collapsed_credit_crops_are_bounded_per_document(tmp_path):
    source = tmp_path / "many-credits.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 80), "Cities and Public Memory", fontsize=24)
    page.insert_text((60, 130), "BYAMINARAO", fontsize=12)
    page.insert_text((60, 155), "BYELENACHEN", fontsize=12)
    doc.save(source)
    doc.close()
    sample = AuthorEvidenceContext.from_samples(
        [], path=source, profile=DocumentProfile("generic", (), ())
    )
    failed = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
    with patch(
        "author_metadata.portable_layout.subprocess.run", return_value=failed
    ) as run:
        assert not infer(sample)["author"]
        assert run.call_count == 1


def test_collapsed_credit_rendering_has_a_pixel_cap():
    class Pixels:
        def tobytes(self, extension):
            return b"pixels"

    class Page:
        rect = fitz.Rect(0, 0, 5000, 5000)

        def get_pixmap(self, *, matrix, clip):
            assert clip.width * clip.height * matrix.a * matrix.d <= 1_000_001
            return Pixels()

    credit = {"text": "BYAMINARAO", "box": [0, 0, 4000, 4000]}
    completed = subprocess.CompletedProcess([], 0, stdout=b"BY AMINA RAO", stderr=b"")
    with patch(
        "author_metadata.portable_layout.subprocess.run", return_value=completed
    ):
        assert _collapsed_byline(Page(), credit) == ["AMINA RAO"]
