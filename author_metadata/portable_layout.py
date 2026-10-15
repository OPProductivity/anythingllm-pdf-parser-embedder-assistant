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
    return _names(untrack(line["text"])) if not _ROLE.search(line["text"]) else []


def _report(names, heading, credit, page, basis):
    return {
        "author": ", ".join(names),
        "source": "text_structured_opening_credit",
        "page": page,
        "evidence": heading + " / " + untrack(credit["text"]),
        "mechanism": basis,
        "geometry": {"byline": credit["box"]},
    }


def _heading(lines, end, credit_size):
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
        len(_words(heading)) < 3
        or re.match(
            r"^(?:fig(?:ure)?\.?\s*\d+|table\s*\d+|plate\s*\d+)\b", heading, re.I
        )
        or _ROLE.search(heading)
        or re.search(r'[“”"«»]|\b(?:references|bibliography|contents)\b', heading, re.I)
    ):
        return "", []
    return heading, selected


def layout_credit(lines, width, height, *, page=1, repeated_headers=()):
    """A heading, separately sized byline and following body share a column."""
    lines = sorted(lines, key=lambda v: (v["box"][1], v["box"][0]))
    for index, credit in enumerate(lines):
        names = _person(credit)
        if (
            not names
            or credit["box"][1] > height * 0.86
            or credit.get("confidence", 100) < 70
        ):
            continue
        heading, cluster = _heading(lines, index, credit["size"])
        if heading and 0 <= credit["box"][1] - cluster[-1]["box"][3] <= 80:
            left = min(v["box"][0] for v in cluster)
            right = max(v["box"][2] for v in cluster)
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
            if body and sum(len(v["text"].split()) for v in body) >= 20:
                body_left = min(v["box"][0] for v in body)
                body_right = max(v["box"][2] for v in body)
                if (
                    abs(body_left - left) <= 40
                    and credit["box"][0] >= min(left, body_left) - 20
                    and credit["box"][2] <= max(right, body_right) + 25
                ):
                    return _report(
                        names, heading, credit, page, "physical_heading_byline_body"
                    )
        # Some journals put their byline in a running header above a large title.
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
            heading = " ".join(v["text"] for v in cluster)
            if (
                5 <= len(_words(heading)) <= 30
                and not _ROLE.search(heading)
                and not re.search(r"\breviews?\b", heading, re.I)
            ):
                return _report(
                    names,
                    heading,
                    credit,
                    page,
                    "repeated_header_above_large_work_heading",
                )
    return not_found()


def publisher_credit(lines, *, page=1):
    """Small OCR title page; keep original creators ahead of later role credits."""
    lines = sorted(lines, key=lambda v: (v["box"][1], v["box"][0]))
    if sum(len(v["text"].split()) for v in lines) > 100:
        return not_found()
    for publisher, line in enumerate(lines):
        publisher_key = "".join(_words(line["text"]))
        if not re.search(
            r"(?:university[a-z]{0,40}press|institutionpress|publishers)", publisher_key
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
            if index and re.search(r"\s+and\s*$", lines[index - 1]["text"], re.I):
                leading = _names(
                    re.sub(r"\s+and\s*$", "", lines[index - 1]["text"], flags=re.I)
                )
                if leading and names:
                    names = leading + names
                    first = index - 1
            if not names or credit.get("confidence", 100) < 70:
                continue
            if boundary < publisher and re.search(
                r"\bedited by\b", lines[boundary]["text"], re.I
            ):
                # A subtitle is not a creator credit on an edited volume.
                # Only a separate, wrapped original-author group precedes
                # this role boundary with sufficient independent typography.
                if (
                    first == index
                    or first == 0
                    or lines[first]["box"][1] - lines[first - 1]["box"][3]
                    < credit["size"] * 2
                ):
                    continue
            title = " ".join(v["text"] for v in lines[:first])
            if len(_words(title)) < 3 or _ROLE.search(title):
                continue
            title_size = max((v["size"] for v in lines[:first]), default=0)
            if (
                credit["size"] >= line["size"] * 1.1
                and title_size >= credit["size"] * 1.2
                or len(names) >= 2
                and boundary < publisher
                and title_size >= credit["size"] * 0.8
            ):
                return _report(
                    names, title, credit, page, "compact_publisher_titlepage_role_order"
                )
    return not_found()


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
    return lines


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
    import fitz

    try:
        stat = context.path.stat()
        key = (str(context.path), stat.st_size, stat.st_mtime_ns)
        if key in _CACHE:
            return dict(_CACHE[key])
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
                    crop_key = key + (i, tuple(line["box"]))
                    names = _CROP_CACHE.get(crop_key) or _collapsed_byline(doc[i], line)
                    if names:
                        _CROP_CACHE[crop_key] = names
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
        if len(_CACHE) > 256:
            _CACHE.clear()
            _CROP_CACHE.clear()
        if report.get("author"):
            _CACHE[key] = dict(report)
        return report
    except OSError, ValueError, RuntimeError:
        return not_found()
