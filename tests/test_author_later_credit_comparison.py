"""Later credit evidence and complete physical rosters remain inspectable."""

import pytest

from author_metadata.context import AuthorEvidenceContext
from author_metadata.native_article import _column_lines, credit_reports
from author_metadata.profile import DocumentProfile
from author_metadata.publishing_relations import local_credit_role_claims, select
from author_metadata.structured_credit import infer_reports
from author_metadata.text_responsibility import claims as span_claims
from author_metadata.contextual_roles import claims as role_claims

pytestmark = pytest.mark.offline_deterministic


def line(text, y, size=12, x=50, right=540):
    return {
        "text": text,
        "box": [x, y, right, y + size],
        "size": size,
        "baseline": y + size,
    }


def test_separate_affiliation_columns_are_separate_people_but_name_fragments_join():
    separate = _column_lines(
        [
            line("Amina Rao", 100, x=50, right=130),
            line("Gabriel Santos", 100, x=300, right=410),
        ],
        0,
        600,
    )
    assert separate[0]["text"] == "Amina Rao, Gabriel Santos"
    fragments = _column_lines(
        [
            line("Amina", 100, x=50, right=90),
            line("Rao", 100, x=95, right=120),
        ],
        0,
        600,
    )
    assert fragments[0]["text"] == "Amina Rao"


def test_numbered_affiliation_does_not_require_a_university_keyword():
    lines = [
        line("Uncertainty in Public Records", 70, 22),
        line("Amina Rao, Gabriel", 120, 12),
        line("Santos and Elena Chen", 136, 12),
        line("1 Westport Research, 2 Urban Systems", 170, 9),
        line("Summary", 200, 15),
        line(
            "This study examines the changing patterns of public records in several cities.",
            230,
            10,
        ),
    ]
    reports = list(credit_reports(lines, 600, 800))
    assert reports[0]["author"] == "Amina Rao, Gabriel Santos, Elena Chen"
    lines[3] = line("A sentence about a different source.", 170, 9)
    assert not list(credit_reports(lines, 600, 800))


def test_later_title_page_and_matching_copyright_are_not_hidden_by_praise():
    ctx = AuthorEvidenceContext.from_samples(
        [
            {
                "page": 1,
                "text": "Praise for this book\nAn important contribution.\nGabriel Santos",
            },
            {
                "page": 4,
                "text": "Uncertainty in Public Records\nAmina Rao\nCopyright 2024 Amina Rao",
            },
            {
                "page": 5,
                "text": "Uncertainty in Public Records\nElena Chen\nCopyright 2024 Elena Chen",
            },
        ],
        title_hint="Uncertainty in Public Records",
        profile=DocumentProfile("generic", (), (1, 4, 5)),
    )
    reports = list(infer_reports(ctx))
    assert [(r["page"], r["author"]) for r in reports] == [
        (4, "Amina Rao"),
        (5, "Elena Chen"),
    ]


def test_wrapper_translator_role_needs_a_separate_explicit_local_byline():
    wrapper = {
        "author": "Amina Rao, Gabriel Santos",
        "family": "labelled_responsibility_field",
        "role": "author",
        "work": "selected work",
        "page": 1,
        "priority": 107,
    }
    translator = {"author": "Gabriel Santos", "role": "translator"}
    local = {
        "expert": "structured_credit",
        "author": "Amina Rao",
        "page": 2,
        "evidence": "Uncertainty in Public Records / by Amina Rao",
    }
    assert not local_credit_role_claims([], [wrapper, translator])
    claims = local_credit_role_claims([local], [wrapper, translator])
    assert claims[0]["author"] == "Amina Rao"
    result, _ = select(
        {}, [wrapper, *claims], kind="scholarly_article", work_scope="single_work"
    )
    assert result["author"] == "Amina Rao"
    # Joint authorship and translation can coexist. The physical joint byline
    # prevents a translator role from silently removing that person.
    joint = {**local, "author": "Amina Rao, Gabriel Santos"}
    assert not local_credit_role_claims([joint], [wrapper, translator])


def test_wrapped_editor_credit_and_other_books_keep_distinct_work_boundaries():
    text = (
        "Another Collection of Public Records\nEdited by Gabriel Santos\n\n"
        "Uncertainty in Public Records\nEdited by Amina Rao\nand Elena Chen\n\n"
        "Westport University Press"
    )
    claims = span_claims(
        [{"page": 4, "text": text}],
        kind="book",
        work_scope="single_work",
        title_hint="Uncertainty in Public Records",
    )
    applicable = [c["author"] for c in claims if c["priority"] > 0]
    assert applicable == ["Amina Rao, Elena Chen"]


def test_neutral_hint_does_not_hide_local_byline_but_quotation_is_insufficient():
    text = (
        "Uncertainty in Public Records\nby Amina Rao\n\nIntroduction\nA new argument."
    )
    claims = span_claims(
        [{"page": 2, "text": text}],
        kind="generic",
        work_scope="single_work",
        title_hint="document-0042",
    )
    assert any(
        c["author"] == "Amina Rao" and c["family"] == "local_explicit_byline"
        for c in claims
    )
    quote = "A quoted narrative\nBy Gabriel Santos\nAnother sentence."
    assert not span_claims(
        [{"page": 2, "text": quote}],
        kind="generic",
        work_scope="single_work",
        title_hint="document-0042",
    )


@pytest.mark.parametrize(
    "credit,creator_type",
    [
        ("Amina Rao", "person"),
        ("Westport News Agency", "organization"),
    ],
)
def test_dated_news_byline_can_credit_a_person_or_an_agency(credit, creator_type):
    text = (
        "New evidence about public records in three cities\n"
        + credit
        + "\nWed 30 Oct 2024 17.59 CET\nA report about local history."
    )
    claims = role_claims(
        [{"page": 1, "text": text}], kind="web_article", work_scope="single_work"
    )
    assert claims[0]["author"] == credit
    assert claims[0]["creator_type"] == creator_type


def test_suggested_institutional_citation_requires_the_current_work_title():
    cover = {
        "page": 1,
        "text": "Protecting Public Records in Growing Cities\nPolicy Brief",
    }
    citation = {
        "page": 2,
        "text": "Suggested citation: Westport Research Council, ‘Protecting Public Records in Growing Cities’, 2024.",
    }
    claims = role_claims([cover, citation], kind="report", work_scope="single_work")
    assert any(c["author"] == "Westport Research Council" for c in claims)
    other = {"page": 1, "text": "A study about another topic"}
    # A cited title elsewhere in the current text alone must not bind the
    # institution to an unrelated cover.
    assert not role_claims([other, citation], kind="report", work_scope="single_work")


def test_expert_biographies_need_collective_responsibility_for_the_current_report():
    roster = {
        "page": 2,
        "text": (
            "Experts on this topic\nAmina Rao   Amina studies public records.\n"
            "Distinguished Engineer\na.rao@example.invalid\n"
            "Gabriel Santos   Gabriel studies the same topic.\n"
            "Research Fellow\ng.santos@example.invalid"
        ),
    }
    related = {"page": 3, "text": "We are two authors of an earlier book."}
    assert not role_claims([roster, related], kind="report", work_scope="single_work")
    current = {
        "page": 3,
        "text": "We are two authors of an earlier book. In this report, we present our new findings.",
    }
    claims = role_claims([roster, current], kind="report", work_scope="single_work")
    assert claims[0]["author"] == "Amina Rao, Gabriel Santos"


def test_departmental_guide_issuer_is_not_a_cited_manual_author():
    cover = {
        "page": 1,
        "text": "University of Westport\nDEPARTMENTAL\nWRITING GUIDE\nHISTORY STUDIES\nLAST UPDATED: 2024",
    }
    body = {
        "page": 2,
        "text": "Your instructor will grade courses using these guidelines. See the manual by Gabriel Santos.",
    }
    claims = role_claims([cover, body], kind="generic", work_scope="single_work")
    assert claims[0]["author"] == "University of Westport, HISTORY STUDIES"


def test_ocr_text_specialist_retains_later_competing_credit():
    from author_metadata.bounded_ocr import _credit_reports
    from author_metadata.context import AuthorEvidenceContext

    context = AuthorEvidenceContext.from_samples(
        [], title_hint="Collective Memory and Public Life"
    )
    text = (
        "Collective Memory and Public Life\nBy Amelia Hart\n"
        "This study examines how communities preserve and interpret historical records across generations and social settings.\n"
        "Collective Memory and Public Life\nBy Bernard Stone\n"
        "This study examines how communities preserve and interpret historical records across generations and social settings."
    )
    reports = list(_credit_reports(text, context, 1))
    assert {"Amelia Hart", "Bernard Stone"} <= {r["author"] for r in reports}


def test_chapter_specialist_retains_local_and_later_catalogue_proposals():
    from author_metadata.book_chapter import infer_reports
    from author_metadata.context import AuthorEvidenceContext
    from author_metadata.profile import DocumentProfile

    context = AuthorEvidenceContext.from_samples(
        [
            {
                "page": 1,
                "text": "Collective Memory in Historical Public Life\nAmelia Hart\nIntroduction\n"
                "EBSCOhost eBook Collection\nAN: 12345; Stone, Bernard; A related scholarly book",
            }
        ],
        title_hint="Collective Memory in Historical Public Life",
        profile=DocumentProfile(
            "book_chapter", ("printed_book_excerpt",), (1,), work_scope="chapter"
        ),
    )
    reports = list(infer_reports(context))
    assert {"Amelia Hart", "Bernard Stone"} <= {r["author"] for r in reports}
