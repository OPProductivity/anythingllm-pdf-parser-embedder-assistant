"""Embedded fields propose identities; acquired publishing evidence adjudicates."""
from dataclasses import replace
from types import SimpleNamespace
import pytest

from author_metadata.context import AuthorEvidenceContext
from author_metadata.embedded_metadata import read
from author_metadata.embedded_credit_experts import assess
from author_metadata.publishing_relations import select

pytestmark = pytest.mark.offline_deterministic


def context(text, *, author="", title="", keywords="", xmp=None, pages=2):
    samples = tuple({"page": n, "text": text} for n in range(1, pages + 1))
    ctx = AuthorEvidenceContext.from_samples(samples, title_hint="document-0042")
    return replace(ctx, evidence_bundle={"trace": {"document_page_count": pages},
                   "embedded_metadata": {"info": {"author": author, "title": title,
                                                   "keywords": keywords}, "xmp": xmp or {}}})


def test_unverified_metadata_never_becomes_an_author():
    ctx = context("A paper about galaxies", author="Celia Rivers", title="A paper about galaxies")
    proposals, attempts, claims = assess(ctx, [ctx])
    assert proposals and attempts and not claims
    assert all(not p["eligible_for_selection"] for p in proposals)


def test_visible_byline_work_and_rights_expert_support_xmp_creator():
    text = "Book review: Ethics of the oceans\nCelia Rivers*\nRetired\n© 2025 Rivers\nIntroduction"
    ctx = context(text, xmp={"dc:creator": ["Celia Rivers"],
                            "dc:title": ["Book review: Ethics of the oceans"]})
    _, _, claims = assess(ctx, [ctx])
    assert claims[0]["author"] == "Celia Rivers"
    result, _ = select({}, claims, kind="review", work_scope="article")
    assert result["author"] == "Celia Rivers"


def test_editor_block_is_not_promoted_from_embedded_author():
    text = "Ethics of the oceans\nEDITED BY\nCelia Rivers\n© 2025 Rivers\nIntroduction"
    ctx = context(text, author="Celia Rivers", title="Ethics of the oceans")
    assert not assess(ctx, [ctx])[2]


def test_named_copyright_without_opening_byline_is_insufficient():
    ctx = context("Ethics of the oceans\n© 2025 Rivers\nSome unrelated prose", author="Celia Rivers", title="Ethics of the oceans")
    assert not assess(ctx, [ctx])[2]


def bibliography(surname="Rivers", given="Celia", title="A history of oceans"):
    return f"{surname},,{given}.,{title.replace(' ', ',')},,Harbor,Press,,2025.,ProQuest,Ebook,Central,,http://example.invalid"


def test_parent_credit_needs_actual_printed_work_bibliography():
    ctx = context("A history of oceans\nCelia Rivers discusses marine life", keywords=bibliography())
    assert not assess(ctx, [ctx])[2]


def test_repeated_printed_parent_work_credit_supports_export_proposal():
    ctx = context("Chapter 7\nRivers, Celia. A history of oceans. Harbor Press, 2025. ProQuest Ebook Central", keywords=bibliography())
    claims = assess(ctx, [ctx])[2]
    assert claims[0]["author"] == "Celia Rivers"
    assert claims[0]["priority"] < 110


@pytest.mark.parametrize("title", ["Oceans, rivers, and the world", "Modern marine life, 1950-1980"])
def test_internal_title_commas_do_not_end_the_parent_work_identity(title):
    raw = bibliography(title=title)
    text = "Rivers, Celia. " + title + ". Harbor Press, 2025. ProQuest Ebook Central"
    ctx = context(text, keywords=raw)
    assert assess(ctx, [ctx])[2][0]["author"] == "Celia Rivers"


def test_selected_foreword_does_not_inherit_an_unverified_parent_author():
    ctx = context("FOREWORD\nRivers, Celia. A history of oceans. Harbor Press, 2025. ProQuest Ebook Central", keywords=bibliography())
    assert not assess(ctx, [ctx])[2]


def test_local_chapter_author_outweighs_metadata_parent_credit():
    ctx = context("Rivers, Celia. A history of oceans. Harbor Press, 2025. ProQuest Ebook Central", keywords=bibliography())
    local = {"author": "Ada Keats", "role": "author", "work": "selected chapter", "page": 1,
             "family": "local_explicit_byline", "priority": 110, "source": "text_byline", "evidence": "By Ada Keats"}
    result, _ = select({}, [*assess(ctx, [ctx])[2], local], kind="book_chapter", work_scope="chapter")
    assert result["author"] == "Ada Keats"


def test_et_al_and_anthology_do_not_supply_complete_selected_author():
    for credit in [bibliography(given="Celia,,et,al"), bibliography(title="An anthology of oceans")]:
        ctx = context("Rivers, Celia. An anthology of oceans. ProQuest Ebook Central", keywords=credit)
        assert not assess(ctx, [ctx])[2]


def test_single_divider_is_explicitly_scoped_and_not_general_single_page_permission():
    text = "Part III\nSeven years later\nRivers, Celia. A history of oceans. Harbor Press, 2025. ProQuest Ebook Central"
    ctx = context(text, keywords=bibliography(), pages=1)
    assert assess(ctx, [ctx])[2][0]["minimal_divider"]
    ordinary = context(text.replace("Part III", "Research results"), keywords=bibliography(), pages=1)
    assert not assess(ordinary, [ordinary])[2]


def test_xmp_sequence_and_attribute_are_separate_and_not_authoritative():
    xml = '''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:pdf="http://ns.adobe.com/pdf/1.3/"><rdf:Description pdf:Author="Different Person"><dc:creator><rdf:Seq><rdf:li>Celia Rivers</rdf:li><rdf:li>Ada Keats</rdf:li></rdf:Seq></dc:creator></rdf:Description></rdf:RDF>'''
    result = read(SimpleNamespace(metadata={"author": "Local Account"}, get_xml_metadata=lambda: xml))
    assert result["xmp"]["dc:creator"] == ["Celia Rivers", "Ada Keats"]
    assert result["xmp"]["pdf:Author"] == ["Different Person"]
    assert "author" not in result


def test_malformed_or_entity_xml_retains_info_and_a_diagnostic():
    for xml in ["not XML", '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///sensitive">]><x>&e;</x>']:
        result = read(SimpleNamespace(metadata={"author": "Celia Rivers"}, get_xml_metadata=lambda: xml))
        assert result["info"]["author"] == "Celia Rivers"
        assert result["diagnostics"] and not result["xmp"]


def test_optional_xmp_provider_error_does_not_abort_document_evidence():
    class PDFFormatError(Exception):
        pass

    def unreadable():
        raise PDFFormatError("object is not a stream")

    result = read(SimpleNamespace(metadata={"author": "Celia Rivers"}, get_xml_metadata=unreadable))
    assert result["info"]["author"] == "Celia Rivers"
    assert result["diagnostics"] == [{"stage": "xmp_read", "error": "PDFFormatError"}]


def test_metadata_read_failure_remains_visible_in_collection_ledger(tmp_path, monkeypatch):
    import fitz
    from author_metadata.evidence_collection import collect

    path = tmp_path / "document-0042.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 90), "By Celia Rivers")
        page.insert_textbox(fitz.Rect(50, 130, 520, 730),
                            "This study examines ocean currents and the responsibility of research institutions. " * 30)
        doc.save(path)

    def unreadable(_self):
        raise ValueError("optional XMP object is damaged")

    monkeypatch.setattr(fitz.Document, "get_xml_metadata", unreadable)
    result = collect(AuthorEvidenceContext.from_samples([], path=path))
    trace = result.evidence_bundle["trace"]
    assert trace["complete"]
    assert trace["embedded_metadata"]["status"] == "partial"
    assert trace["embedded_metadata"]["diagnostics"][0]["stage"] == "xmp_read"
