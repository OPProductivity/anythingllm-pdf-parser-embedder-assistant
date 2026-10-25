"""Small, bounded OCR recovery for absent or corrupt native opening evidence."""

import re
import shutil as shutil  # Historical patch points; acquisition uses the same modules.
import subprocess as subprocess

from .structured_credit import _names, _title_supported, _words
from .title_evidence import not_found
from .portable_layout import layout_credit_reports, publisher_credit_reports

_CACHE = {}


def _credit_reports(text, context, page):
    lines = [" ".join(v.split()) for v in text.splitlines() if v.strip()]
    opening = " ".join(lines[:35])
    if len(opening.split()) < 100 and re.search(
        r"\b(?:university press|institution press|publishers)\b", opening, re.I
    ):
        for index, line in enumerate(lines[:25]):
            if (
                not re.search(
                    r"\b(?:university press|institution press|publishers)\b", line, re.I
                )
                or index < 2
            ):
                continue
            names = _names(lines[index - 1])
            if not names:
                continue
            # A line-ending conjunction can join a wrapped two-author credit.
            if index >= 3 and re.search(r"\s+and\s*$", lines[index - 2], re.I):
                preceding = _names(
                    re.sub(r"\s+and\s*$", "", lines[index - 2], flags=re.I)
                )
                if preceding:
                    names = preceding + names
            filename_words = set(_words(context.path.stem))
            if all(_words(name)[-1] in filename_words for name in names):
                yield {
                    "author": ", ".join(names),
                    "source": "text_ocr_author_credit",
                    "page": page,
                    "evidence": "publisher title-page credit: "
                    + " / ".join(lines[max(0, index - 3) : index + 1]),
                }
    if re.search(r"\bin\s*conversation\b", opening, re.I) and re.search(
        r"\bwith\b", context.title_hint, re.I
    ):
        for line in lines[:30]:
            m = re.fullmatch(r"with\s+(.{5,80})", line, re.I)
            names = _names(m.group(1)) if m else []
            if len(names) == 1 and set(_words(names[0])).issubset(
                set(_words(context.path.stem))
            ):
                yield {
                    "author": names[0],
                    "source": "text_ocr_author_credit",
                    "page": page,
                    "evidence": "interviewer credit: " + line,
                    "role": "interviewer",
                }
    for index, line in enumerate(lines[:35]):
        if (
            re.fullmatch(r"By", line, re.I)
            and index + 1 < len(lines)
            and re.search(r"\b(?:thesis|dissertation)\b", opening, re.I)
            and re.search(r"\b(?:degree|requirements|graduate)\b", opening, re.I)
        ):
            raw = re.sub(
                r",\s*(?:B\s*\.?\s*S\s*\.?|M\s*\.?\s*A\s*\.?).*$",
                "",
                lines[index + 1],
                flags=re.I,
            )
            names = _names(raw)
            if names:
                yield {
                    "author": ", ".join(names),
                    "source": "text_ocr_author_credit",
                    "page": page,
                    "evidence": "thesis title-page By: " + raw,
                }
        names = _names(line.lstrip("-–— "))
        if not names or index == 0 or index + 1 >= len(lines):
            continue
        if _names(lines[index + 1]):
            # A wrapped heading can itself look like a person's name. Keep
            # the following name line available instead of consuming it as prose.
            continue
        for start in range(max(0, index - 8), index):
            visible = " ".join(lines[start:index])
            title_ok = _title_supported(visible, context.title_hint, names)
            # A complete chapter-heading suffix may follow an author/catalog
            # prefix in the selected filename; do not accept a mere final word.
            expected = tuple(w for w in _words(context.title_hint) if w != "chapter")
            seen = _words(visible)
            title_ok |= len(expected) >= 5 and seen[:4] == expected[-4:]
            explicit = re.match(r"^(?:by\s+|[-–—]\s*)", line, re.I)
            core = tuple(
                w
                for w in _words(context.title_hint)
                if w not in set(_words(" ".join(names)))
            )
            if explicit and 1 <= len(core) <= 3 and seen == core:
                title_ok = True
            local_heading = False
            if not title_ok and context.path is not None:
                filename_words = set(_words(context.path.stem))
                heading = lines[start:index]
                # A curatorial surname can corroborate a complete printed
                # credit on one scanned leaf, even if a scanner's technical
                # title prevents literal title matching. It cannot supply a
                # missing first name or replace the local heading/byline/body.
                local_heading = (
                    len(names) == 1
                    and _words(names[0])[-1] in filename_words
                    and 5 <= len(seen) <= 24
                    and len(heading) <= 4
                    and all(value.isupper() for value in heading)
                    and not re.search(r"\b(?:references|bibliography|contents|index|figure|table)\b", visible, re.I)
                )
                title_ok = local_heading
            if title_ok and len(" ".join(lines[index + 1 : index + 4]).split()) >= 12:
                yield {
                    "author": ", ".join(names),
                    "source": "text_ocr_author_credit",
                    "page": page,
                    "evidence": visible + " / " + line,
                    **({"mechanism": "printed_leaf_credit_with_curatorial_surname"} if local_heading else {}),
                }
    return


def _credit(text, context, page):
    """Compatibility API; all occurrences are evaluated before choosing one."""
    reports = list(_credit_reports(text, context, page))
    return reports[0] if reports else not_found()


def bundle_reports(context):
    """Interpret every region and every acquired reading, including alternatives."""
    from .native_article import credit_reports, alternating_credit_reports

    reports = []
    for primary in (context.evidence_bundle or {}).get("ocr", []):
        variants = [primary] + (
            [primary["alternate"]] if primary.get("alternate") else []
        )
        for index, region in enumerate(variants):
            lines = [
                {**line, "baseline": line.get("baseline", line["box"][3])}
                for line in region["lines"]
            ]
            alternatives = [
                *publisher_credit_reports(lines, page=region["page"]),
                *layout_credit_reports(
                    lines, region["width"], region["height"], page=region["page"]
                ),
                *credit_reports(lines, region["width"], region["height"]),
                *alternating_credit_reports(lines, region["width"], region["height"]),
                *_credit_reports(region["text"], context, region["page"]),
            ]
            if region.get("reason") == "collapsed_explicit_byline":
                value = " ".join(region["text"].split())
                names = _names(value) if re.match(r"^BY\s+", value, re.I) else []
                if (
                    names
                    and "".join(_words(", ".join(names)))
                    == str(region.get("native_letters", "")).casefold()
                ):
                    alternatives.append(
                        {
                            "author": ", ".join(names),
                            "page": region["page"],
                            "evidence": value,
                            "mechanism": "ocr_space_restoration_native_letters_verified",
                        }
                    )
            for report in alternatives:
                if report.get("author") and region.get("alternate"):
                    alternate = _credit(
                        region["alternate"]["text"], context, region["page"]
                    )
                    a, b = report["author"].split(), alternate.get("author", "").split()
                    if len(a) == len(b) and all(
                        x == y
                        or (
                            re.fullmatch(r"[A-Z]{1,3}\.", x)
                            and re.fullmatch(r"[A-Z]{1,3}\.", y)
                            and x[0] == y[0]
                        )
                        for x, y in zip(a, b)
                    ):
                        report["author"] = " ".join(
                            min((x, y), key=len) for x, y in zip(a, b)
                        )
                        report["ocr_initial_recheck"] = [a, b]
                if report.get("author"):
                    reports.append(
                        {
                            **report,
                            "source": "text_ocr_author_credit",
                            "page": region["page"],
                            "extraction": "tesseract",
                            "crop": region["clip"],
                            "reading_variant": index,
                            "geometry_coordinate_space": "crop_pixels",
                        }
                    )
    return reports


def infer(context):
    """Compatibility result from the same completed acquisition as arbitration.

    The collector inspects every scheduled region regardless of an early name.
    All reports remain available through bundle_reports; this entry point keeps
    the historical one-result API without a second OCR plan or cross-call cache.
    """
    from .evidence_collection import collect

    reports = bundle_reports(collect(context))
    return reports[0] if reports else not_found()
