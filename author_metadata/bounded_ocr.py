"""Small, bounded OCR recovery for absent or corrupt native opening evidence."""

import math
from pathlib import Path
import re
import shutil
import subprocess
import time

from .structured_credit import _names, _title_supported, _words
from .title_evidence import not_found

_CACHE = {}


def _credit(text, context, page):
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
                return {
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
                return {
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
                return {
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
            if title_ok and len(" ".join(lines[index + 1 : index + 4]).split()) >= 12:
                return {
                    "author": ", ".join(names),
                    "source": "text_ocr_author_credit",
                    "page": page,
                    "evidence": visible + " / " + line,
                }
    return not_found()


def infer(context):
    if context.path is None or context.profile.kind in {
        "periodical_issue",
        "legal_opinion",
        "corporate_publication",
        "review",
    }:
        return not_found()
    native = "".join(s.get("text", "") for s in context.opening_pages)
    corrupt = (
        sum(ord(c) < 32 and c not in "\r\n\t" for c in native) > len(native) * 0.04
    )
    if len(native.strip()) >= 80 and not corrupt:
        return not_found()
    path = Path(context.path)
    if not path.is_file():
        return not_found()
    executable = (
        shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    )
    if not Path(executable).is_file():
        return not_found()
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns, context.title_hint)
    if key in _CACHE:
        return dict(_CACHE[key])
    import fitz

    started = time.monotonic()
    result = not_found()
    try:
        with fitz.open(path) as doc:
            for index in range(min(4, len(doc))):
                page = doc[index]
                rect = page.rect
                clips = (
                    [None]
                    if rect.width < rect.height * 1.15
                    else [
                        fitz.Rect(rect.width * 0.54, 0, rect.width, rect.height),
                        fitz.Rect(0, 0, rect.width * 0.46, rect.height),
                    ]
                )
                for clip in clips:
                    remaining = 6 - (time.monotonic() - started)
                    if remaining < 0.5:
                        break
                    region = clip or rect
                    scale = min(
                        2.5, math.sqrt(4_000_000 / max(1, region.width * region.height))
                    )
                    png = page.get_pixmap(
                        matrix=fitz.Matrix(scale, scale), clip=clip
                    ).tobytes("png")
                    completed = subprocess.run(
                        [executable, "stdin", "stdout", "-l", "eng", "--psm", "3"],
                        input=png,
                        capture_output=True,
                        timeout=min(3, remaining),
                    )
                    text = completed.stdout.decode("utf-8", errors="replace")
                    result = _credit(text, context, index + 1)
                    if result.get("author"):
                        # OCR sometimes merges a punctuation mark into a middle
                        # initial. Verify that narrow ambiguity at another scale;
                        # use an initial only when both readings support it.
                        if re.search(r"\b[A-Z]{2,3}\.\s+\S+$", result["author"]):
                            scale2 = min(
                                2,
                                math.sqrt(
                                    4_000_000 / max(1, region.width * region.height)
                                ),
                            )
                            png2 = page.get_pixmap(
                                matrix=fitz.Matrix(scale2, scale2), clip=clip
                            ).tobytes("png")
                            second = subprocess.run(
                                [
                                    executable,
                                    "stdin",
                                    "stdout",
                                    "-l",
                                    "eng",
                                    "--psm",
                                    "3",
                                ],
                                input=png2,
                                capture_output=True,
                                timeout=min(
                                    2, max(0.5, 6 - (time.monotonic() - started))
                                ),
                            )
                            alternate = _credit(
                                second.stdout.decode("utf-8", errors="replace"),
                                context,
                                index + 1,
                            )
                            a, b = (
                                result["author"].split(),
                                alternate.get("author", "").split(),
                            )
                            if len(a) == len(b) and all(
                                x == y
                                or (
                                    re.fullmatch(r"[A-Z]{1,3}\.", x)
                                    and re.fullmatch(r"[A-Z]{1,3}\.", y)
                                    and x[0] == y[0]
                                )
                                for x, y in zip(a, b)
                            ):
                                result["author"] = " ".join(
                                    min((x, y), key=len) for x, y in zip(a, b)
                                )
                                result["ocr_initial_recheck"] = [a, b]
                        result.update(
                            {
                                "extraction": "tesseract",
                                "crop": list(clip) if clip else "whole_page",
                                "seconds": round(time.monotonic() - started, 3),
                            }
                        )
                        break
                if result.get("author") or time.monotonic() - started >= 5.5:
                    break
    except OSError, ValueError, RuntimeError, subprocess.TimeoutExpired:
        result = not_found()
    if len(_CACHE) > 128:
        _CACHE.clear()
    if result.get("author"):
        _CACHE[key] = dict(result)
    return result
