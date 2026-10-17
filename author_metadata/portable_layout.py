"""Work credits from physical layout, independent of export names and PDF titles."""

from collections import defaultdict
import csv
import io
import math
import re
import shutil
import subprocess

from .structured_credit import _names, _words
from .title_evidence import not_found

_CACHE = {}
_CROP_CACHE = {}
_ROLE = re.compile(
    r"\b(?:edited by|translated by|foreword by|reviewed by|series editor|interview with)\b",
    re.I,
)


def untrack(value):
    """Undo explicitly spaced capitals, preserving larger word gaps."""
    chunks = re.split(r"\s{2,}", value.strip())
    return " ".join(
        "".join(part.split())
        if re.fullmatch(r"(?:[^\W\d_]\s+){2,}[^\W\d_]", part)
        else part
        for part in chunks
    )


def _person(line):
    if re.search(r"\bet\.?\s+al\.?\b", line["text"], re.I):
        return []
    value = re.sub(
        r",\s*(?:Ph\.?D\.?|M\.?D\.?|M\.?A\.?|M\.?Sc\.?)(?=\s*[,;]|\s+and\b|$)",
        "",
        untrack(line["text"]),
        flags=re.I,
    )
    value = re.sub(r",\s*(?:and|&)\s+", " and ", value, flags=re.I)
    return _names(value) if not _ROLE.search(value) else []


def _report(names, heading, credit, page, basis):
    return {
        "author": ", ".join(names),
        "source": "text_structured_opening_credit",
        "page": page,
        "evidence": heading + " / " + untrack(credit["text"]),
        "mechanism": basis,
        "geometry": {"byline": credit["box"]},
    }


def _heading(
    lines,
    end,
    credit_size,
    *,
    allow_quotes=False,
    allow_embedded_quotes=False,
    allow_short=False,
):
    selected = []
    for line in reversed(lines[:end]):
        if not line["text"].strip():
            continue
        if line["size"] < credit_size * 1.2 or len(selected) >= 7:
            break
        if selected and selected[0]["box"][1] - line["box"][3] > 24:
            break
        selected.insert(0, line)
    heading = " ".join(untrack(v["text"]) for v in selected)
    if (
        not (1 if allow_short else 3) <= len(_words(heading)) <= 45
        or re.match(
            r"^(?:fig(?:ure)?\.?\s*\d+|table\s*\d+|plate\s*\d+)\b", heading, re.I
        )
        or _ROLE.search(heading)
        or re.search(r"\b(?:references|bibliography|contents)\b", heading, re.I)
        or (
            not allow_quotes
            and re.search(r'[“”"«»]', heading)
            and (not allow_embedded_quotes or re.match(r'^[“"«]', heading))
        )
    ):
        return "", []
    return heading, selected


def layout_credit_reports(lines, width, height, *, page=1, repeated_headers=()):
    """A heading, separately sized byline and following body share a column."""
    lines = sorted(lines, key=lambda v: (v["box"][1], v["box"][0]))
    for index, credit in enumerate(lines):
        names = _person(credit)
        if not names and page == 1 and credit["text"].islower():
            header_names = _names(credit["text"].title())
            after = [v for v in lines[index + 1 :] if v["box"][1] >= credit["box"][3]]
            if header_names and after and (credit["box"][3] < height * 0.2):
                heading_line = after[0]
                body_lines = [v for v in after[1:] if len(v["text"].split()) >= 7]
                body_size = sorted((v["size"] for v in body_lines))
                if (
                    2 <= len(_words(heading_line["text"])) <= 24
                    and heading_line["size"] >= credit["size"] * 1.1
                    and body_size
                    and (credit["size"] >= body_size[len(body_size) // 2] * 1.25)
                    and (sum((len(v["text"].split()) for v in body_lines)) >= 100)
                    and (not _ROLE.search(heading_line["text"]))
                ):
                    yield _report(
                        header_names,
                        heading_line["text"],
                        credit,
                        page,
                        "prominent_lowercase_byline_above_heading_and_body",
                    )
        if (
            not names
            or credit["box"][1] > height * 0.86
            or credit.get("confidence", 100) < 70
        ):
            continue
        following = [
            v
            for v in lines[index + 1 :]
            if v["box"][1] >= credit["box"][3] - 2
            and v["box"][1] - credit["box"][3] < 135
        ]
        abstract = any(
            (
                re.match("^\\s*Abstract\\b", v["text"], re.I)
                for v in lines[index + 1 :]
                if 0 <= v["box"][1] - credit["box"][3] < 180
            )
        )
        body_sizes = sorted(
            (v["size"] for v in following if len(v["text"].split()) >= 7)
        )
        strong_opening = (
            page == 1
            and credit["box"][1] < height * 0.35
            and body_sizes
            and (credit["size"] >= body_sizes[len(body_sizes) // 2] * 1.2)
        )
        local_numbered_work = any(
            0 <= credit["box"][1] - v["box"][3] <= 150
            and re.match(
                r"^(?:chapter\s*)?\d{1,3}(?:[.:]?\s+|$)", v["text"].strip(), re.I
            )
            and len(v["text"].split()) <= 7
            for v in lines[max(0, index - 6) : index]
        )
        heading, cluster = _heading(
            lines,
            index,
            credit["size"],
            allow_quotes=abstract,
            allow_embedded_quotes=bool(strong_opening),
            allow_short=local_numbered_work,
        )
        if heading and 0 <= credit["box"][1] - cluster[-1]["box"][3] <= 80:
            left = min((v["box"][0] for v in cluster))
            right = max((v["box"][2] for v in cluster))
            following = [
                v
                for v in lines[index + 1 :]
                if v["box"][1] >= credit["box"][3] - 2
                and v["box"][1] - credit["box"][3] < 135
            ]
            body = [
                v
                for v in following
                if len(v["text"].split()) >= 5 and v["size"] <= credit["size"] * 1.6
            ]
            if body and sum((len(v["text"].split()) for v in body)) >= 20:
                body_left = min((v["box"][0] for v in body))
                body_right = max((v["box"][2] for v in body))
                if (
                    (
                        abs(body_left - left) <= 40
                        or body_left <= left <= body_right
                        or (abstract and left <= body_left <= right)
                    )
                    and credit["box"][0] >= min(left, body_left) - 20
                    and (credit["box"][2] <= max(right, body_right) + 25)
                ):
                    yield _report(
                        names, heading, credit, page, "physical_heading_byline_body"
                    )
        if (
            page == 1
            and credit["text"].isupper()
            and (credit["box"][1] < height * 0.45)
        ):
            cluster = []
            for line in reversed(lines[:index]):
                if (
                    not line["text"].isupper()
                    or line["size"] < credit["size"] * 0.95
                    or line["size"] > credit["size"] * 1.8
                    or (cluster and cluster[0]["box"][1] - line["box"][3] > 25)
                ):
                    break
                cluster.insert(0, line)
            heading = " ".join((v["text"] for v in cluster))
            body = [
                v
                for v in lines[index + 1 :]
                if len(v["text"].split()) >= 7 and v["size"] <= credit["size"] * 0.85
            ]
            center = (credit["box"][0] + credit["box"][2]) / 2
            if (
                cluster
                and 5 <= len(_words(heading)) <= 30
                and (not _ROLE.search(heading))
                and (5 <= credit["box"][1] - cluster[-1]["box"][3] <= 70)
                and all(
                    (
                        abs((v["box"][0] + v["box"][2]) / 2 - center) <= width * 0.1
                        for v in cluster
                    )
                )
                and (sum((len(v["text"].split()) for v in body)) >= 80)
            ):
                yield _report(
                    names, heading, credit, page, "centered_display_title_and_byline"
                )
        if (
            credit["box"][3] < height * 0.16
            and tuple(_words(", ".join(names))) in repeated_headers
        ):
            start = index + 1
            while start < len(lines) and lines[start]["box"][1] < credit["box"][3]:
                start += 1
            cluster = []
            for line in lines[start : start + 7]:
                if line["box"][1] - credit["box"][3] > height * 0.3:
                    break
                if line["size"] < credit["size"] * 1.6:
                    break
                cluster.append(line)
            heading = " ".join((v["text"] for v in cluster))
            if (
                5 <= len(_words(heading)) <= 30
                and (not _ROLE.search(heading))
                and (not re.search("\\breviews?\\b", heading, re.I))
            ):
                yield _report(
                    names,
                    heading,
                    credit,
                    page,
                    "repeated_header_above_large_work_heading",
                )
        chapter = any(
            (
                re.fullmatch("chapter\\d+", "".join(v["text"].split()), re.I)
                for v in lines[:index]
            )
        )
        if page == 1 and credit["box"][1] < height * 0.3 and (chapter or abstract):
            cluster = []
            for v in following:
                if v["size"] < credit["size"] * 0.85 or re.match(
                    "^abstract\\b", v["text"], re.I
                ):
                    break
                if len(v["text"].split()) > 15 or v["box"][1] - credit["box"][3] > 110:
                    break
                cluster.append(v)
            heading = " ".join((v["text"] for v in cluster))
            body = [
                v
                for v in lines[index + 1 :]
                if cluster
                and v["box"][1] >= cluster[-1]["box"][3]
                and (v["box"][1] - cluster[-1]["box"][3] < 140)
                and (len(v["text"].split()) >= 7)
                and (v["size"] < credit["size"] * 0.95)
            ]
            if (
                3 <= len(_words(heading)) <= 30
                and (not _ROLE.search(heading))
                and body
                and (sum((len(v["text"].split()) for v in body)) >= 20)
                and all(
                    (
                        credit["box"][0] >= v["box"][0] - 30
                        and credit["box"][2] <= v["box"][2] + 30
                        for v in cluster
                    )
                )
            ):
                yield _report(
                    names,
                    heading,
                    credit,
                    page,
                    "chapter_or_abstract_author_above_heading",
                )
    return


def layout_credit(lines, width, height, *, page=1, repeated_headers=()):
    reports = list(
        layout_credit_reports(
            lines, width, height, page=page, repeated_headers=repeated_headers
        )
    )
    return reports[0] if reports else not_found()


def publisher_credit_reports(lines, *, page=1):
    """Small OCR title page; keep original creators ahead of later role credits."""
    lines = sorted(lines, key=lambda v: (v["box"][1], v["box"][0]))
    if sum((len(v["text"].split()) for v in lines)) > 100:
        return
    for publisher, line in enumerate(lines):
        publisher_key = "".join(_words(line["text"]))
        if not re.search(
            "(?:university[a-z]{0,40}press|institutionpress|publishers)", publisher_key
        ):
            continue
        boundary = next(
            (i for i, v in enumerate(lines[:publisher]) if _ROLE.search(v["text"])),
            publisher,
        )
        for index in range(boundary - 1, 0, -1):
            credit = lines[index]
            names = _person(credit)
            first = index
            if index and re.search("\\s+and\\s*$", lines[index - 1]["text"], re.I):
                leading = _names(
                    re.sub("\\s+and\\s*$", "", lines[index - 1]["text"], flags=re.I)
                )
                if leading and names:
                    names = leading + names
                    first = index - 1
            if not names or credit.get("confidence", 100) < 70:
                continue
            if boundary < publisher and re.search(
                "\\bedited by\\b", lines[boundary]["text"], re.I
            ):
                if (
                    first == index
                    or first == 0
                    or lines[first]["box"][1] - lines[first - 1]["box"][3]
                    < credit["size"] * 2
                ):
                    continue
            title = " ".join((v["text"] for v in lines[:first]))
            if len(_words(title)) < 3 or _ROLE.search(title):
                continue
            title_size = max((v["size"] for v in lines[:first]), default=0)
            if (
                credit["size"] >= line["size"] * 1.1
                and title_size >= credit["size"] * 1.2
                or (
                    len(names) >= 2
                    and boundary < publisher
                    and (title_size >= credit["size"] * 0.8)
                )
            ):
                yield _report(
                    names, title, credit, page, "compact_publisher_titlepage_role_order"
                )
    return


def publisher_credit(lines, *, page=1):
    reports = list(publisher_credit_reports(lines, page=page))
    return reports[0] if reports else not_found()


def read_tsv(payload):
    """Recover the same OCR text plus positioned lines from one Tesseract call."""
    text = payload.decode("utf-8", errors="replace")
    if not text.startswith("level\tpage_num\t"):
        return text, []
    grouped = defaultdict(list)
    for row in csv.DictReader(
        io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE
    ):
        if row.get("level") == "5" and row.get("text", "").strip():
            grouped[(row["block_num"], row["par_num"], row["line_num"])].append(row)
    lines = []
    for words in grouped.values():
        x = min(int(w["left"]) for w in words)
        y = min(int(w["top"]) for w in words)
        right = max(int(w["left"]) + int(w["width"]) for w in words)
        bottom = max(int(w["top"]) + int(w["height"]) for w in words)
        sizes = sorted(int(w["height"]) for w in words)
        confidence = sum(float(w["conf"]) for w in words) / len(words)
        lines.append(
            {
                "text": " ".join(w["text"] for w in words),
                "box": [x, y, right, bottom],
                "size": sizes[len(sizes) // 2],
                "confidence": confidence,
            }
        )
    return "\n".join(v["text"] for v in lines), lines


def _native_page(page):
    import fitz

    lines = []
    for block in page.get_text(
        "dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
    )["blocks"]:
        for line in block.get("lines", []):
            spans = line["spans"]
            text = "".join(s["text"] for s in spans)
            if text.strip() and abs(line.get("dir", (1, 0))[0]) > 0.9:
                lines.append(
                    {
                        "text": text,
                        "box": list(line["bbox"]),
                        "size": max(s["size"] for s in spans),
                    }
                )
    # Uppercase display names are sometimes split into adjacent PDF lines
    # on one baseline. Rejoin only close, same-size capital fragments.
    joined = []
    for line in sorted(lines, key=lambda v: (v["box"][1], v["box"][0])):
        previous = joined[-1] if joined else None
        if (
            previous
            and line["text"].strip().isupper()
            and previous["text"].strip().isupper()
            and len(line["text"].split()) <= 3
            and len(previous["text"].split()) <= 3
            and abs(line["box"][1] - previous["box"][1]) < 1
            and abs(line["size"] - previous["size"]) < 0.3
            and 0 <= line["box"][0] - previous["box"][2] <= line["size"] * 1.2
        ):
            previous["text"] += " " + line["text"]
            previous["box"] = [
                min(previous["box"][0], line["box"][0]),
                min(previous["box"][1], line["box"][1]),
                max(previous["box"][2], line["box"][2]),
                max(previous["box"][3], line["box"][3]),
            ]
        else:
            joined.append(dict(line))
    return joined


def _collapsed_byline(page, line):
    """OCR may restore spaces only when every native letter is corroborated."""
    import fitz

    executable = (
        shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    )
    if not re.fullmatch(r"BY[A-Z]{7,60}", line["text"].strip()):
        return []
    crop = (fitz.Rect(line["box"]) + (-4, -4, 4, 4)) & page.rect
    try:
        scale = min(4, math.sqrt(1_000_000 / max(1, crop.width * crop.height)))
        png = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=crop).tobytes(
            "png"
        )
        result = subprocess.run(
            [executable, "stdin", "stdout", "-l", "eng", "--psm", "7"],
            input=png,
            capture_output=True,
            timeout=2,
        )
    except OSError, RuntimeError, subprocess.TimeoutExpired:
        return []
    value = result.stdout.decode("utf-8", errors="replace").strip()
    names = _names(value) if re.match(r"^BY\s+", value, re.I) else []
    return (
        names
        if "".join(_words(", ".join(names))) == line["text"].strip()[2:].casefold()
        else []
    )


def infer(context):
    if (
        context.path is None
        or context.profile.kind not in {"generic", "scholarly_article", "book_chapter"}
        or context.profile.work_scope == "whole_issue"
    ):
        return not_found()
    if context.evidence_bundle is not None:
        pages = [
            p
            for p in context.evidence_bundle.get("native", [])
            if p["page"] <= 5
            and "horizontal_lines" in p
            and "width" in p
            and "height" in p
        ]
        headers = {
            _words(", ".join(_person(line)))
            for p in pages
            if p["page"] > 1
            for line in p.get("horizontal_lines", [])
            if line["box"][3] < p["height"] * 0.16 and _person(line)
        }
        reports = [
            layout_credit(
                p.get("horizontal_lines", []),
                p["width"],
                p["height"],
                page=p["page"],
                repeated_headers=headers,
            )
            for p in pages
        ]
        return next((r for r in reports if r.get("author")), not_found())
    import fitz

    try:
        with fitz.open(context.path) as doc:
            report = not_found()
            crop_attempted = False
            pages = [
                (_native_page(doc[i]), doc[i].rect.width, doc[i].rect.height)
                for i in range(min(3, len(doc)))
            ]
            headers = set()
            for lines, _, height in pages[1:]:
                for line in lines:
                    if line["box"][3] < height * 0.16 and _person(line):
                        headers.add(_words(", ".join(_person(line))))
            for i, (lines, width, height) in enumerate(pages[:2]):
                report = layout_credit(
                    lines, width, height, page=i + 1, repeated_headers=headers
                )
                if report.get("author"):
                    break
                for index, line in enumerate(
                    sorted(lines, key=lambda v: (v["box"][1], v["box"][0]))
                ):
                    if not re.fullmatch(r"BY[A-Z]{7,60}", line["text"].strip()):
                        continue
                    preceding = [
                        v
                        for v in lines
                        if v["box"][3] <= line["box"][1]
                        and line["box"][1] - v["box"][3] < 180
                    ]
                    if not any(v["size"] > line["size"] * 1.2 for v in preceding):
                        continue
                    if crop_attempted:
                        continue
                    crop_attempted = True
                    names = _collapsed_byline(doc[i], line)
                    if names:
                        report = _report(
                            names,
                            "explicit positioned By credit",
                            line,
                            i + 1,
                            "ocr_space_restoration_native_letters_verified",
                        )
                        break
                if report.get("author"):
                    break
        return report
    except OSError, ValueError, RuntimeError:
        return not_found()
