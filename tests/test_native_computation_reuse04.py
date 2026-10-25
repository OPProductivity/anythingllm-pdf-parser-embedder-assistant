"""One extraction must preserve both native representations and typography."""

import copy
import fitz
import pytest
from author_metadata.field_map import native_page_atoms
from author_metadata.native_article import positioned_lines
from author_metadata.portable_layout import _native_page
from author_metadata.lexical_scope import lexical_scope
from author_metadata.title_evidence import words

pytestmark = pytest.mark.offline_deterministic


def test_one_textpage_keeps_word_references_and_fonts(monkeypatch):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 60), "A study of shared memory", fontsize=20)
    page.insert_text((50, 90), "Amelia Hart", fontsize=12)
    page.insert_text((310, 90), "Boris Vale", fontsize=12)
    flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
    before = page.get_text("words", sort=False)
    original = fitz.Page.get_textpage
    calls = []

    def tracked(self, *args, **kwargs):
        calls.append(kwargs.get("flags"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_textpage", tracked)
    atoms = native_page_atoms(page, 1)
    assert len(calls) == 1 and calls[0] == flags
    assert [a["text"] for a in atoms] == [w[4] for w in before]
    assert [a["source_block"] for a in atoms] == [w[5] for w in before]
    assert [a["source_word"] for a in atoms] == [w[7] for w in before]
    assert atoms[0]["font_size"] == 20 and atoms[-1]["font_size"] == 12
    doc.close()


def test_shared_dict_is_read_only_and_reproduces_independent_line_interpretations():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 60), "INDEPENDENT MEMORY", fontsize=18)
    page.insert_text((60, 90), "Amelia Hart", fontsize=12)
    data = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
    retained = copy.deepcopy(data)
    assert positioned_lines(page, native_dict=data) == positioned_lines(page)
    assert _native_page(page, native_dict=data) == _native_page(page)
    assert data == retained
    doc.close()


def test_title_token_reuse_preserves_unicode_and_inference_boundaries():
    value = "Écriture, MEMORY & film"
    with lexical_scope():
        assert words(value) == ("écriture", "memory", "film")
        assert words(value) is words(value)
    with lexical_scope():
        assert words(value) == ("écriture", "memory", "film")
