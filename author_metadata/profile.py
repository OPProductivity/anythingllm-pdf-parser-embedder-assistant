"""Conservative publication-category cues, independent of PDF producer metadata."""

import re
import unicodedata
from dataclasses import dataclass

from rag_pdf_tools import normalize_text

from .names import looks_like_review_heading
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
        if submission and degree:
            yield page, text


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
    corporate = corporate_cue(opening)
    if corporate:
        publication_type, cue = corporate
        return DocumentProfile("corporate_publication", (cue,), pages,
                               publication_type=publication_type)
    browser_print = has_browser_print_footer(first_text)
    issue_masthead = bool(re.search(r"(?im)^\s*vol\.?\s*(?:[ivxlcdm]+|\d+)\.?\s*$", first_text[:1200]))
    issue_number = bool(re.search(r"(?im)^\s*no\.?\s*\d(?:[\s\d])*\.?\s*$", first_text[:1200]))
    issue_date = bool(re.search(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b", first_text[:1200], flags=re.I))
    if issue_masthead and issue_number and issue_date:
        return DocumentProfile("periodical_issue", ("masthead_volume_number_date",), pages,
                               publication_type="newspaper_or_periodical_issue", work_scope="whole_issue")
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
    journal_issue = bool(re.search(r"\bVol\.?\s*\d+\s*,?\s*Issue\b", head[:500], flags=re.I))
    scholarly_cues = bool(re.search(r"\bdoi\s*:|\babstract\b", head, flags=re.I)) or journal_issue
    if (book_identifier and book_frontmatter) or book_navigation or book_praise or series_frontmatter or (imprint_title_page and not scholarly_cues):
        return DocumentProfile("book", ("book_frontmatter_or_titlepage",), pages,
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
    return DocumentProfile("generic", (), pages)
