"""Bounded filename/PDF credit corroboration and reusable author grades."""

from pathlib import Path

import fitz
import pytest

from author_metadata.dispatcher import infer_author_from_samples
from author_metadata.context import AuthorEvidenceContext
from author_metadata.credit_match import match_filename_to_scholarly_byline
from author_metadata.profile import DocumentProfile
from author_metadata.identity import author_confidence, resolve_author_from_metadata_and_inference
from author_metadata.names import looks_like_person_name
from author_metadata.pdf import infer_author_from_initial_pdf_pages


pytestmark = pytest.mark.offline_deterministic


def test_repository_author_field_matches_filename_not_a_body_citation():
    samples = [
        {"page": 1, "text": "UC Davis\nTitle\nA study of rural memoirs\nAuthor\nPruitt, Lisa R\nPublication Date\n2018"},
        {"page": 2, "text": "Harkins and McCarroll, eds.\nJoe Bageant, Deer Hunting\nLisa R. Pruitt"},
    ]
    report = infer_author_from_samples(samples, Path("archive - Lisa R Pruitt.pdf"))
    assert report["author"] == "Lisa R Pruitt"
    assert report["source"] == "filename_pdf_credit_match"
    assert report["page"] == 1


def test_bare_filename_surname_can_corroborate_explicit_author_field():
    report = infer_author_from_samples(
        [{"page": 1, "text": "Title\nA new study\nAuthor\nJane Congdon\nPublication Date\n2020"}],
        Path("study of Appalachia - Congdon.pdf"),
    )
    assert report["author"] == "Jane Congdon"
    assert report["source"] == "filename_pdf_credit_match"


@pytest.mark.parametrize("text", [
    "Title\nA study\nHarkins was cited in the introduction\nPublication Date\n2020",
    "Title\nA study\nEditor\nJane Harkins\nPublication Date\n2020",
    "Title\nA study\nAuthor\nPublication Date\n2020",
])
def test_filename_match_does_not_promote_mentions_or_document_furniture(text):
    report = infer_author_from_samples([{"page": 1, "text": text}], Path("study - Harkins.pdf"))
    assert report.get("source") != "filename_pdf_credit_match"


def test_filename_credit_must_match_printed_author_and_abstain_on_conflict():
    report = infer_author_from_samples(
        [{"page": 1, "text": "Title\nA study\nAuthor\nJane Congdon\nAuthor\nJane Harkins"}],
        Path("study - Congdon Harkins.pdf"),
    )
    assert report.get("source") != "filename_pdf_credit_match"


def test_whole_issue_does_not_borrow_named_author_field():
    report = infer_author_from_samples(
        [{"page": 1, "text": "THE EXAMPLE WEEKLY\nVOL. I.\nJUNE 11, 1900.\nNo. 17.\nAuthor\nJane Congdon"}],
        Path("issue - Congdon.pdf"),
    )
    assert report.get("source") != "filename_pdf_credit_match"


def test_author_confidence_and_final_role_guard():
    accepted = resolve_author_from_metadata_and_inference(
        "The University", {"author": "Jane Congdon", "source": "filename_pdf_credit_match"})
    assert accepted == {"author": "Jane Congdon", "source": "filename_pdf_credit_match"}
    assert author_confidence(accepted) == {
        "score": 90, "level": "high", "basis": "filename_and_visible_author_field"}
    for furniture in ("Publication Date", "Critical Paper", "Working Paper", "The University",
                      "Duke University Press"):
        rejected = resolve_author_from_metadata_and_inference(
            "", {"author": furniture, "source": "text_author_label"})
        assert rejected == {"author": "", "source": "not_available"}
        assert author_confidence(rejected)["score"] == 0


def test_working_paper_heading_is_not_a_person_but_paper_can_be_a_surname():
    assert not looks_like_person_name("Working Paper")
    assert not looks_like_person_name("Research Paper")
    assert looks_like_person_name("Jane Paper")


def test_fast_pdf_preview_exposes_same_author_confidence(tmp_path):
    path = tmp_path / "article - Congdon.pdf"
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((50, 50), "Title\nA new study\nAuthor\nJane Congdon\nPublication Date\n2020")
        document.save(path)
    preview = infer_author_from_initial_pdf_pages(path, page_limit=4)
    assert preview["author"] == "Jane Congdon"
    assert preview["source"] == "filename_pdf_credit_match"
    assert preview["author_confidence"]["score"] == 90


@pytest.mark.parametrize(("filename", "byline", "following", "expected"), [
    ("lopez-caballero-2025-race.pdf", "Paula López Caballero\ue840", "CEIICH-UNAM\nAbstract", "Paula López Caballero"),
    ("Burke_Transforming power.pdf", "Brian J. Burke1", "University of Georgia\nIntroduction", "Brian J. Burke"),
    ("Nancy Fraser, Rethinking Recognition.pdf", "nancy fraser", "RETHINKING RECOGNITION\nIntroduction", "Nancy Fraser"),
])
def test_scholarly_standalone_byline_needs_filename_and_page_structure(
    filename, byline, following, expected,
):
    context = AuthorEvidenceContext.from_samples(
        [{"page": 1, "text": f"Journal of Culture\nA long academic article title\n{byline}\n{following}"}],
        path=Path(filename), title_hint="A long academic article title",
        profile=DocumentProfile("scholarly_article", (), (1,)),
    )
    report = match_filename_to_scholarly_byline(context)
    assert report is not None
    assert report["author"] == expected
    assert report["source"] == "filename_pdf_standalone_match"
    assert author_confidence({"author": expected, "source": report["source"]})["score"] == 82


def test_scholarly_body_mention_does_not_become_byline():
    report = infer_author_from_samples(
        [{"page": 1, "text": "Journal\nA long article title\nAbstract\nNancy Fraser argued for recognition.\nIntroduction"}],
        Path("Nancy Fraser, Rethinking Recognition.pdf"),
        title_hint="A long article title",
    )
    assert report.get("source") != "filename_pdf_standalone_match"


def test_scholarly_title_line_is_not_filename_matched_as_author():
    context = AuthorEvidenceContext.from_samples(
        [{"page": 1, "text": (
            "MODOS\nJournal of Art History\nISSN: 2526-2963\n"
            "Radio Espacio Estacion broadcast\nFordlândia: an ecocritical approach\n"
            "through site-oriented practices\nGabriela Paiva de Toledo\n"
            "Image credit: the artist"
        )}],
        path=Path("Radio_Espacio_Estacion_broadcast_Fordlandia.pdf"),
        profile=DocumentProfile("scholarly_article", (), (1,)),
    )
    assert match_filename_to_scholarly_byline(context) is None
