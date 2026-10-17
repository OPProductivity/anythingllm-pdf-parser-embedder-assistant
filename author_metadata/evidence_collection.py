"""Acquire a fixed evidence plan before any author verdict.

Collection is independent of author confidence and genre. Failed reads stay in
an explicit completeness ledger. Cached evidence is content-based, not a label.
"""

from dataclasses import replace
from collections import OrderedDict
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import re
import shutil
import subprocess
import time

_CACHE = OrderedDict()


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


def _image_regions(page):
    """Image areas without a usable native text layer need their own reading."""
    import fitz

    words = page.get_text("words")
    regions = []
    area = max(1, page.rect.get_area())
    for info in page.get_image_info():
        rect = fitz.Rect(info["bbox"]) & page.rect
        if rect.is_empty or rect.get_area() / area < 0.04:
            continue
        overlay = " ".join(
            str(w[4]) for w in words if fitz.Rect(w[:4]).intersects(rect)
        )
        if text_quality(overlay) != "usable_native":
            if not any(
                (rect & old).get_area() >= rect.get_area() * 0.9 for old in regions
            ):
                regions.append(rect)
    return regions


def _plan(document):
    """Five opening pages; front matter and editorial boundaries can extend it."""
    pages = set(range(1, min(5, len(document)) + 1))
    opening = "\n".join(
        document[n - 1].get_text("text", sort=True) for n in sorted(pages)
    )
    extensions = []
    # This describes a publishing sequence, not whether an author was found.
    frontmatter = bool(
        re.search(
            r"(?im)^\s*(?:contents|table of contents|praise(?: for.*?)?|endorsements|a series edited by.*?)\s*$",
            opening,
        )
    )
    if frontmatter:
        later = list(range(6, min(10, len(document)) + 1))
        pages.update(later)
        if later:
            extensions.append({"reason": "front_matter_sequence", "pages": later})
    editorial = bool(re.search(r"(?i)\b(?:guest\s*)?editorial\b", opening[:2000]))
    if editorial and len(document) <= 24:
        later = list(range(6, len(document) + 1))
        pages.update(later)
        if later:
            extensions.append(
                {"reason": "short_editorial_signature_boundary", "pages": later}
            )
    return sorted(pages), extensions


def collect(context):
    """Return all acquired native/OCR evidence and a complete attempt ledger."""
    if context.evidence_bundle is not None:
        return context
    trace = {
        "version": 1,
        "native_plan": [],
        "ocr_plan": [],
        "attempts": [],
        "extensions": [],
        "complete": True,
        "scope": "planned pages and image regions, not entire book",
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

    path = Path(context.path)
    started = time.monotonic()
    try:
        with path.open("rb") as stream:
            key = hashlib.file_digest(stream, "sha256").hexdigest()
        if key in _CACHE:
            _CACHE.move_to_end(key)
            bundle = deepcopy(_CACHE[key])
            bundle["trace"]["cache_hit"] = True
        else:
            native, ocr = [], []
            executable = (
                shutil.which("tesseract")
                or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
            )
            with fitz.open(path) as document:
                trace["document_page_count"] = len(document)
                pages, trace["extensions"] = _plan(document)
                trace["native_plan"] = pages
                tasks = []
                for number in pages:
                    page = document[number - 1]
                    try:
                        text = page.get_text("text", sort=True)
                        from .native_article import positioned_lines

                        native.append(
                            {
                                "page": number,
                                "text": text,
                                "horizontal_lines": positioned_lines(page),
                                "width": page.rect.width,
                                "height": page.rect.height,
                            }
                        )
                        trace["attempts"].append(
                            {"kind": "native", "page": number, "status": "read"}
                        )
                        reason = text_quality(text)
                        regions = (
                            [page.rect]
                            if reason != "usable_native"
                            else _image_regions(page)
                        )
                        if reason == "usable_native":
                            reason = "image_without_native_overlay"
                        for region in regions:
                            # Facing leaves are separate OCR tasks. Neither an
                            # early name nor an empty first leaf ends the plan.
                            if region.width > region.height * 1.15:
                                midpoint = (region.x0 + region.x1) / 2
                                leaves = [
                                    fitz.Rect(
                                        region.x0, region.y0, midpoint, region.y1
                                    ),
                                    fitz.Rect(
                                        midpoint, region.y0, region.x1, region.y1
                                    ),
                                ]
                            else:
                                leaves = [region]
                            for leaf in leaves:
                                tasks.append(
                                    {
                                        "page": number,
                                        "clip": list(leaf),
                                        "reason": reason,
                                    }
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
                trace["ocr_plan"] = list(tasks)
                from .portable_layout import read_tsv

                for task in tasks:
                    attempt = {"kind": "ocr", **task}
                    try:
                        if not Path(executable).is_file():
                            raise OSError("Tesseract unavailable")
                        page = document[task["page"] - 1]
                        region = fitz.Rect(task["clip"])
                        scale = min(
                            2.5, math.sqrt(4_000_000 / max(1, region.get_area()))
                        )
                        png = page.get_pixmap(
                            matrix=fitz.Matrix(scale, scale), clip=region
                        ).tobytes("png")
                        result = subprocess.run(
                            [
                                executable,
                                "stdin",
                                "stdout",
                                "-l",
                                "eng",
                                "--psm",
                                "3",
                                "tsv",
                            ],
                            input=png,
                            capture_output=True,
                            timeout=6,
                            check=True,
                        )
                        text, lines = read_tsv(result.stdout)
                        ocr.append(
                            {
                                **task,
                                "text": text,
                                "lines": lines,
                                "scale": scale,
                                "width": region.width * scale,
                                "height": region.height * scale,
                                "extraction": "tesseract",
                                "coordinate_space": "crop_pixels",
                            }
                        )
                        attempt.update(status="read", characters=len(text))
                        # Ambiguous merged initials justify an additional
                        # reading, independently of whether a name was accepted.
                        if re.search(r"\b[A-Z]{2,3}\.\s+[A-Z][a-z]+", text):
                            alternate = {
                                **task,
                                "reason": "ambiguous_initials_recheck",
                                "scale": 2.0,
                            }
                            trace["ocr_plan"].append(alternate)
                            try:
                                png2 = page.get_pixmap(
                                    matrix=fitz.Matrix(2, 2), clip=region
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
                                        "tsv",
                                    ],
                                    input=png2,
                                    capture_output=True,
                                    timeout=6,
                                    check=True,
                                )
                                text2, lines2 = read_tsv(second.stdout)
                                ocr[-1]["alternate"] = {
                                    "text": text2,
                                    "lines": lines2,
                                    "width": region.width * 2,
                                    "height": region.height * 2,
                                }
                                trace["attempts"].append(
                                    {
                                        "kind": "ocr",
                                        **alternate,
                                        "status": "read",
                                        "characters": len(text2),
                                    }
                                )
                            except (
                                OSError,
                                ValueError,
                                RuntimeError,
                                subprocess.SubprocessError,
                            ) as error:
                                trace["complete"] = False
                                trace["attempts"].append(
                                    {
                                        "kind": "ocr",
                                        **alternate,
                                        "status": "failed",
                                        "error": str(error),
                                    }
                                )
                    except (
                        OSError,
                        ValueError,
                        RuntimeError,
                        subprocess.SubprocessError,
                    ) as error:
                        trace["complete"] = False
                        attempt.update(status="failed", error=str(error))
                    trace["attempts"].append(attempt)
            bundle = {"trace": trace, "native": native, "ocr": ocr}
            _CACHE[key] = deepcopy(bundle)
            while len(_CACHE) > 512:
                _CACHE.popitem(last=False)
        # Keep caller sampling/drawing order for compatibility; expose sorted
        # physical order alongside it to every publishing-role specialist.
        samples = {int(s.get("page") or 0): dict(s) for s in context.samples}
        for sample in bundle["native"]:
            samples.setdefault(sample["page"], dict(sample))
        bundle["trace"]["collection_seconds"] = round(time.monotonic() - started, 4)
        return replace(
            context,
            samples=tuple(samples[n] for n in sorted(samples)),
            evidence_bundle=bundle,
        )
    except (OSError, ValueError, RuntimeError) as error:
        trace.update(complete=False, error=str(error))
        return replace(
            context,
            evidence_bundle={
                "trace": trace,
                "native": list(context.samples),
                "ocr": [],
            },
        )


def evidence_views(context):
    """Keep native and OCR alternatives; do not overwrite healthy native text."""
    bundle = context.evidence_bundle or {}
    native = {int(s["page"]): dict(s) for s in bundle.get("native", context.samples)}
    for sample in context.samples:
        native.setdefault(int(sample.get("page") or 0), dict(sample))
    views = [replace(context, samples=tuple(native[n] for n in sorted(native)))]
    if bundle.get("ocr"):
        merged = dict(native)
        for number in {s["page"] for s in bundle["ocr"]}:
            parts = [s["text"] for s in bundle["ocr"] if s["page"] == number]
            previous = native.get(number, {}).get("text", "")
            # An image region can coexist with native text. Both are evidence.
            text = (
                "\n".join(parts)
                if text_quality(previous) != "usable_native"
                else previous + "\n" + "\n".join(parts)
            )
            merged[number] = {
                "page": number,
                "text": text,
                "extraction": "native_and_ocr",
            }
        views.append(replace(context, samples=tuple(merged[n] for n in sorted(merged))))
    return views
