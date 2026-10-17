"""Planning follows publishing structure, never an accepted author."""

import fitz
import pytest
from author_metadata.evidence_collection import _plan

pytestmark = pytest.mark.offline_deterministic


def document(texts):
    doc = fitz.open()
    for text in texts:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(40, 40, 550, 800), text, fontsize=10)
    return doc


def prose():
    return (
        "This argument examines cultural changes and historical records. It discusses public life and collective memory. "
        * 22
    )


def test_copyright_in_article_does_not_extend_body_to_twelve():
    with document(
        ["Article Title\nCopyright 2025 Example Press\nAbstract\n" + prose()]
        + [prose()] * 13
    ) as doc:
        pages, _ = _plan(doc)
        assert pages == [1, 2, 3, 4, 5, 13, 14]


def test_parent_title_and_rights_do_not_extend_past_local_chapter_start():
    with document(
        [
            "Parent Book\nExample University Press",
            "Copyright 2025\nAll rights reserved\nISBN 1234",
            "CHAPTER 5\nLocal Contribution\nAmelia Hart\n" + prose(),
            prose(),
            prose(),
        ]
        + [prose()] * 9
    ) as doc:
        pages, _ = _plan(doc)
        assert pages == [1, 2, 3, 4, 5, 13, 14]


def test_closing_copyright_cannot_open_a_frontmatter_sequence():
    with document([prose()] * 14) as doc:
        pages, _ = _plan(
            doc,
            ocr_pages=[
                {
                    "page": 14,
                    "text": "Copyright 2025 Example Press. All rights reserved. ISBN 1234",
                }
            ],
        )
        assert pages == [1, 2, 3, 4, 5, 13, 14]


def test_first_five_image_contents_still_extend_regardless_of_name():
    with document([""] * 14) as doc:
        pages, _ = _plan(doc, ocr_pages=[{"page": 1, "text": "Contents\nAmelia Hart"}])
        assert pages == list(range(1, 15))


def test_foreword_continuation_is_not_mistaken_for_main_body():
    with document(
        ["Foreword\nAmelia Hart", prose(), prose(), prose(), prose()] + [prose()] * 9
    ) as doc:
        pages, _ = _plan(doc)
        assert pages == list(range(1, 15))
