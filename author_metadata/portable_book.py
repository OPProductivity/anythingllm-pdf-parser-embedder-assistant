"""Corroborate whole-book credits against a physical title, not an export name."""

from dataclasses import replace
import re

from .book import infer as infer_book
from .portable_layout import _native_page, publisher_credit
from .structured_credit import _words
from .title_evidence import not_found


def infer(context):
    if context.path is None or context.profile.kind != "book":
        return not_found()
    import fitz

    try:
        with fitz.open(context.path) as doc:
            # Parent-volume covers attached to short chapter excerpts are not
            # whole-book authorship. Keep their existing scope parser in charge.
            if len(doc) < 80:
                return not_found()
            for index in range(min(4, len(doc))):
                lines = sorted(
                    _native_page(doc[index]), key=lambda v: (v["box"][1], v["box"][0])
                )
                if not lines or sum(len(v["text"].split()) for v in lines) > 100:
                    continue
                if re.search(
                    r"\b(?:praise|acclaim|series editor|contents|other books|also by)\b",
                    " ".join(v["text"] for v in lines),
                    re.I,
                ):
                    continue
                eligible = [
                    v
                    for v in lines
                    if len(_words(v["text"])) >= 2
                    and v["box"][3] < doc[index].rect.height * 0.6
                ]
                largest = max((v["size"] for v in eligible), default=0)
                cluster = []
                for line in eligible:
                    if line["size"] < largest * 0.85:
                        if cluster:
                            break
                        continue
                    if cluster and line["box"][1] - cluster[-1]["box"][3] > 35:
                        break
                    cluster.append(line)
                title = " ".join(v["text"] for v in cluster)
                if not 3 <= len(_words(title)) <= 30 or re.search(
                    r"\b(?:edited by|written by|copyright|translated by)\b", title, re.I
                ):
                    continue
                report = infer_book(replace(context, title_hint=title))
                if report.get("author") and not str(
                    report.get("source", "")
                ).startswith("filename_"):
                    return {
                        **report,
                        "physical_title": title,
                        "title_evidence_page": index + 1,
                    }
                report = publisher_credit(lines, page=index + 1)
                if report.get("author"):
                    return report
    except OSError, ValueError, RuntimeError:
        pass
    return not_found()
