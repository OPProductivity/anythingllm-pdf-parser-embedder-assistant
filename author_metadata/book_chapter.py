"""Downloaded book contributions: local title/byline versus parent-book credits."""

import re
import unicodedata

from .names import (
    looks_like_person_name,
    normalize_author_candidate,
    split_author_line_candidates,
)
from .title_evidence import not_found, words

_NUMBER = r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_NUMBER_ONLY = re.compile(rf"^{_NUMBER}[.]?$", re.I)
_HEADING = re.compile(rf"^(?:chapter\s+)?{_NUMBER}[.\s]+(.+)$", re.I)
_CHAPTER = re.compile(rf"^chapter(?:\s+{_NUMBER})?$", re.I)
_ROLE = re.compile(
    r"\b(?:edited by|editors?|translat(?:ed|ors?)|interview(?:ees?| with)|participants?|contributors?|foreword|preface|acknowledg(?:e)?ments?|references|bibliography)\b",
    re.I,
)
_FURNITURE = re.compile(
    r"^(?:doi|https?://|downloaded|ebsco|all rights|copyright|published|online isbn|print isbn|subject:|series:|collection:|keywords?:|abstract|search in|\u00a9)",
    re.I,
)
_JOURNAL = re.compile(
    r"\bISSN\b|\bjournal homepage\b|\bTo cite this article\b|\bVol\.?\s*\d+.*\b(?:Issue|No\.)\b",
    re.I,
)


def _lines(text):
    return [
        " ".join(unicodedata.normalize("NFKC", line).replace("\u00ad", "").split())
        for line in text.splitlines()
        if line.strip()
    ]


def _opening(samples):
    # A part divider or blank scan may precede the first contribution. A book
    # cover is not such a divider and must not expose its later chapters here.
    for sample in samples or ():
        if not 1 <= int(sample.get("page") or 0) <= 3:
            continue
        lines = _lines(str(sample.get("text") or ""))
        yield sample, lines
        if lines and not (
            len(" ".join(lines).split()) < 15
            and re.match(r"^Part\s+[IVX\d]+$", lines[0], re.I)
        ):
            break


def _names(line):
    # A trailing hyphen is an unfinished token. Internal surname hyphens
    # remain valid; only a complete joined credit may become a person.
    if re.search(r"\w[-–]\s*$", line):
        return []
    if (
        _ROLE.search(line)
        or _FURNITURE.match(line)
        or any(mark in line for mark in (":", "?", "!", "(", ")"))
    ):
        return []
    raw = line.lstrip("'\u2018\u2019").strip()
    if raw.islower():
        raw = " ".join(part.capitalize() for part in raw.split())
    names = split_author_line_candidates(raw, require_complete=True)
    return names or ([raw] if looks_like_person_name(raw) else [])


def _title(lines):
    value = " ".join(lines).strip(" :")
    return (
        value
        if 2 <= len(words(value)) <= 28
        and not any(_ROLE.search(line) or _FURNITURE.match(line) for line in lines)
        and not re.search(r"[.!?]\s+[A-Z]", value)
        else ""
    )


def _numbered_credits(lines):
    for index, line in enumerate(lines[:80]):
        heading = _HEADING.fullmatch(line)
        number_only = _NUMBER_ONLY.fullmatch(line) or _CHAPTER.fullmatch(line)
        if not heading and not number_only:
            continue
        # Export order can place the chapter number between title and byline.
        if (
            number_only
            and line.isalpha()
            and not _CHAPTER.fullmatch(line)
            and index
            and index + 1 < len(lines)
        ):
            names = _names(lines[index + 1])
            for width in range(1, min(index, 4) + 1):
                title = _title(lines[index - width : index])
                if names and title and not _NUMBER_ONLY.fullmatch(lines[index - 1]):
                    yield names, title, index + 1
        # Standard numbered heading, title continuation, then chapter byline.
        for author_index in range(index + 1, min(index + 6, len(lines))):
            names = _names(lines[author_index])
            # A title continuation may have person-like capitalization. The
            # chapter byline is the last compact name line before body text.
            if author_index + 1 < len(lines) and _names(lines[author_index + 1]):
                continue
            title_lines = ([heading.group(1)] if heading else []) + lines[
                index + 1 : author_index
            ]
            title = _title(title_lines)
            if (
                names
                and title
                and not any(_NUMBER_ONLY.fullmatch(value) for value in title_lines)
            ):
                yield names, title, author_index
        # Some exports emit a complete title/byline before the chapter number.
        if (
            number_only
            and index >= 2
            and index + 1 < len(lines)
            and re.match(r"^\d{4}[.]?\s+\S", lines[index + 1])
        ):
            names = _names(lines[index - 1])
            title = _title(lines[index - 2 : index - 1])
            if names and title:
                yield names, title, index - 1


def _local_heading_credit_reports(context):
    """A chapter's own title followed immediately by its complete byline."""
    hint_words = words(context.title_hint)
    if len(hint_words) < 5:
        return
    for sample in context.opening_pages:
        if int(sample.get("page") or 0) != 1:
            continue
        lines = _lines(str(sample.get("text") or ""))[:18]
        for start in range(min(10, len(lines))):
            for width in range(1, min(4, len(lines) - start)):
                title = " ".join(lines[start : start + width])
                title_words = words(title)
                if len(title_words) < 4 or not any(
                    title_words[pos : pos + 4] == hint_words[offset : offset + 4]
                    for pos in range(len(title_words) - 3)
                    for offset in range(len(hint_words) - 3)
                ):
                    continue
                for credit_width in range(1, min(4, len(lines) - start - width) + 1):
                    credit_lines = lines[start + width : start + width + credit_width]
                    if any(
                        _FURNITURE.match(line) or _ROLE.search(line)
                        for line in credit_lines
                    ):
                        break
                    raw = " ".join(credit_lines)
                    if re.search(r"\((?:ed\.?|editor|editors)\)", raw, re.I):
                        continue
                    if credit_lines[-1].rstrip().endswith((",", "&", "and")):
                        continue
                    names = split_author_line_candidates(raw, require_complete=True)
                    if not names:
                        name = normalize_author_candidate(raw)
                        names = (
                            [name]
                            if looks_like_person_name(name, title_hint=title)
                            else []
                        )
                    if names and all(
                        looks_like_person_name(name, title_hint=title) for name in names
                    ):
                        yield {
                            "author": ", ".join(names),
                            "source": "text_strict_credit_block",
                            "page": 1,
                            "evidence": title + " / " + raw,
                        }
    return


def _local_heading_credit(context):
    reports = list(_local_heading_credit_reports(context))
    return reports[0] if reports else not_found()


def chapter_cues(samples):
    for sample, lines in _opening(samples):
        text = "\n".join(lines)
        if _JOURNAL.search(text[:1800]):
            continue
        chapter_export = bool(
            re.search(
                r"/books/(?:edited-volume/)?chapter-pdf/|/edited-volume/\d+/chapter/|\b10\.\d{4,9}/\S*978\d{10}(?:[-_]\d+|\.ch\d+)\b",
                text,
                re.I,
            )
        )
        book_export = (
            "ebook collection" in text.casefold()
            or "portion of the ebook" in text.casefold()
        )
        explicit_chapter = any(_CHAPTER.fullmatch(line) for line in lines[:14])
        credits = list(_numbered_credits(lines))
        if chapter_export or (
            explicit_chapter and (book_export or credits or "isbn" in text.casefold())
        ):
            return ("chapter_export_or_label",)
        opener = any(
            (_NUMBER_ONLY.fullmatch(line) or _HEADING.fullmatch(line))
            and all(
                _NUMBER_ONLY.fullmatch(previous)
                or re.match(r"^(?:Template:|Date:|Dir:)", previous)
                for previous in lines[:index]
            )
            for index, line in enumerate(lines[:6])
        )
        if credits and (book_export or opener):
            return ("numbered_chapter_title_byline",)
    return ()


def _running_head_is_catalog_title(context, name):
    """A chapter catalog's later book-title segment is not an author label."""
    if context.path is None:
        return False
    parts = re.split(r"\s+[-–—]{1,2}\s+", context.path.stem)
    if len(parts) < 4 or not any(
        re.fullmatch(r"(?:ch(?:apter)?\.?|chapitre|hoofdstuk|kapitel|cap[ií]tulo)\s*\d+", part, re.I)
        for part in parts[1:3]
    ):
        return False
    candidate = words(name)
    if not candidate or candidate[-1] in words(parts[0]):
        return False
    return any(words(part) == candidate for part in parts[2:])


def infer_reports(context):
    if context.profile.kind != "book_chapter":
        return
    yield from _local_heading_credit_reports(context)
    if "chapter_hinted_running_head" in context.profile.cues:

        def name_key(value):
            return "".join(
                char
                for char in unicodedata.normalize("NFKD", value).casefold()
                if char.isalnum() and not unicodedata.combining(char)
            )

        hint = name_key(context.title_hint)
        for sample in context.opening_pages:
            if int(sample.get("page") or 0) <= 1:
                continue
            for line in str(sample.get("text") or "").splitlines()[:5]:
                match = re.fullmatch(r"\s*\d{2,4}\s+(.{5,90})\s*", line)
                name = normalize_author_candidate(match.group(1)) if match else ""
                surname = name_key(name.split()[-1]) if name else ""
                if (surname and surname in hint and looks_like_person_name(name)
                        and not _running_head_is_catalog_title(context, name)):
                    yield {
                        "author": name,
                        "source": "text_strict_credit_block",
                        "page": int(sample["page"]),
                        "evidence": line.strip(),
                    }
    if "repository_chapter_label" in context.profile.cues:
        from .scholarly_article import _repository_cover_credit

        repository = _repository_cover_credit(context)
        if repository.get("author"):
            yield repository
    if "printed_book_excerpt" in context.profile.cues:
        for sample in context.opening_pages:
            if int(sample.get("page") or 0) != 1:
                continue
            text = str(sample.get("text") or "")
            if re.search(r"\bFirst (?:presented|published|distributed)\b", text, re.I):
                lines = _lines(text)
                for index, line in enumerate(lines[:15]):
                    if not line.startswith("[") or index < 2:
                        continue
                    names = _names(lines[index - 1])
                    if len(names) == 1:
                        yield {
                            "author": names[0],
                            "source": "text_strict_credit_block",
                            "page": 1,
                            "evidence": lines[index - 1] + " / " + line[:100],
                        }
            if "eBook Collection" in text or "EBSCOhost" in text:
                catalog = re.search(
                    r"(?im)^\s*AN:\s*\d+\s*;\s*([^;\n]{5,90})\s*;\s*[^;\n]{5,100}", text
                )
                if catalog:
                    name = normalize_author_candidate(catalog.group(1).strip(" ."))
                    if looks_like_person_name(name):
                        yield {
                            "author": name,
                            "source": "text_bibliographic_author_label",
                            "page": 1,
                            "evidence": catalog.group(0).strip(),
                        }
    for sample in context.opening_pages:
        if int(sample.get("page") or 0) != 1:
            continue
        text = str(sample.get("text") or "")
        if re.search(r"(?im)^\s*Chapter Title:\s*\S", text) and re.search(
            r"(?im)^\s*Book Title:\s*\S", text
        ):
            credit = re.search(r"(?im)^\s*Book Author(?:\(s\))?:\s*([^\n]+)", text)
        elif re.search(r"(?im)^\s*TYPE:\s*Book Chapter\s*$", text):
            credit = re.search(
                r"(?im)^\s*(?:CHAPTER AUTHOR|BOOK AUTHOR):\s*([^\n]+)", text
            )
        elif re.search(r"(?im)^\s*(?:Sage Reference|The SAGE Handbook)\b", text):
            credit = re.search(r"(?im)^\s*Author:\s*([^\n]+)", text)
        else:
            credit = None
        if credit:
            name = normalize_author_candidate(credit.group(1))
            if looks_like_person_name(name, allow_all_caps=True):
                yield {
                    "author": name,
                    "source": "text_author_label",
                    "page": 1,
                    "evidence": credit.group(0).strip(),
                }
    if context.path is not None and any(
        "ProQuest Ebook Central" in str(sample.get("text") or "")
        for sample in context.opening_pages
    ):
        from .legacy_fallback import infer_author_from_samples_or_filename

        cited = infer_author_from_samples_or_filename(
            context.opening_pages,
            context.path,
            title_hint=context.title_hint,
        )
        if cited.get("source") == "text_bibliographic_author_label":
            yield cited
    for sample, lines in _opening(context.samples):
        # Book-platform chapter covers can extract their title after the
        # abstract keyword furniture and parent-volume editorial board.
        if any(
            _CHAPTER.fullmatch(line) for line in lines[:14]
        ) and "/chapter/" in "\n".join(lines):
            for index in range(1, min(30, len(lines) - 1)):
                names = _names(lines[index])
                if not names or not re.match(
                    r"^This (?:essay|chapter)\b", lines[index + 1]
                ):
                    continue
                for width in range(1, min(4, index) + 1):
                    title = _title(lines[index - width : index])
                    if title and all(
                        looks_like_person_name(name, title_hint=title) for name in names
                    ):
                        yield {
                            "author": ", ".join(names),
                            "source": "text_strict_credit_block",
                            "page": int(sample["page"]),
                            "evidence": title + " / " + lines[index],
                        }
        for names, title, index in _numbered_credits(lines):
            if any(_ROLE.search(line) for line in lines[max(0, index - 6) : index]):
                continue
            if not all(
                looks_like_person_name(name, title_hint=title) for name in names
            ):
                continue
            yield {
                "author": ", ".join(names),
                "source": "text_strict_credit_block",
                "page": int(sample["page"]),
                "evidence": title + " / " + lines[index],
            }
    if context.path is not None:
        from .legacy_fallback import infer_author_from_samples_or_filename

        corroborated = infer_author_from_samples_or_filename(
            context.opening_pages,
            context.path,
            title_hint=context.title_hint,
        )
        if (
            corroborated.get("author")
            and corroborated.get("source") == "filename_corroborated_text_name"
        ):
            yield corroborated
        if (
            corroborated.get("author")
            and corroborated.get("source") == "filename_leading_name"
        ):
            parts = re.split(r"\s+[-–—]\s+", context.title_hint, maxsplit=1)
            if len(parts) == 2 and words(parts[0]) == words(corroborated["author"]):
                title = words(parts[1])
                if len(title) >= 3 and any(
                    any(
                        opening[start : start + len(title)] == title
                        for start in range(len(opening) - len(title) + 1)
                    )
                    for sample in context.opening_pages
                    if int(sample.get("page") or 0) == 1
                    for opening in [
                        words(" ".join(str(sample.get("text") or "").splitlines()[:4]))
                    ]
                ):
                    yield corroborated
    return


def infer(context):
    reports = list(infer_reports(context))
    if reports:
        return reports[0]
    return {
        "author": "",
        "source": "chapter_roles_not_resolved",
        "page": 0,
        "evidence": "No complete local chapter title/byline; parent-book credits are not chapter authors.",
    }
