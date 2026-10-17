"""Acquire a fixed evidence plan before any author verdict.

Collection is independent of author confidence and genre. Failed reads stay in
an explicit completeness ledger. Each new call acquires its own evidence.
"""

from dataclasses import replace
import math
import os
import io
from pathlib import Path
import re
import shutil
import subprocess
import time

# Compatibility handle for old diagnostics; cross-call evidence is not retained.
_CACHE = {}


def text_quality(text):
    compact = "".join(text.split())
    if not compact:
        return "sparse_native"
    controls = sum(ord(c) < 32 and c not in "\r\n\t" for c in text)
    punctuation = sum(not (c.isalnum() or c in ".,;:’()-–—%/?&") for c in compact)
    if controls > 0 or "\ufffd" in text or punctuation > len(compact) * 0.20:
        return "corrupt_native"
    if len(compact) < 80:
        return "sparse_native"
    return "usable_native"


def _invisible_native_overlay(page):
    spans = page.get_texttrace()
    total = sum(len(span.get("chars", ())) for span in spans)
    hidden = sum(len(span.get("chars", ())) for span in spans if span.get("type") == 3)
    return total > 0 and hidden / total >= 0.5


def _image_regions(page, *, invisible_overlay=None):
    """Image areas without a usable native text layer need their own reading."""
    import fitz

    words = page.get_text("words")
    invisible_overlay = (
        _invisible_native_overlay(page)
        if invisible_overlay is None
        else invisible_overlay
    )
    regions = []
    area = max(1, page.rect.get_area())
    for info in page.get_image_info():
        rect = fitz.Rect(info["bbox"]) & page.rect
        narrow_text_shape = (
            rect.width >= 60
            and 8 <= rect.height <= 60
            and rect.width >= rect.height * 2
        )
        if rect.is_empty or (
            rect.get_area() / area < 0.04
            and not (narrow_text_shape and rect.get_area() / area >= 0.001)
        ):
            continue
        overlay = " ".join(
            str(w[4]) for w in words if fitz.Rect(w[:4]).intersects(rect)
        )
        covered = [
            fitz.Rect(w[:4]) & rect for w in words if fitz.Rect(w[:4]).intersects(rect)
        ]
        native_area = sum(box.get_area() for box in covered)
        # A few native captions/overlay words do not represent the lettering
        # elsewhere in a large cover image. Keep it as an acquisition task.
        partial_overlay = (
            rect.get_area() / area >= 0.35 and native_area < rect.get_area() * 0.025
        )
        if (
            text_quality(overlay) != "usable_native"
            or partial_overlay
            or (invisible_overlay and rect.get_area() / area >= 0.65)
        ):
            if not any(
                (rect & old).get_area() >= rect.get_area() * 0.9 for old in regions
            ):
                regions.append(rect)
    return regions


def _plan(document, extra_text="", *, ocr_pages=None):
    """Opening and closing apparatus, extended by publishing structure."""
    pages = set(range(1, min(5, len(document)) + 1))
    page_text = {}
    for number in sorted(pages):
        try:
            page_text[number] = document[number - 1].get_text("text", sort=True)
        except OSError, ValueError, RuntimeError:
            # The scheduled native attempt records this failure later and
            # still schedules a rendered-image reading of the page.
            page_text[number] = ""
    opening = "\n".join(page_text.values())
    opening += "\n" + extra_text
    extensions = []
    # This describes a publishing sequence, not whether an author was found.
    if ocr_pages is not None:
        for region in ocr_pages:
            number = region["page"]
            if number in page_text:
                page_text[number] += "\n" + region.get("text", "")
    elif extra_text and pages:
        # Compatibility callers without page coordinates retain their supplied
        # structure. Actual collection passes per-page OCR, excluding closings.
        page_text[max(pages)] += "\n" + extra_text
    apparatus_at, body_at = 0, 0
    apparatus_kind = ""
    for number, text in page_text.items():
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        heading = "\n".join(lines[:10])
        labelled = re.search(
            r"(?im)^(?:table of contents|contents(?: at a glance)?|detailed contents|"
            r"list of (?:figures|tables|illustrations)|praise(?: for.*)?|endorsements|acclaim|"
            r"foreword(?: by.*)?|preface(?: to.*)?|acknowledg\w*|dedication|"
            r"about (?:the )?(?:authors?|editors?)|title page|"
            r"inhaltsverzeichnis|sommaire|sum[aá]rio|inhoudsopgave|"
            r"pr[eé]face|pr[oó]logo|pref[aá]cio|d[eé]dicace)\s*$",
            heading,
        )
        legal = (
            sum(
                bool(re.search(pattern, text, re.I))
                for pattern in (
                    r"©|\bcopyright\b",
                    r"all rights reserved|rights? .* identified",
                    r"\bISBN\b",
                    r"catalog(?:u)?ing.in.publication",
                    r"first published|published by",
                )
            )
            >= 3
        )
        sparse_imprint = len(text.split()) < 160 and bool(
            re.search(
                r"\b(?:university press|a series edited by|catalog(?:u)?ing.in.publication)\b",
                text,
                re.I,
            )
        )
        if labelled or legal or sparse_imprint:
            apparatus_at = number
            apparatus_kind = labelled[0].casefold() if labelled else "legal_or_imprint"
        body = re.search(
            r"(?im)^(?:abstract[.:]?|(?:\d{1,2}[. ]+)?(?:general )?introduction|"
            r"chapter\s+\d+\b|part\s+(?:[ivxlcdm]+|\d+|one|two|three)\b)",
            heading,
        )
        dense_prose = (
            len(text.split()) >= 180 and len(re.findall(r"[.!?]\s+[A-Z]", text)) >= 6
        )
        if (body and not labelled) or (
            dense_prose
            and number > apparatus_at
            and apparatus_kind == "legal_or_imprint"
        ):
            body_at = number
    # The word copyright inside a citation or a closing legal page does not
    # imply that the opening is still front matter. A later body boundary can
    # close that sequence, regardless of whether an author has been found.
    frontmatter = apparatus_at > 0 and body_at < apparatus_at
    if frontmatter:
        later = list(range(6, min(12, len(document)) + 1))
        pages.update(later)
        if later:
            extensions.append({"reason": "front_matter_sequence", "pages": later})
    # Closing author biographies and relocated catalogue records are common
    # responsibility evidence. Merely finding an opening name cannot cancel it.
    closing = (
        sorted({max(1, len(document) - 1), len(document)} - pages)
        if len(document)
        else []
    )
    if closing:
        pages.update(closing)
        extensions.append(
            {"reason": "closing_responsibility_apparatus", "pages": closing}
        )
    editorial = bool(re.search(r"(?i)\b(?:guest\s*)?editorial\b", opening[:2000]))
    if editorial and len(document) <= 24:
        later = list(range(6, len(document) + 1))
        pages.update(later)
        if later:
            extensions.append(
                {"reason": "short_editorial_signature_boundary", "pages": later}
            )
    return sorted(pages), extensions


def _apparatus_continuation(document, opening_plan, native, ocr):
    """Follow continuing front matter, not the presence or absence of a name."""
    if not opening_plan:
        return [], False
    last = max(opening_plan)
    text_by_page = {r["page"]: r.get("text", "") for r in native}
    for region in ocr:
        text_by_page[region["page"]] = (
            text_by_page.get(region["page"], "") + "\n" + region.get("text", "")
        )

    def apparatus(number):
        text = text_by_page.get(number, "")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        heading = " ".join(lines[:3])
        labelled = re.match(
            r"^(?:contents|table of contents|detailed contents|list of (?:figures|tables|illustrations)|"
            r"praise|endorsements|foreword|preface|acknowledg\w*|copyright|"
            r"about (?:the )?(?:authors?|editors?)|inhaltsverzeichnis|sommaire|inhoudsopgave)\b",
            heading,
            re.I,
        )
        locators = sum(
            bool(re.search(r"\.{2,}\s*(?:\d+|[ivxlcdm]+)\s*$", line, re.I))
            for line in lines
        )
        return bool(labelled or locators >= 4)

    blank = not text_by_page.get(last, "").strip()
    continuing = apparatus(last) or (
        blank and any(apparatus(n) for n in range(max(1, last - 2), last))
    )
    # A declared bounded window avoids scanning entire books. Reaching this
    # limit is visible in the trace rather than disguised as a completed work.
    if not continuing or last >= len(document):
        return [], False
    if last >= 32:
        return [], True
    return list(range(last + 1, min(last + 3, 32, len(document)) + 1)), False


def _read_region(document, task, executable):
    """Acquire a region with a quality-driven retry, never an author-driven stop."""
    import fitz
    from .portable_layout import read_tsv

    page = document[task["page"] - 1]
    region = fitz.Rect(task["clip"])
    line_task = task.get("reason") in {"collapsed_explicit_byline", "narrow_text_image"}
    scale = min(
        4.0 if line_task else 2.5,
        math.sqrt((1_000_000 if line_task else 4_000_000) / max(1, region.get_area())),
    )
    attempts, readings = [], []
    schedule = [(7 if line_task else 3, scale, task["reason"])]
    for psm, factor, reason in schedule:
        attempt = {"kind": "ocr", **task, "reason": reason, "psm": psm, "scale": factor}
        try:
            if not Path(executable).is_file():
                raise OSError("Tesseract unavailable")
            pix = page.get_pixmap(
                matrix=fitz.Matrix(factor, factor), clip=region, alpha=False
            )
            # Pure white pixels contain no visual lettering to OCR. This is an
            # explicit image inspection, not an inference from an accepted name.
            if pix.samples and pix.samples.count(b"\xff") == len(pix.samples):
                attempt.update(status="blank_region", characters=0)
                attempts.append(attempt)
                return [], attempts
            from PIL import Image, ImageOps

            pixels = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            histogram = pixels.convert("L").histogram()
            total = max(1, pix.width * pix.height)
            preprocessing = []
            # White lettering on a dark cover needs the same contrast
            # convention as ordinary printed text. A solid black rectangle
            # has no light glyphs and is not silently declared a blank page.
            if (
                sum(histogram[:90]) / total > 0.70
                and sum(histogram[180:]) / total > 0.003
            ):
                pixels = ImageOps.invert(pixels)
                preprocessing.append("dark_background_inverted")
            border = 10 if line_task else 0
            if border:
                pixels = ImageOps.expand(pixels, border=border, fill="white")
                preprocessing.append("tight_line_white_border")
            payload = io.BytesIO()
            pixels.save(payload, format="PNG")
            attempt["preprocessing"] = preprocessing
            result = subprocess.run(
                [executable, "stdin", "stdout", "-l", "eng", "--psm", str(psm), "tsv"],
                input=payload.getvalue(),
                capture_output=True,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
                timeout=10,
                check=True,
            )
            text, lines = read_tsv(result.stdout)
            if border:
                for line in lines:
                    line["box"] = [value - border for value in line["box"]]
            confidences = [line["confidence"] for line in lines if "confidence" in line]
            confidence = sum(confidences) / len(confidences) if confidences else None
            block = {
                **task,
                "text": text,
                "lines": lines,
                "scale": factor,
                "width": region.width * factor,
                "height": region.height * factor,
                "extraction": "tesseract",
                "coordinate_space": "crop_pixels",
                "psm": psm,
                "ocr_word_confidence_mean": confidence,
            }
            block["preprocessing"] = preprocessing
            readings.append(block)
            attempt.update(
                status="read", characters=len(text), ocr_word_confidence_mean=confidence
            )
            if len(schedule) == 1:
                if not text.strip() or (confidence is not None and confidence < 40):
                    schedule.append(
                        (1, factor, "empty_or_low_quality_orientation_recheck")
                    )
                elif re.search(r"\b[A-Z]{2,3}\.\s+[A-Z][a-z]+", text):
                    schedule.append((3, 2.0, "ambiguous_initials_recheck"))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            attempt.update(status="failed", error=str(error))
            if len(schedule) == 1 and Path(executable).is_file():
                schedule.append((6, min(2.0, scale), "transient_read_retry"))
        attempts.append(attempt)
    if not readings:
        return [], attempts
    chosen = max(
        readings,
        key=lambda reading: (
            bool(reading["text"].strip()),
            reading.get("ocr_word_confidence_mean") or 0,
        ),
    )
    alternate = next((reading for reading in readings if reading is not chosen), None)
    if alternate is not None:
        chosen["alternate"] = alternate
    return [chosen], attempts


def collect(context):
    """Finish every planned native and image-region inspection before deciding."""
    if context.evidence_bundle is not None:
        return context
    trace = {
        "version": 2,
        "cross_call_cache": False,
        "native_plan": [],
        "opening_plan": [],
        "ocr_plan": [],
        "attempts": [],
        "extensions": [],
        "complete": True,
        "scope": "planned opening and closing apparatus, not entire book",
    }
    if context.path is None or not Path(context.path).is_file():
        trace.update(
            mode="supplied_samples_only",
            native_plan=[s.get("page") for s in context.samples],
        )
        return replace(
            context,
            evidence_bundle={
                "trace": trace,
                "native": list(context.samples),
                "ocr": [],
            },
        )
    import fitz
    from .native_article import positioned_lines
    from .portable_layout import _native_page

    started = time.monotonic()
    native, ocr = [], []
    try:
        executable = (
            shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        )
        with fitz.open(context.path) as document:
            trace["document_page_count"] = len(document)
            pages, trace["extensions"] = _plan(document)
            trace["native_plan"] = pages
            trace["opening_plan"] = sorted(
                set(range(1, min(5, len(document)) + 1))
                | {
                    p
                    for extension in trace["extensions"]
                    if extension["reason"] == "front_matter_sequence"
                    for p in extension["pages"]
                }
            )
            tasks = []

            def read_native(number):
                try:
                    page = document[number - 1]
                    text = page.get_text("text", sort=True)
                    native.append(
                        {
                            "page": number,
                            "text": text,
                            "horizontal_lines": positioned_lines(page),
                            "layout_lines": _native_page(page),
                            "width": page.rect.width,
                            "height": page.rect.height,
                        }
                    )
                    trace["attempts"].append(
                        {"kind": "native", "page": number, "status": "read"}
                    )
                    native[-1]["visible_byline_texts"] = [
                        line["text"]
                        for line in native[-1]["horizontal_lines"]
                        if line.get("visible")
                        and re.match(r"^(?:by|written by)\s+", line["text"], re.I)
                    ]
                    for line in native[-1]["horizontal_lines"]:
                        if re.fullmatch(r"BY[A-Z]{7,60}", line["text"].strip()):
                            preceding = [
                                v
                                for v in native[-1]["horizontal_lines"]
                                if 0 <= line["box"][1] - v["box"][3] < 180
                            ]
                            if any(v["size"] > line["size"] * 1.2 for v in preceding):
                                crop = (
                                    fitz.Rect(line["box"]) + (-4, -4, 4, 4)
                                ) & page.rect
                                tasks.append(
                                    {
                                        "page": number,
                                        "clip": list(crop),
                                        "reason": "collapsed_explicit_byline",
                                        "native_letters": line["text"].strip()[2:],
                                    }
                                )
                    reason = text_quality(text)
                    invisible_overlay = _invisible_native_overlay(page)
                    clean_sparse_native = (
                        reason == "sparse_native"
                        and bool(text.strip())
                        and not page.get_image_info()
                        and not page.get_drawings()
                    )
                    regions = (
                        []
                        if clean_sparse_native
                        else [page.rect]
                        if reason != "usable_native"
                        else _image_regions(page, invisible_overlay=invisible_overlay)
                    )
                    if clean_sparse_native:
                        trace["attempts"].append(
                            {
                                "kind": "image_assessment",
                                "page": number,
                                "status": "clean_native_text_only",
                                "reason": "No embedded image or vector artwork remains unread; sparse native text alone does not require OCR",
                            }
                        )
                    if reason == "usable_native":
                        reason = "image_without_native_overlay"
                    for region in regions:
                        region_reason = (
                            "invisible_native_text_overlay"
                            if invisible_overlay
                            and region.get_area() >= page.rect.get_area() * 0.65
                            else reason
                        )
                        leaves = [region]
                        if (
                            region.width > region.height * 1.15
                            and region.height >= page.rect.height * 0.7
                            and region.get_area() >= page.rect.get_area() * 0.75
                        ):
                            middle = (region.x0 + region.x1) / 2
                            leaves = [
                                fitz.Rect(region.x0, region.y0, middle, region.y1),
                                fitz.Rect(middle, region.y0, region.x1, region.y1),
                            ]
                        tasks.extend(
                            {
                                "page": number,
                                "clip": list(leaf),
                                "reason": "narrow_text_image"
                                if leaf.width >= leaf.height * 2 and leaf.height <= 60
                                else region_reason,
                            }
                            for leaf in leaves
                        )
                except (OSError, ValueError, RuntimeError) as error:
                    trace["complete"] = False
                    trace["attempts"].append(
                        {
                            "kind": "native",
                            "page": number,
                            "status": "failed",
                            "error": str(error),
                        }
                    )

                    try:
                        failed_page = document[number - 1]
                        tasks.append(
                            {
                                "page": number,
                                "clip": list(failed_page.rect),
                                "reason": "native_read_failed",
                            }
                        )
                    except (OSError, ValueError, RuntimeError) as image_error:
                        trace["attempts"].append(
                            {
                                "kind": "ocr",
                                "page": number,
                                "status": "failed",
                                "reason": "native_read_failed",
                                "error": str(image_error),
                            }
                        )

            for number in pages:
                read_native(number)
            consumed = 0
            # OCR may reveal a front-matter sequence hidden from native text.
            # Continue from publishing structure, regardless of any author guess.
            stage = 0
            while True:
                pending = tasks[consumed:]
                consumed = len(tasks)
                trace["ocr_plan"] = tasks
                for task in pending:
                    try:
                        readings, attempts = _read_region(document, task, executable)
                        ocr.extend(readings)
                        trace["attempts"].extend(attempts)
                        if not readings and not any(
                            a["status"] == "blank_region" for a in attempts
                        ):
                            trace["complete"] = False
                    except (OSError, ValueError, RuntimeError) as error:
                        trace["complete"] = False
                        trace["attempts"].append(
                            {
                                "kind": "ocr",
                                **task,
                                "status": "failed",
                                "error": str(error),
                            }
                        )
                if stage == 0:
                    extra = "\n".join(r["text"] for r in ocr)
                    expanded, _ = _plan(document, extra, ocr_pages=ocr)
                    additional = sorted(set(expanded) - set(trace["native_plan"]))
                    if additional:
                        trace["native_plan"] = sorted(set(expanded))
                        trace["extensions"].append(
                            {
                                "reason": "ocr_revealed_publishing_sequence",
                                "pages": additional,
                            }
                        )
                        for number in additional:
                            read_native(number)
                        trace["opening_plan"] = sorted(
                            set(trace["opening_plan"]) | set(additional)
                        )
                additional_opening, limited = _apparatus_continuation(
                    document, trace["opening_plan"], native, ocr
                )
                if limited:
                    trace["planning_limit_reached"] = (
                        "Continuing front matter at the 32-page opening window"
                    )
                additional = sorted(set(additional_opening) - set(trace["native_plan"]))
                if additional_opening:
                    trace["opening_plan"] = sorted(
                        set(trace["opening_plan"]) | set(additional_opening)
                    )
                if additional:
                    trace["native_plan"] = sorted(
                        set(trace["native_plan"]) | set(additional)
                    )
                    trace["extensions"].append(
                        {"reason": "continuing_opening_apparatus", "pages": additional}
                    )
                    for number in additional:
                        read_native(number)
                stage += 1
                if consumed == len(tasks) and not additional:
                    break
    except (OSError, ValueError, RuntimeError) as error:
        trace.update(complete=False, error=str(error))
    trace["collection_seconds"] = round(time.monotonic() - started, 4)
    samples = {int(s.get("page") or 0): dict(s) for s in context.samples}
    for sample in native:
        samples.setdefault(sample["page"], dict(sample))
    return replace(
        context,
        samples=tuple(samples[n] for n in sorted(samples)),
        evidence_bundle={"trace": trace, "native": native, "ocr": ocr},
    )


def evidence_views(context):
    """Keep native and OCR alternatives; do not overwrite healthy native text."""
    bundle = context.evidence_bundle or {}
    native = {int(s["page"]): dict(s) for s in bundle.get("native", context.samples)}
    for sample in context.samples:
        native.setdefault(int(sample.get("page") or 0), dict(sample))
    views = [replace(context, samples=tuple(native[n] for n in sorted(native)))]
    if bundle.get("ocr"):
        # Inspect every acquired alternative. No exponential cross-product is
        # needed: primary and alternate readings each cover the same regions.
        for alternate in (False, True):
            merged = dict(native)
            changed = False
            for number in {s["page"] for s in bundle["ocr"]}:
                regions = [s for s in bundle["ocr"] if s["page"] == number]
                chosen = [s.get("alternate", s) if alternate else s for s in regions]
                changed |= any(a is not b for a, b in zip(chosen, regions))
                parts = [s["text"] for s in chosen]
                previous = native.get(number, {}).get("text", "")
                text = (
                    "\n".join(parts)
                    if text_quality(previous) != "usable_native"
                    or any(
                        s.get("reason") == "invisible_native_text_overlay"
                        for s in chosen
                    )
                    else previous + "\n" + "\n".join(parts)
                )
                merged[number] = {
                    "page": number,
                    "text": text,
                    "extraction": "native_and_ocr_alternate"
                    if alternate
                    else "native_and_ocr",
                    "visible_byline_texts": native.get(number, {}).get(
                        "visible_byline_texts", []
                    ),
                    "direct_byline_rereads": [
                        line
                        for region in chosen
                        if region.get("reason") == "invisible_native_text_overlay"
                        for line in region.get("lines", [])
                        if re.match(
                            r"^(?:by|written by)\s+", line.get("text", ""), re.I
                        )
                    ],
                }
            if not alternate or changed:
                views.append(
                    replace(context, samples=tuple(merged[n] for n in sorted(merged)))
                )
    return views
