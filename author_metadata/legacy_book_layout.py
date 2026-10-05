"""Book front-matter title-page evidence, separate from endorsements and series roles."""

import re

from rag_pdf_tools import normalize_text


def title_page_matches_book(lines, title_hint):
    """Require a later title page to repeat the selected work's title words."""
    core_title = normalize_text(str(title_hint or "").split(" -- ", 1)[0])
    expected = [word.casefold() for word in re.findall(r"[^\W\d_]+", core_title)]
    expected = [word for word in expected if word not in {"a", "an", "and", "in", "of", "the", "to"}]
    if len(expected) < 3:
        return False
    page_words = {word.casefold() for word in re.findall(r"[^\W\d_]+", " ".join(lines[:16]))}
    return sum(word in page_words for word in expected) >= max(3, len(expected) - 1)


def candidate_is_book_title_fragment(candidate, title_hint):
    core_title = normalize_text(str(title_hint or "").split(" -- ", 1)[0])
    candidate_words = re.findall(r"[^\W\d_]+", normalize_text(candidate or "").casefold())
    title_words = re.findall(r"[^\W\d_]+", core_title.casefold())
    return len(candidate_words) >= 2 and " ".join(candidate_words) in " ".join(title_words)


def extract_publisher_backed_name(
    lines, title_hint, *, normalize_author_candidate, looks_like_person_name,
    looks_like_publisher_imprint_line, later_book_title_page=False,
):
    """Read a name immediately above an imprint on a qualified title page."""
    top_lines = list(lines[:18])
    core_title = str(title_hint or "").split(" -- ", 1)[0]
    for index, line in enumerate(top_lines[:12]):
        candidate = normalize_author_candidate(line)
        checked_title = core_title if later_book_title_page else title_hint
        if not looks_like_person_name(candidate, title_hint=checked_title, allow_all_caps=False):
            continue
        if later_book_title_page and candidate_is_book_title_fragment(candidate, core_title):
            continue
        if "," in line:
            continue
        following = top_lines[index + 1] if index + 1 < len(top_lines) else ""
        following_two = " ".join(top_lines[index + 1:index + 3])
        following_three = " ".join(top_lines[index + 1:index + 4])
        split_imprint_prefix = bool(re.fullmatch(r"[A-Z]{2,}(?:\s+[A-Z]{2,})?", following))
        if (
            looks_like_publisher_imprint_line(following)
            or (
                split_imprint_prefix
                and (
                    looks_like_publisher_imprint_line(following_two)
                    or looks_like_publisher_imprint_line(following_three)
                )
            )
        ):
            return candidate, f"{line} / publisher-imprint-nearby"
    return "", ""
