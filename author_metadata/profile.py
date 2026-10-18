"""Conservative publication-category cues, independent of PDF producer metadata."""

import re
import unicodedata
from dataclasses import dataclass

from rag_pdf_tools import normalize_text

from .names import (has_author_affiliation_hint, looks_like_person_name,
                    looks_like_review_heading, normalize_author_candidate,
                    split_author_line_candidates)
from .book_chapter import chapter_cues
from .corporate import corporate_cue
from .muse_review import covers as muse_review_covers


@dataclass(frozen=True)
class DocumentProfile:
    kind: str
    cues: tuple[str, ...]
    sampled_pages: tuple[int, ...]
    scope_complete: bool = False
    publication_type: str = "unresolved"
    work_scope: str = "single_work"


def has_browser_print_footer(text):
    return bool(re.search(r"(?m)^https?://\S+\s*\|\s*1/\d+\s*$", text or ""))


def compact_scholarly_opening(text, title_hint):
    """Recognize a title/byline followed by article prose and scholarly evidence.

    This is deliberately a last-resort article cue. It never replaces an
    explicit book, chapter, review, issue, report, or web classification.
    """
    if len(text or "") < 1000:
        return False
    lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
    hint_words = {word for word in re.findall(r"[^\W\d_]+", title_hint.casefold(), re.UNICODE)
                  if len(word) > 2 and word not in {"and", "for", "the", "with"}}
    if len(hint_words) < 2:
        return False
    for index in range(1, min(5, len(lines))):
        line = lines[index]
        credit, separator, affiliation = line.partition(" / ")
        names = split_author_line_candidates(credit, title_hint="", allow_all_caps=False)
        if not names and separator:
            name = normalize_author_candidate(credit)
            if looks_like_person_name(name):
                names = [name]
        if not names:
            continue
        title = " ".join(lines[:index])
        title_words = re.findall(r"[^\W\d_]+", title.casefold(), re.UNICODE)
        if not 4 <= len(title_words) <= 28:
            continue
        if len(set(title_words) & hint_words) < 2:
            continue
        if len(" ".join(lines[index + 1:]).split()) < 120:
            continue
        affiliation_credit = bool(separator and has_author_affiliation_hint(affiliation))
        author_year_citation = bool(re.search(
            r"\([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ'’-]+(?:\s+et al\.)?"
            r"(?:\s+and\s+[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ'’-]+)?\s+(?:19|20)\d{2}\)",
            text[:2200],
        ))
        if affiliation_credit or author_year_citation:
            return True
    return False


def thesis_title_pages(samples):
    """Find short opening pages that explicitly identify a degree submission."""
    for sample in samples or []:
        try:
            page = int(sample.get("page") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if not 1 <= page <= 4:
            continue
        text = unicodedata.normalize("NFKC", str(sample.get("text") or ""))
        if len(text.split()) > 350:
            continue
        compact = " ".join(text.split())
        submission = re.search(
            r"\b(?:this\s+(?:thesis|dissertation)\s+is\s+submitted|"
            r"submitted\s+in\s+partial\s+fulfill?ment\s+of\s+the\s+requirements)\b",
            compact, flags=re.I,
        )
        degree = re.search(r"\b(?:Doctor|Master|Bachelor)\s+of\s+[A-Za-z]+\b", compact, flags=re.I)
        if not (submission and degree):
            # Microfilm OCR often separates letters in the degree heading.
            # Keep the damaged-text route tied to a short title page that
            # still states its dissertation submission explicitly.
            submission = re.search(r"\bDissertation\s+Submitted\s+to\s+the\s+Faculty\b", compact, re.I)
            degree = re.search(r"\bD\s+O\s+C\s+T\s+O\s+R\s+O\s+F\s+P\s+H", compact, re.I)
        if not (submission and degree):
            submission = (re.search(r"\bA\s+Dissertation\b", compact, re.I)
                          and re.search(r"\bSubmitted\s+to\b", compact, re.I)
                          and re.search(r"\bpartial\s+fulfill?ment\b", compact, re.I))
            degree = re.search(r"\bDoctor\s+of\s+Philosophy\b", compact, re.I)
        if not (submission and degree):
            submission = bool(re.search(r"\b(?:A\s+Thesis|A\s+Dissertation|Dissertation\s+Prepared\s+for\s+the\s+Degree)\b", compact, re.I))
            degree = bool(re.search(r"\b(?:Degree\s+of\s+Doctor\s+of\s+Philosophy|Doctor\s+of\s+Philosophy|Master\s+of\s+[A-Za-z]+)\b", compact, re.I))
            submission = submission and bool(re.search(r"\b(?:presented\s+to|submitted\s+to|prepared\s+for|partial\s+fulfill?ment)\b", compact, re.I))
        if not (submission and degree):
            submission = bool(re.search(r"\b(?:Honors\s+Theses|Honors\s+Research\s+Projects)\b", compact, re.I))
            degree = submission and bool(re.search(r"\b(?:Honors\s+College|University\s+of|The\s+University)\b", compact, re.I))
        if submission and degree:
            yield page, text


def _scholarly_footer_citation(text):
    """Keep the citation grammar without its whitespace-only backtracking.

    Either branch of the full expression requires one of these literal cores.
    This prerequisite changes no matches, pages, or publishing interpretations.
    """
    if not re.search(r"J Eng Educ\.|\d{4};\d{1,3}:\d+[-–]\d+", text, re.I):
        return False
    return bool(re.search(
        r"(?im)^\s*(?:J Eng Educ\.|[A-Za-z ]+\s+\d{4};\d{1,3}:\d+[-–]\d+)", text
    ))


def classify_document(samples, title_hint=""):
    """Route only strongly signalled layouts; generic remains the fallback."""
    def page_number(sample):
        try:
            return int(sample.get("page") or 0)
        except (TypeError, ValueError, AttributeError):
            return 0

    opening = [sample for sample in samples or [] if 1 <= page_number(sample) <= 4]
    pages = tuple(int(sample.get("page") or 0) for sample in opening)
    lines = [normalize_text(line) for sample in opening for line in
             str(sample.get("text") or "").splitlines()[:40] if normalize_text(line)]
    head = "\n".join(lines[:100])
    first_text = str(opening[0].get("text") or "") if opening else ""
    hint = normalize_text(title_hint or "")
    if any(thesis_title_pages(opening)):
        return DocumentProfile("thesis_dissertation", ("degree_submission_title_page",), pages,
                               publication_type="thesis_or_dissertation")
    # Export covers identify the selected contribution before a publisher's
    # ISBN, imprint or abstract can be mistaken for a whole book or article.
    first_head = first_text[:3500]
    chapter_cover = bool(re.search(r"(?im)^\s*Chapter Title:\s*\S", first_head)
                         and re.search(r"(?im)^\s*Book Title:\s*\S", first_head))
    loan_chapter = bool(re.search(r"(?im)^\s*TYPE:\s*Book Chapter\s*$", first_head))
    researchgate_chapter = bool(re.search(r"(?im)^\s*Chapter\s*[·•]\s*(?:January|February|March|April|May|June|July|August|September|October|November|December)", first_head))
    platform_chapter = bool(re.search(r"(?im)^\s*CHAPTER\s*$", first_head)
                            and re.search(r"\b(?:Online ISBN|Print ISBN|/chapter/|/edited-volume/)\b", first_head, re.I))
    handbook_entry = bool(re.search(r"(?im)^\s*(?:The SAGE Handbook|Sage Reference)\b", first_head)
                          and re.search(r"(?im)^\s*Author:\s*\S", first_head))
    anthology_excerpt = bool(re.search(r"(?im)^\s*[^\n]{3,90}:\s*An Anthology\b[^\n]*Edited by", first_head))
    chapter_heading = bool(re.search(r"(?im)^\s*(?:chapter\s+(?:one|two|three|four|five|six|\d+)|\d{1,2}\.\s+[^\n]{8,100})\b", first_head))
    excerpt_citation = bool(re.search(r"\b(?:Ebook pages|Printed page|ProQuest Ebook Central|EBSCO Publishing\s*:\s*eBook Collection)\b", first_head, re.I)
                            and re.search(r"\b(?:chapter|part\s+[ivx]+|introduction)\b", first_head[:900], re.I))
    chapter_with_book_citation = chapter_heading and bool(re.search(r"\b(?:ebook collection|proquest ebook central|book title:)\b", first_head, re.I))
    anthology_header = bool(re.search(r"(?im)^\s*Literary Theory:\s*An Anthology\b", first_head))
    chapter_hinted_excerpt = bool(re.search(r"\bchapter\b", hint, re.I) and any(
        re.search(r"(?m)^\s*\d{2,4}\s+[A-ZÀ-ÖØ-Þ][^\n]{5,90}$", str(s.get("text") or "")[:180])
        for s in opening[1:]
    ))
    reprinted_excerpt = bool(re.search(r"(?im)^\s*From\s+[A-Z][^\n]{8,140}", first_head)
                             and re.search(r"\bReprinted\s+by\s+permission\b", first_head, re.I))
    ebook_interior = bool(re.search(r"(?m)^\s*\d{2,4}\s*\n", first_head[:80])
                          and re.search(r"\beBook Collection\b|\bEBSCOhost\b", first_text, re.I))
    anthology_reprint = bool(re.search(r"(?im)^\s*\d{2,4}\s*[•·]\s*[A-Z\s()/-]{8,100}$", first_head[:200])
                               and re.search(r"\bFirst (?:presented|published|distributed)\b", first_head[:1000], re.I))
    opening_chapter = bool(re.search(r"\A\s*(?:\d{1,3}\s*\n)?\s*(?:chapter\s+(?:one|two|three|four|five|six|\d+)\s*(?:[—:-]|\n)|\d{1,2}\.\s+[^\n]{8,100})", first_head[:250], re.I))
    named_introduction = bool(re.search(r"(?im)^\s*i\s*n\s*t\s*r\s*o\s*d\s*u\s*c\s*t\s*i\s*o\s*n\s*$", first_head[:120])
                              and not re.search(r"\b(?:journal|abstract|doi|issn)\b", first_head[:1000], re.I))
    if (chapter_cover or loan_chapter or researchgate_chapter or platform_chapter or
            handbook_entry or anthology_excerpt or chapter_with_book_citation or excerpt_citation or anthology_header
            or opening_chapter or named_introduction or chapter_hinted_excerpt
            or reprinted_excerpt or ebook_interior or anthology_reprint):
        cue = next(name for value, name in ((chapter_cover, "chapter_and_book_title_cover"),
                   (loan_chapter, "interlibrary_loan_book_chapter"),
                   (researchgate_chapter, "repository_chapter_label"),
                   (platform_chapter, "platform_chapter_record"),
                   (handbook_entry, "handbook_entry_record"),
                   (anthology_excerpt or anthology_header, "edited_anthology_excerpt"),
                   (chapter_with_book_citation or excerpt_citation, "ebook_chapter_excerpt"),
                   (opening_chapter or named_introduction, "opening_chapter_heading"),
                   (chapter_hinted_excerpt, "chapter_hinted_running_head"),
                   (reprinted_excerpt or ebook_interior or anthology_reprint,
                    "printed_book_excerpt")) if value)
        return DocumentProfile("book_chapter", (cue,), pages,
                               publication_type="book_chapter", work_scope="chapter")
    corporate = corporate_cue(opening)
    if corporate:
        publication_type, cue = corporate
        return DocumentProfile("corporate_publication", (cue,), pages,
                               publication_type=publication_type)
    browser_print = has_browser_print_footer(first_text)
    browser_print = browser_print or bool(re.search(r"(?m)^Page 1 of \d+\s*$", first_text[:700])
                                    and re.search(r"(?m)^https?://\S+", first_text[:1000]))
    issue_masthead = bool(re.search(r"(?im)^\s*vol\.?\s*(?:[ivxlcdm]+|\d+)\.?\s*$", first_text[:1200]))
    issue_number = bool(re.search(r"(?im)^\s*no\.?\s*\d(?:[\s\d])*\.?\s*$", first_text[:1200]))
    issue_date = bool(re.search(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b", first_text[:1200], flags=re.I))
    if issue_masthead and issue_number and issue_date:
        return DocumentProfile("periodical_issue", ("masthead_volume_number_date",), pages,
                               publication_type="newspaper_or_periodical_issue", work_scope="whole_issue")
    if (re.search(r"(?im)^Vol\s+\d+,\s*No\s+\d+,\s*(?:Spring|Summer|Autumn|Fall|Winter)\s+\d{4}\s*\|\|", first_text[:500])
            and any(re.search(r"\bjournal\s+of\b", str(s.get("text") or "")[:400], re.I)
                    for s in opening[1:2])):
        return DocumentProfile("periodical_issue", ("journal_issue_cover_and_masthead",), pages,
                               publication_type="journal_issue", work_scope="whole_issue")
    if re.search(r"\bOFFICIAL REPORTS\s+OF\s+THE\s+[A-Z ]{3,45}COURT\b",
                 first_text[:1600], flags=re.I):
        return DocumentProfile("legal_opinion", ("official_court_report",), pages,
                               publication_type="court_opinion")
    review_heading = any(looks_like_review_heading(line) for line in lines[:24])
    explicit_review_credit = any(re.match(r"^reviewed\s+by\b", line, flags=re.I) for line in lines[:40])
    review_essay = bool(re.search(r"\breview\s+essay\b", hint, flags=re.I))
    cited_work = bool(re.search(r",\s*by\s+[A-Z]", head))
    review_journal = bool(re.search(r"(?m)^.{0,30}\breview\s+of\s+books\b", head[:500], flags=re.I))
    multi_work_review = review_journal and "to cite this article" in head.casefold() and ";" in head
    reviewed_role_context = explicit_review_credit and bool(re.search(
        r"\b(?:review\s+of|exhibition|documentary|film|book)\b", head[:1800], flags=re.I,
    ))
    muse_review = bool(next(muse_review_covers(opening), None))
    if review_heading or reviewed_role_context or (review_essay and cited_work) or multi_work_review or muse_review:
        cues = tuple(name for condition, name in (
            (review_heading, "review_heading"),
            (reviewed_role_context, "reviewed_by_role"),
            (review_essay and cited_work, "review_essay_citation"),
            (multi_work_review, "multiple_reviewed_works"),
            (muse_review, "muse_review_cover"),
        ) if condition)
        return DocumentProfile("review", cues, pages, publication_type="review")
    report_name = bool(re.search(r"\b(?:annual|technical|research|policy|evaluation)\s+report\b|\breport\s+(?:no\.?|number)\b|\bpolicy\s+brief\b", head + "\n" + hint, flags=re.I))
    report_roles = bool(re.search(r"(?m)^(?:prepared\s+(?:by|for)|submitted\s+to|executive\s+summary)\b", head, flags=re.I))
    report_release = "for release" in head.casefold() and "recommended citation" in head.casefold()
    institutional_report = bool(re.search(r"(?im)^report\s+to\s+(?:the\s+)?\S", first_text[:1800])) and bool(
        re.search(r"(?m)^\s*[A-Z]{2,8}[-/]\d{2,4}[-/]\d{3,8}\b", first_text[:1800]))
    policy_brief = bool(re.search(r"(?im)^policy\s+brief\s*$", first_text[:1200])) and bool(
        re.search(r"(?im)^(?:suggested\s+citation|recommended\s+citation|published\s+by)\b",
                  "\n".join(str(s.get("text") or "")[:1800] for s in opening[:2])))
    if (report_name and report_roles) or report_release or institutional_report or policy_brief:
        return DocumentProfile("report", ("institutional_report" if institutional_report else
                                          "policy_brief" if policy_brief else "report_publication",), pages,
                               publication_type="policy_brief" if policy_brief else "report")
    if (re.search(r"\bHarvard\s+Latino\s+Law\s+Review\b",
                  "\n".join(str(s.get("text") or "")[:700] for s in opening[:3]), re.I)
            or re.search(r"\b\d+\s+Harv\.\s+Latino\s+L\.\s+Rev\.\s+\d+\b",
                  "\n".join(str(s.get("text") or "")[:1600] for s in opening), re.I)
            or (re.search(r"(?im)^\s*ARTICLE\s*$", first_text[:300])
                and re.search(r"\b10\.1177/\S+", first_text[:200]))):
        return DocumentProfile("scholarly_article", ("journal_article_identifier",), pages,
                               publication_type="scholarly_article")
    chapter_roles = bool(re.search(r"(?im)^\s*(?:lead author|coordinated by|contributors)\s*:",
                                   "\n".join(str(s.get("text") or "")[:3000] for s in opening)))
    chapter_heading = bool(re.search(r"(?m)^\s*\d{1,2}\.\s+[^\n]{4,100}$",
                                     "\n".join(str(s.get("text") or "")[:3000] for s in opening)))
    if chapter_roles and chapter_heading:
        return DocumentProfile("book_chapter", ("numbered_chapter_role_credit",), pages,
                               publication_type="book_chapter", work_scope="chapter")
    contribution_cues = chapter_cues(opening)
    if contribution_cues:
        return DocumentProfile("book_chapter", contribution_cues, pages,
                               publication_type="book_chapter", work_scope="chapter")
    journal_cover = bool(re.search(r"(?im)^\s*Source:\s*[^\n]*(?:Journal|Review|Discourse|History|Studies)\b", first_head)
                         and re.search(r"(?im)^\s*Author\(s\):\s*\S", first_head))
    journal_citation = bool(re.search(r"(?im)^\s*To cite this article:\s*\S", first_head)
                            and re.search(r"\b(?:journal|review|studies|history|critique)\b", first_head, re.I))
    journal_header = bool(re.search(r"(?im)^\s*(?:Journal of [^\n]{4,90}|Geoforum\s+\d+|new left review\s+\d+|Feminist Media Histories,?\s+Vol\.|Social Forces\s+\d+)", first_head)
                          and re.search(r"\b(?:pp\.?\s*\d+|\d+\s*[-–]\s*\d+|Vol\.?\s*\d+|\d{4}\)\s*\d+|doi)\b", first_head, re.I))
    journal_footer = bool(re.search(r"(?im)^\s*(?:AJS\s+Volume\s+\d+|RADICAL HISTORY REVIEW\s+\d+|Constellations\s+Volume\s+\d+|d\s*iff\s*erences\s*:\s*A Journal of Feminist Cultural Studies)", first_text)
                          or (re.search(r"(?im)^\s*Journal of Political Ecology\s+Vol\.", str(opening[1].get("text") or "")) if len(opening)>1 else False))
    journal_article_label = bool(re.search(r"(?im)^\s*ARTICLE\s*$", first_head[:300])
                                 and re.search(r"\b(?:journal|research|review|studies|politics)\b", first_head[:500], re.I))
    law_review = bool(re.search(r"\b\d+\s+Harv\.\s+Latino\s+L\.\s+Rev\.\s+\d+\b", "\n".join(str(s.get("text") or "")[:1600] for s in opening), re.I))
    journal_article_label = journal_article_label or bool(
        re.search(r"(?im)^\s*ARTICLE\s*$", first_head[:300])
        and re.search(r"\b10\.1177/\S+", first_head[:200])
    )
    journal_citation_line = bool(re.search(r"(?im)^\s*[A-Z][^\n]{4,70}\s*,\s*Vol\.\s*\d+", first_head[:1200])
                                 and re.search(r"\b(?:pp\.|stable url|ISSN)\b", first_head[:1500], re.I))
    scholarly_footer = _scholarly_footer_citation(first_text)
    scholarly_footer = scholarly_footer or bool(re.search(r"\bA Journal of Feminist Cultural Studies\b", first_text, re.I))
    scholarly_footer = scholarly_footer or bool(re.search(r"(?im)^\s*new left review\s+\d+\s+(?:may|jan|mar|jul|sep|nov)", first_head[:250]))
    scholarly_footer = scholarly_footer or bool(re.search(r"(?im)^\s*(?:SPRING|SUMMER|FALL|WINTER)\s+\d{4}\s+contexts\b", "\n".join(str(s.get("text") or "")[:300] for s in opening), re.I))
    article_info = bool(re.search(r"(?im)^\s*a\s*r\s*t\s*i\s*c\s*l\s*e\s*i\s*n\s*f\s*o\s*$", first_head)
                        and re.search(r"(?im)^\s*a\s*b\s*s\s*t\s*r\s*a\s*c\s*t\s*$", first_head))
    if (journal_cover or journal_citation or journal_header or article_info or
            journal_footer or journal_article_label or journal_citation_line or scholarly_footer or law_review):
        return DocumentProfile("scholarly_article", ("journal_publication_record",), pages,
                               publication_type="scholarly_article")
    if browser_print:
        return DocumentProfile("web_article", ("browser_print_footer",), pages,
                               publication_type="browser_print_article")
    journal_furniture = bool(re.search(r"(?im)^TYPE\s+(?:REVIEW|EDITORIAL|OPINION|ORIGINAL RESEARCH|RESEARCH ARTICLE)\s*$", first_text)) and bool(
        re.search(r"(?im)^DOI\s+10\.\S+", first_text))
    if journal_furniture:
        return DocumentProfile("scholarly_article", ("journal_article_type_and_doi",), pages,
                               publication_type="scholarly_article")
    book_identifier = bool(re.search(r"\bISBN(?:-1[03])?\b", head, flags=re.I))
    book_frontmatter = bool(re.search(r"(?m)^(?:praise\s+for|title\s+page|copyright|contents)\b", head, flags=re.I))
    book_navigation = bool(re.search(r"(?m)^CONTENTS\s*\nCover\s*\nEndorsements\s*\nTitle Page\b", head, flags=re.I))
    book_praise = bool(re.search(r"(?m)^praise\s+for\s+.{4,100}$", head, flags=re.I))
    core_title = hint.split(" -- ", 1)[0]
    title_words = [word for word in re.findall(r"[^\W\d_]+", core_title.casefold())
                   if word not in {"a", "an", "and", "in", "of", "the", "to"}]
    imprint_title_page = False
    if len(title_words) >= 2:
        for sample in opening:
            text = str(sample.get("text") or "")
            if len(text.split()) > 160 or not re.search(
                r"\b(?:university(?:\s+of\s+\w+){0,3}\s+press|publishers?|publishing|routledge|palgrave|macmillan|penguin)\b",
                text, flags=re.I,
            ):
                continue
            visible_words = set(re.findall(r"[^\W\d_]+", text.casefold()))
            if sum(word in visible_words for word in title_words) >= max(2, len(title_words) - 1):
                imprint_title_page = True
                break
    series_frontmatter = "titles in the series" in head.casefold() and not bool(
        re.search(r"\b(?:doi|abstract)\b", head, flags=re.I)
    )
    def short_imprint_page(sample):
        text = str(sample.get("text") or "")
        letters = re.sub(r"[^a-z]", "", text.casefold())
        return (4 <= len(text.split()) <= 65
                and (re.search(r"\b(?:University\s+Press|Strelka\s+Press|Zero\s+Books|Routledge|Palgrave\s+Macmillan)\b", text, re.I)
                     or "universitypress" in letters)
                and not re.search(r"\b(?:Source:|Chapter Title:|Journal of|Vol\.|ISBN|DOI|Abstract)\b", text, re.I))
    compact_titlepage = any(short_imprint_page(sample) for sample in opening[:4])
    journal_issue = bool(re.search(r"\bVol\.?\s*\d+\s*,?\s*Issue\b", head[:500], flags=re.I))
    scholarly_cues = bool(re.search(r"\bdoi\s*:|\babstract\b", head, flags=re.I)) or journal_issue
    all_opening = "\n".join(str(s.get("text") or "")[:2200] for s in opening)
    book_imprint = bool(re.search(r"\b(?:University\s+Press|National\s+Academies\s+Press|Routledge|Palgrave\s+Macmillan|Zero\s+Books)\b", all_opening, re.I))
    book_copyright = bool(re.search(r"\b(?:Copyright|©|Library of Congress|ISBN)\b", all_opening, re.I))
    book_titlepage = book_imprint and book_copyright and not scholarly_cues
    editor_titlepage = any(
        3 <= len(str(sample.get("text") or "").split()) <= 90
        and re.search(r"(?im)^\s*Edited\s+by\s*$", str(sample.get("text") or ""))
        and not re.search(r"\b(?:journal|article|abstract|doi|review)\b", str(sample.get("text") or ""), re.I)
        for sample in opening[:4]
    )
    noisy_editor_titlepage = bool(re.search(r"\bHandbook\s+of\b", first_head[:300], re.I)
                                  and re.search(r"(?im)^\s*EDIT\s*ED\s*By\s*$", first_head[:700])
                                  and len(first_text.split()) < 100)
    translator_titlepage = any(
        len(str(sample.get("text") or "").split()) <= 80
        and re.search(r"(?im)^\s*Translated\s+by\s+", str(sample.get("text") or ""))
        and re.search(r"(?im)^\s*With\s+an\s+introduction\s+by\s+", str(sample.get("text") or ""))
        for sample in opening[:4]
    )
    full_book_blurb = bool(re.search(r"\bIn this (?:engaging|important|new) book\b", all_opening, re.I))
    by_titlepage = any(
        3 <= len(str(sample.get("text") or "").split()) <= 70
        and re.search(r"(?im)^\s*By\s+[A-Z][^\n]{5,130}", str(sample.get("text") or ""))
        and len(re.split(r"(?im)^\s*By\s+", str(sample.get("text") or ""), maxsplit=1)[0].split()) >= 5
        and any(3 <= len(str(prior.get("text") or "").split()) <= 15
                and not re.search(r"(?im)^\s*By\s+", str(prior.get("text") or ""))
                for prior in opening[:index])
        for index, sample in enumerate(opening[:4])
    )
    foreword_cover = bool(re.search(r"(?im)^\s*FOREWORD\s+BY\s+[A-Z]", first_head[:250])
                          and len(first_text.split()) < 50)
    if ((book_identifier and book_frontmatter) or book_navigation or book_praise
            or series_frontmatter or compact_titlepage or book_titlepage or editor_titlepage
            or noisy_editor_titlepage
            or translator_titlepage or full_book_blurb or by_titlepage or foreword_cover
            or (imprint_title_page and not scholarly_cues)):
        return DocumentProfile("book", ("book_frontmatter_or_titlepage",) +
                               (("foreword_cover",) if foreword_cover else ()), pages,
                               publication_type="book")
    dated_web_credit = bool(re.search(r"(?m)^[A-Z][^\n]{3,75}\n[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?,\s+\d{4}\n", head))
    pipe_dated_credit = bool(re.search(r"(?m)^[A-Z][^\n|]{3,75}\s*\|\s*[A-Z][A-Z]+\s+\d{1,2},\s*\d{4}$", head))
    web_masthead = sum(bool(re.search(rf"\b{term}\b", head, flags=re.I)) for term in
                       ("subscribe", "renew", "view author profile", "join the conversation")) >= 2
    web_byline = bool(re.search(r"(?m)^BY\s+[A-Z][A-Z .'-]{5,80}$", head))
    if dated_web_credit or pipe_dated_credit or (web_masthead and web_byline):
        return DocumentProfile("web_article", ("name_date_headline_layout" if
                               dated_web_credit or pipe_dated_credit else "masthead_and_byline",), pages,
                               publication_type="web_article")
    if scholarly_cues:
        return DocumentProfile("scholarly_article", ("journal_volume_issue" if journal_issue else "doi_or_abstract",), pages,
                               publication_type="scholarly_article")
    if first_text and compact_scholarly_opening(first_text, hint):
        return DocumentProfile("scholarly_article", ("compact_title_byline_and_scholarly_body",), pages,
                               publication_type="scholarly_article")
    return DocumentProfile("generic", (), pages)
