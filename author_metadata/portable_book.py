"""Corroborate whole-book credits against a physical title, not an export name."""

from dataclasses import replace
import re

from .book import infer as infer_book
from .portable_layout import _native_page, publisher_credit_reports
from .structured_credit import _words
from .title_evidence import not_found


def credit_reports(context):
    if (
        context.path is None
        or context.profile.kind != "book"
        or context.profile.work_scope == "chapter"
    ):
        return
    import fitz

    try:
        with fitz.open(context.path) as doc:
            # Length does not establish work boundaries. Inspect every
            # acquired opening title interpretation, with local chapter
            # responsibility still available to the final adjudicator.
            planned = {int(p["page"]) for p in context.planned_opening_pages}
            for index in (
                [n - 1 for n in sorted(planned)] if planned else range(min(5, len(doc)))
            ):
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
                    yield {
                        **report,
                        "physical_title": title,
                        "title_evidence_page": index + 1,
                    }
                yield from publisher_credit_reports(lines, page=index + 1)
    except OSError, ValueError, RuntimeError:
        pass
    return


def infer(context):
    reports = list(credit_reports(context))
    return reports[0] if reports else not_found()
