from author_metadata.dispatcher import infer_author_from_samples
from pathlib import Path
import tempfile

import pytest
import fitz

import auto_anythingllm_pipeline as pipeline
from author_metadata.profile import classify_document
from author_metadata.pdf import (
    infer_author_from_initial_pdf_pages, selected_extraction_author_samples,
    recover_author_from_selected_extraction,
)


pytestmark = pytest.mark.offline_deterministic


def test_review_with_reviewed_book_is_not_routed_as_book():
    samples = [{"page": 1, "text": (
        "Book Review\nA Study of Shared Histories\nRicky Mullins, Example University\n"
        "Brooke Mullins, Example University\nBook Reviewed: Another Book, by J. D. Vance\n"
        "ISBN 9781234567890\nCopyright 2020"
    )}]
    assert classify_document(samples, "A Study of Shared Histories").kind == "review"


def test_report_prepared_by_is_separate_from_prepared_for():
    samples = [{"page": 1, "text": (
        "Technical Report No. 14\nAn Evaluation of Public Libraries\n"
        "Prepared by\nAlice Smith\nPrepared for\nJohn Jones\nExecutive Summary"
    )}]
    assert classify_document(samples, "An Evaluation of Public Libraries").kind == "report"
    result = infer_author_from_samples(
        samples, Path("report.pdf"), title_hint="An Evaluation of Public Libraries",
    )
    assert result["author"] == "Alice Smith"
    assert result["source"] == "text_report_prepared_by"


def test_degree_submission_routes_title_page_author_not_committee():
    samples = [{"page": 1, "text": (
        "Studies in Public Memory\nAlice Smith\nDepartment of History\n"
        "Example University\nThesis Committee:\nBob Jones (Chair)\n"
        "Submitted in partial fulfillment of the requirements\n"
        "for the degree of Doctor of Philosophy."
    )}]
    assert classify_document(samples).kind == "thesis_dissertation"
    result = infer_author_from_samples(samples, Path("dissertation.pdf"))
    assert result["author"] == "Alice Smith"
    assert result["source"] == "text_thesis_titlepage_author"
    selected = recover_author_from_selected_extraction(samples)
    assert selected["author"] == "Alice Smith"


def test_dissertation_title_page_without_byline_credits_name_before_degree_label():
    text = ("The Experiences of Students at a University\nMary Ann Begley\n"
            "A Dissertation\nSubmitted to the Graduate College in partial fulfillment "
            "of the requirements for the degree of DOCTOR OF PHILOSOPHY\n"
            "Committee:\nDr. Ellen Broido, Advisor")
    samples = [{"page": 1, "text": text}]
    assert classify_document(samples).kind == "thesis_dissertation"
    assert infer_author_from_samples(samples, Path("dissertation.pdf"))["author"] == "Mary Ann Begley"


def test_dissertation_byline_excludes_committee_and_dean():
    text = ("A Dissertation\nentitled\nA Study of Degree Awards\nby\n"
            "Rosalinda C. Dunlap\nSubmitted to the Graduate Faculty as partial "
            "fulfillment of the requirements for the Doctor of Philosophy Degree\n"
            "Dr. Penny Gosetti, Committee Chair\nDr. Patricia Komuniecki, Dean\n"
            "College of Graduate Studies")
    samples = [{"page": 1, "text": text}]
    assert classify_document(samples).kind == "thesis_dissertation"
    assert infer_author_from_samples(samples, Path("dissertation.pdf"))["author"] == "Rosalinda C. Dunlap"


def test_editorial_ui_preview_samples_signed_last_page_only_for_editorials():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "editorial.pdf"
        doc = fitz.open()
        for index in range(9):
            page = doc.new_page()
            text = ("Editorial\nReflections on a scholarly field\nDOI: 10.1000/example"
                    if index == 0 else
                    "Suzanne Oboler\nFounding Editor\nJohn Jay College of Criminal Justice"
                    if index == 8 else "Editorial prose continues.")
            page.insert_text((72, 72), text)
        doc.save(path)
        doc.close()
        report = infer_author_from_initial_pdf_pages(path, page_limit=4)
    assert report["author"] == "Suzanne Oboler"
    assert report["page"] == 9


def test_thesis_mismatched_title_page_people_abstains():
    samples = [{"page": 1, "text": (
        "Studies in Public Memory\nAlice Smith\nDepartment of History\n"
        "Bruno Santos\nDepartment of Sociology\n"
        "This thesis is submitted for the degree of Doctor of Philosophy."
    )}]
    result = infer_author_from_samples(samples, Path("by-Alice-Smith.pdf"))
    assert result["author"] == ""
    assert result["source"] == "thesis_author_not_resolved"


def test_degree_words_in_long_article_do_not_select_thesis_route():
    text = "Abstract\n" + "A study of thesis submissions and degrees. " * 80
    text += "\nThis thesis is submitted for the degree of Doctor of Philosophy."
    assert classify_document([{"page": 1, "text": text}]).kind != "thesis_dissertation"


def test_book_frontmatter_allows_later_matching_title_page():
    samples = [
        {"page": 1, "text": "The Hidden Politics of Public Memory"},
        {"page": 2, "text": (
            "The Hidden Politics of Public Memory\nJane Author\nOxford University Press"
        )},
        {"page": 3, "text": "Copyright 2024\nISBN 9781234567890\nContents"},
    ]
    title = "The Hidden Politics of Public Memory -- Jane Author"
    assert classify_document(samples, title).kind == "book"
    result = pipeline.infer_author_from_text_samples(samples, title_hint=title)
    assert result["author"] == "Jane Author"
    assert result["source"] == "text_titlepage_publisher_byline"
    assert result["page"] == 2


def test_book_title_fragment_and_series_editor_do_not_become_author():
    samples = [
        {"page": 1, "text": "The Hidden Politics of Public Memory"},
        {"page": 2, "text": (
            "The Hidden Politics of Public Memory\nSeries Editors\nJane Author\nOxford University Press"
        )},
        {"page": 3, "text": "Copyright 2024\nISBN 9781234567890\nContents"},
    ]
    result = pipeline.infer_author_from_text_samples(samples, title_hint="The Hidden Politics of Public Memory")
    assert result["author"] != "Jane Author"


def test_ambiguous_keyword_title_does_not_select_special_route():
    samples = [{"page": 1, "text": "Annual Report Analysis\nAbstract\nAlice Smith\nThe study examines reports."}]
    assert classify_document(samples, "Annual Report Analysis").kind == "scholarly_article"


def test_web_article_layout_is_distinct_from_report_and_book():
    samples = [{"page": 1, "text": (
        "Duncan Green\nMarch 3rd, 2025\nWhy should you be interested in a new blog on activism?\n"
        "A discussion of public work."
    )}]
    assert classify_document(samples, "Why should you be interested in a new blog on activism?").kind == "web_article"


def test_profile_never_claims_complete_document_scope_from_sampled_pages():
    profile = classify_document([{"page": 1, "text": "Book Review\nBook Reviewed: Example"}])
    assert profile.sampled_pages == (1,)
    assert not profile.scope_complete


def test_browser_print_is_not_scholarly_from_body_abstract_or_reference_doi():
    samples = [{"page": 1, "text": (
        "Here is what we are working on\nMozilla\nAbstract images are available.\n"
        "https://example.org/article | 1/3"
    )}, {"page": 3, "text": "References\nDOI: 10.1000/example"}]
    profile = classify_document(samples, "Here is what we are working on")
    assert profile.kind == "web_article"
    assert profile.publication_type == "browser_print_article"


def test_whole_issue_suppresses_title_adjacent_person_guess():
    samples = [{"page": 1, "text": (
        "THE EXAMPLE WEEKLY\nVOL. I.\nMONDAY, JUNE 11, 1900.\nNo. 1 7.\n"
        "A Person's Name Appears in a Story\nAlice Jones"
    )}]
    result = infer_author_from_samples(samples, Path("example-weekly.pdf"))
    assert result["source"] == "whole_issue_no_single_author"
    assert not result["author"]


def test_numbered_chapter_roles_do_not_become_book_editor_credit():
    samples = [{"page": 1, "text": "An Edited Book\nEdited by Example Editor"},
               {"page": 3, "text": "5. Preparing for the field\nCoordinated by Adam Boyette\nContributors: Dorsa Amir"}]
    result = infer_author_from_samples(samples, Path("chapter.pdf"))
    assert result["source"] == "chapter_roles_not_resolved"
    assert not result["author"]


@pytest.mark.parametrize("opening,expected", [
    ("CHAPTER 2\nTRIANGULATING JAPANESE\nFILM STYLE\nBen Singer\n"
     "It would be ludicrous to define cinema from one sequence.", "Ben Singer"),
    ("DOI: 10.4324/9781003273141-12\n9\nLATINX LIFE WRITERS\n"
     "OF SOUTH AMERICAN ORIGIN\nOR HERITAGE\nCynthia Martínez\n"
     "Though writers remain underrepresented, their work matters.", "Cynthia Martínez"),
])
def test_book_chapter_title_continuations_are_not_authors(opening, expected):
    result = infer_author_from_samples([{"page": 1, "text": opening}], Path("chapter.pdf"))
    assert classify_document([{"page": 1, "text": opening}]).kind == "book_chapter"
    assert result["author"] == expected


def test_book_chapter_platform_credit_is_not_parent_book_editor():
    text = ("A Handbook of Society\nJane Editor\nEditorial Board\n"
            "Online ISBN: 9781003174288\nCHAPTER\nAbstract\n"
            "A Study of Cultural Memory\nAlice Smith\nThis essay examines the relevant history.\n"
            "https://academic.oup.com/edited-volume/123/chapter/456")
    result = infer_author_from_samples([{"page": 1, "text": text}], Path("chapter.pdf"))
    assert classify_document([{"page": 1, "text": text}]).kind == "book_chapter"
    assert result["author"] == "Alice Smith"


def test_numbered_scholarly_section_is_not_book_chapter():
    text = ("Journal homepage: journal.example\nLiterature and Cultural Memory\n"
            "Astrid Erll\nAbstract\n1. The Power of Fiction\n"
            "Cultural memory is based on communication through media.")
    assert classify_document([{"page": 1, "text": text}]).kind != "book_chapter"


def test_parent_book_cover_is_not_reclassified_from_later_chapter():
    samples = [
        {"page": 1, "text": "Parent Book Title\nEdited by Jane Editor\nOxford University Press"},
        {"page": 2, "text": "1 A Study of Cultural Memory\nAlice Smith\nThis chapter explains its subject."},
    ]
    assert classify_document(samples, "Parent Book Title").kind != "book_chapter"


def test_pipeline_exports_the_canonical_pdf_author_helpers():
    assert pipeline.infer_author_from_initial_pdf_pages is infer_author_from_initial_pdf_pages
    assert pipeline.selected_extraction_author_samples is selected_extraction_author_samples


def test_selected_extraction_records_the_document_profile():
    result = recover_author_from_selected_extraction([{
        'page': 1,
        'text': 'Research on Social Inequality\nAlice Martin\nExample University\nAbstract\nResearch follows.',
    }], title_hint='Research on Social Inequality')
    assert result['document_profile']['kind'] == 'scholarly_article'
    assert result['document_profile']['classification_pages'] == [1]
    assert result['document_profile']['sampled_pages'] == [1]
    assert result['document_profile']['scope_complete'] is False


@pytest.mark.parametrize("citation_credit,citation_title,expected", [
    ("Sara Riva", "Tracing Invisibility as a Colonial Project: Indigenous Women Who Seek Asylum at the U.S.-Mexico Border", "Sara Riva"),
    ("Another Person", "Tracing Invisibility as a Colonial Project: Indigenous Women Who Seek Asylum at the U.S.-Mexico Border", ""),
    ("Sara Riva", "Tracing Invisibility as a Colonial Project: Indigenous Women Who Seek Asylum at the U.S.-Mexico Borderlands", ""),
])
def test_truncated_scholarly_pdf_title_needs_matching_publisher_citation(
    citation_credit, citation_title, expected,
):
    visible = "Tracing Invisibility as a Colonial Project: Indigenous Women Who Seek Asylum at the U.S.-Mexico Border"
    title = visible[:-2]
    sample = {"page": 1, "text": (
        "Journal of Immigrant & Refugee Studies\n"
        "Tracing Invisibility as a Colonial Project: Indigenous\n"
        "Women Who Seek Asylum at the U.S.-Mexico\n"
        "Border\nSara Riva\n"
        f"To cite this article: {citation_credit} (2021): {citation_title}\n"
        "DOI: 10.1000/example"
    )}
    result = infer_author_from_samples([sample], Path("article.pdf"), title_hint=title)
    assert result["author"] == expected


def test_book_stacked_authors_are_one_credit_not_conflicting_suffixes():
    samples = [{"page": 1, "text": (
        "Research on Social Inequality\nAlice Martin\nBruno Santos\nUniversity Press"
    )}]
    result = infer_author_from_samples(
        samples, Path("book.pdf"), title_hint="Research on Social Inequality",
    )
    assert result["author"] == "Alice Martin, Bruno Santos"
    assert result["source"] == "text_titlepage_publisher_byline"


def test_book_conflicting_title_pages_still_abstain():
    samples = [
        {"page": 1, "text": "Research on Social Inequality\nBy\nAlice Martin\nUniversity Press"},
        {"page": 3, "text": "Research on Social Inequality\nBy\nBruno Santos\nUniversity Press"},
    ]
    result = infer_author_from_samples(
        samples, Path("book.pdf"), title_hint="Research on Social Inequality",
    )
    assert result["author"] == ""
    assert result["source"] == "conflicting_work_credits"


def test_book_conflict_cannot_be_overridden_by_filename_byline():
    samples = [
        {"page": 1, "text": "Shared Histories\nBy\nAlice Smith\nUniversity Press"},
        {"page": 3, "text": "Shared Histories\nBy\nBob Jones\nUniversity Press"},
    ]
    result = infer_author_from_samples(
        samples, Path("Shared-Histories-by-Alice-Smith.pdf"), title_hint="Shared Histories",
    )
    assert result["author"] == ""
    assert result["source"] == "conflicting_work_credits"
    assert "page 1: Alice Smith" in result["evidence"]
    assert "page 3: Bob Jones" in result["evidence"]


def test_book_missing_credit_still_allows_explicit_filename_byline():
    samples = [{"page": 1, "text": "Praise for Shared Histories\nUniversity Press"}]
    result = infer_author_from_samples(
        samples, Path("Shared-Histories-by-Alice-Smith.pdf"), title_hint="Shared Histories",
    )
    assert result["author"] == "Alice Smith"
    assert result["source"] == "filename_explicit_byline"


def test_selected_extraction_cannot_override_conflicting_book_credits():
    pages = [
        {"page": 1, "text": "Shared Histories\nBy\nAlice Smith\nUniversity Press"},
        {"page": 3, "text": "Shared Histories\nBy\nBob Jones\nUniversity Press"},
    ]
    result = recover_author_from_selected_extraction(pages, title_hint="Shared Histories")
    assert result["author"] == ""
    assert result["source"] == "selected_extraction_conflicting_work_credits"


def test_selected_ocr_titlepage_inline_byline():
    pages = [
        {"page": 1, "text": "RAPHAEL DALLEO AND ELENA MACHADO SAEZ"},
        {"page": 4, "text": "THE LATINO/A CANON AND THE EMERGENCE OF POST-SIXTIES LITERATURE By Raphael Dalleo and Elena Machado Sáez"},
    ]
    result = recover_author_from_selected_extraction(pages, title_hint="The Latino a Canon")
    assert result["author"] == "Raphael Dalleo, Elena Machado Sáez"
    assert result["source"] == "selected_extraction_text_byline"


def test_selected_ocr_catalog_card_requires_matching_name_twice():
    card = ("Library of Congress Cataloging-in-Publication Data García, Cindy, 1968-"
            "Salsa crossings : dancing latinidad in Los Angeles / Cindy García.")
    pages = [{"page": index, "text": "front matter"} for index in range(1, 5)]
    pages.append({"page": 5, "text": card})
    result = recover_author_from_selected_extraction(pages, title_hint="SALSAC~1")
    assert result["author"] == "Cindy García"
    assert result["sample_pages"] == [1, 2, 3, 4, 5]
    pages[-1]["text"] = card.replace("/ Cindy García", "/ Another Person")
    assert recover_author_from_selected_extraction(pages, title_hint="SALSAC~1")["author"] == ""


def test_dos_short_filename_book_uses_explicit_editor_titlepage():
    samples = [{"page": 3, "text": (
        "Precarity and Belonging\nLabor, Migration, and Noncitizenship\n"
        "EDITED BY CATHERINE S. RAMÍREZ, SYLVANNA M. FALCÓN,\n"
        "JUAN POBLETE, STEVEN C. MCKAY, AND\nFELICITY AMAYA SCHAEFFER\n"
        "Rutgers University Press") }]
    result = infer_author_from_samples(samples, Path("PRECAR~1.PDF"), title_hint="PRECAR~1")
    assert result["author"] == ("CATHERINE S. RAMÍREZ, SYLVANNA M. FALCÓN, "
                                "JUAN POBLETE, STEVEN C. MCKAY, FELICITY AMAYA SCHAEFFER")


def test_microfilm_thesis_ocr_letter_spacing_in_author():
    sample = {"page": 3, "text": (
        "PO L IT IC S O F A Z T L A N\nby\nIgnacio M olina G arcia\n"
        "A Dissertation Submitted to the Faculty o f the\n"
        "In Partial Fulfillm ent o f the Requirem ents\n"
        "F or the D egree o f\nD O C T O R O F P H IL O S O P H Y")}
    result = infer_author_from_samples([sample], Path("dissertation.pdf"), title_hint="Politics of Aztlan")
    assert result["author"] == "Ignacio Molina Garcia"
    assert result["source"] == "text_thesis_titlepage_author"


def test_ebsco_catalog_wrapper_does_not_override_book_editor_titlepage():
    samples = [
        {"page": 1, "text": (
            "1996. National Academies Press.\n"
            "EBSCO Publishing: eBook Collection (EBSCOhost) printed on 2/16/2026\n"
            "123655; Lott, Juanita Tamayo, Edmonston, Barry, Goldstein, Joshua, "
            "National Research Council (U.S.); Spotlight on Heterogeneity")},
        {"page": 2, "text": (
            "Spotlight on\nHeterogeneity\nThe Federal Standards for Racial and Ethnic\n"
            "Classification\nSummary of a Workshop\n"
            "Barry Edmonston, Joshua Goldstein, Juanita Tamayo Lott, Editors\n"
            "National Academies Press")},
    ]
    result = infer_author_from_samples(samples, Path("LottJuanitaTamayoEdm_1996_SpotlightonHeterogeneity.pdf"))
    assert result["author"] == "Barry Edmonston, Joshua Goldstein, Juanita Tamayo Lott"


def test_university_of_texas_press_titlepage_is_book_author_evidence():
    samples = [
        {"page": 2, "text": "Mexican Americans and the Question of Race"},
        {"page": 4, "text": (
            "Mexican Americans and\nthe Question of Race\nJulie A. Dowling\n"
            "University of Texas Press   Austin")},
    ]
    title = "Mexican Americans and the Question of Race -- Julie A Dowling, (1975- ) -- University of Texas Press"
    result = infer_author_from_samples(samples, Path("catalog.pdf"), title_hint=title)
    assert result["author"] == "Julie A. Dowling"
    assert result["page"] == 4


def test_single_page_proquest_excerpt_citation_requires_matching_work_title():
    sample = {"page": 1, "text": (
        "Part III\nSeven Years After\n"
        "Galeano, Eduardo. Open Veins of Latin America : Five Centuries of the Pillage of a Continent, "
        "Monthly Review Press, 1997. ProQuest Ebook Central,\n"
        "Copyright © 1997. Monthly Review Press. All rights reserved.\n"
        "Ebook pages 284-284 | Printed page 1 of 1")}
    path = Path("Open_Veins_of_Latin_America_Five_Centuries_of_the_...pdf")
    assert infer_author_from_samples([sample], path, title_hint=path.stem)["author"] == "Eduardo Galeano"
    assert infer_author_from_samples([sample], path, title_hint="Another Work Entirely")["author"] == ""
