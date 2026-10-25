"""Publisher role evidence remains subordinate to work and contributor scope."""
from pathlib import Path

import pytest

from author_metadata.book_chapter import _running_head_is_catalog_title
from author_metadata.bounded_ocr import _credit_reports
from author_metadata.context import AuthorEvidenceContext
from author_metadata.contextual_roles import claims
from author_metadata.publishing_relations import select, reconcile_report_foreword

pytestmark = pytest.mark.offline_deterministic


def test_interleaved_biographies_keep_each_credential_and_contact_with_its_person():
    samples = [{"page": 2, "text": "Experts on this topic\n"
        "Amelia Hart              Amelia leads a worldwide research team\n"
        "architecture and applied research are her responsibility\n"
        "Distinguished Engineer,\nInstitute for Research\nhttps://example.org/hart\n\n"
        "Boris Vale              Boris studies collective memory\n"
        "customers use the research in their daily work\n"
        "Research Fellow\nApplied Research\nand Teaching\nexample.org/vale\nvale@example.org\n"},
        {"page": 3, "text": "We are two authors of an earlier monograph. In this report, we reflect on our completed research."}]
    result = claims(samples, kind="book_chapter", work_scope="chapter")
    assert any(r["author"] == "Amelia Hart, Boris Vale" and r["work"] == "selected work" for r in result)
    samples[1]["text"] = "The two experts were interviewed about someone else's report."
    assert not any(r["family"] == "biographic_roster_and_current_report_voice"
                   for r in claims(samples, kind="book_chapter", work_scope="chapter"))


@pytest.mark.parametrize("voice", ["our platform", "our full-stack computing platform", "our growing international business"])
def test_company_profile_voice_allows_modifiers_without_promoting_a_compiler(voice):
    samples = [{"page": 1, "text": "Company Overview\nNovember 2026"},
               {"page": 3, "text": "ASTER\nASTER’s products serve many customers. We develop "
                + voice + ".\nHeadquarters: Utrecht | Headcount: 400"}]
    result = claims(samples, kind="corporate_publication", work_scope="whole_work")
    assert any(r["author"] == "ASTER" and r["creator_type"] == "organization" for r in result)
    samples[1]["text"] = "ASTER\nHeadquarters: Utrecht | Headcount: 400\nA customer uses its own products."
    assert not any(r["family"] == "corporate_profile_self_presentation" for r in claims(samples, kind="corporate_publication", work_scope="whole_work"))


def test_personal_masthead_and_first_person_voice_remain_context_not_authorship():
    sample = {"page": 1, "text": "Amelia Hart’s Weblog\nA lesson about research\n29th May 2026\nI think this explains the research problem."}
    result = claims([sample], kind="web_article", work_scope="article")
    assert result and all(r["work"] != "selected work" for r in result)
    old = {"author": "Boris Vale", "source": "text_byline"}
    assert select(old, result, kind="web_article", work_scope="article")[0] == old
    sample["text"] += "\nA guest contribution by Boris Vale."
    assert not any(r["work"] == "selected work" for r in claims([sample], kind="web_article", work_scope="article"))


def test_scanned_leaf_credit_needs_both_local_structure_and_curatorial_surname():
    text = "AN EXPANDED ACCOUNT OF SOCIAL MEMORY\nIN THE MODERN WORLD\nAMELIA HART\nThe present chapter examines a broad selection of publishing practices and explains how they developed."
    context = AuthorEvidenceContext.from_samples([], path=Path("Reading week 6 Hart.pdf"), title_hint="Scan_Generic_19-11-2026")
    result = list(_credit_reports(text, context, 1))
    assert any(r["author"].casefold() == "amelia hart" and r["mechanism"] == "printed_leaf_credit_with_curatorial_surname" for r in result)
    neutral = AuthorEvidenceContext.from_samples([], path=Path("document-3.pdf"), title_hint=context.title_hint)
    assert not list(_credit_reports(text, neutral, 1))
    references = text.replace("AN EXPANDED ACCOUNT OF SOCIAL MEMORY", "REFERENCES AND EXTERNAL RESEARCH SOURCES")
    assert not list(_credit_reports(references, context, 1))


def test_parent_title_segment_is_not_a_running_author_head():
    context = AuthorEvidenceContext.from_samples([], path=Path("Hart - Chapter 1 - Social Memory - Visible Narratives - Public Life.pdf"))
    assert _running_head_is_catalog_title(context, "Visible Narratives")
    assert not _running_head_is_catalog_title(context, "Amelia Hart")
    neutral = AuthorEvidenceContext.from_samples([], path=Path("document-1.pdf"))
    assert not _running_head_is_catalog_title(neutral, "Visible Narratives")


def test_foreword_scope_uses_cover_contents_and_independent_author_proof():
    actor = {"author": "Amelia Hart", "source": "text_structured_opening_credit",
             "mechanism": "physical_heading_byline_body", "page": 3,
             "evidence": "Foreword: Changes in modern industry / Amelia Hart"}
    bundle = {"trace": {"document_page_count": 40}, "native": [
        {"page": 1, "text": "Research Trends Report"},
        {"page": 2, "text": "Table of Contents\nForeword 3\nIntroduction 6\nChapter 1 11"}]}
    revised, audit = reconcile_report_foreword(actor, bundle, [actor], [])
    assert not revised["author"] and audit["work"] == "nested foreword"
    # A whole-work author may also write its foreword. A real independent
    # title-page credit settles that relationship instead of a label veto.
    proof = {"author": "Amelia Hart", "source": "text_author_label", "page": 1}
    assert reconcile_report_foreword(actor, bundle, [actor, proof], [])[0] == actor
    # A PDF whose selected work is itself a foreword keeps its author.
    bundle["native"][0]["text"] = "Foreword to the Research Trends Report"
    assert reconcile_report_foreword(actor, bundle, [actor], [])[0] == actor
