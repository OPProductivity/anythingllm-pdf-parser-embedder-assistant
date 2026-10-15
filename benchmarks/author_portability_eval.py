"""Reproducible native-only author experiments. No upload or GROBID imports."""

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path


def key(value):
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", value.casefold()))


def complete_matches(actual, expected):
    """Full reviewed credits, preserving person count; ignore punctuation/order."""

    def people(value):
        return sorted(
            "".join(
                c for c in unicodedata.normalize("NFKD", v.casefold()) if c.isalnum()
            )
            for v in re.split(r"\s*[,;]\s*|\s+and\s+|\s*&\s*", value, flags=re.I)
            if v.strip()
        )

    return people(actual) == people(expected)


def matches(actual, expected, policy):
    if key(actual) == key(expected):
        return True
    if not actual or not expected:
        return False
    if policy == "legacy172":
        if ";" not in expected:
            return key(actual) == key(expected.split()[-1])
        names = [key(v) for v in expected.split(";")]
        found = [key(v) for v in re.split(r"\s*;\s*|\s*,\s*(?=[A-ZÀ-ÖØ-Þ])", actual)]
        return (
            names == found or [key(v.split()[-1]) for v in expected.split(";")] == found
        )
    names = [key(v.strip().split()[-1]) for v in expected.split(";") if v.strip()]
    found = [
        key(v.strip().split()[-1])
        for v in re.split(r"\s*[,;]\s*|\s+and\s+|\s*&\s*", actual)
        if v.strip()
    ]
    return sorted(names) == sorted(found)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--population", default="all")
    parser.add_argument(
        "--ablation",
        choices=["none", "no_ocr", "no_new_credit_rules", "no_portable_layout"],
        default="none",
    )
    args = parser.parse_args()
    sys.path.insert(0, args.repo)
    from auto_anythingllm_pipeline import pdf_metadata
    from author_metadata.context import AuthorEvidenceContext
    from author_metadata.work_identity import resolve_work_identity
    from author_metadata.dispatcher import infer_author
    from author_metadata.identity import (
        resolve_author_from_metadata_and_inference,
        author_confidence,
    )
    import author_metadata.dispatcher as dispatcher

    if args.ablation == "no_ocr":
        portable = sys.modules.get("author_metadata.portable_layout")
        if portable is not None:
            portable._collapsed_byline = lambda page, line: []
        dispatcher.infer_bounded_ocr = lambda context: {
            "author": "",
            "source": "not_found",
            "page": 0,
            "evidence": "",
        }
    elif args.ablation == "no_new_credit_rules":
        for name in [
            "infer_structured_credit",
            "credential_roster",
            "positioned_opening",
            "document_record",
            "publisher_titlepage",
            "biographic_titlepage",
            "work_rights_credit",
            "whole_book_editors",
            "continued_byline",
        ]:
            setattr(
                dispatcher,
                name,
                lambda context: {
                    "author": "",
                    "source": "not_found",
                    "page": 0,
                    "evidence": "",
                },
            )
        dispatcher.remove_translator_role = lambda report, context: report
    if args.ablation in {"no_new_credit_rules", "no_portable_layout"}:

        def empty(*args, **kwargs):
            return {"author": "", "source": "not_found", "page": 0, "evidence": ""}

        dispatcher.infer_portable_layout = empty
        bounded = sys.modules.get("author_metadata.bounded_ocr")
        if bounded is not None:
            bounded.layout_credit = empty
            bounded.publisher_credit = empty
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    rows = []
    cache = {}
    started = time.perf_counter()
    for case in manifest["cases"]:
        if args.population != "all" and case["population"] != args.population:
            continue
        path = Path(case["path"])
        cache_key = (case["sha256"], str(path))
        if cache_key not in cache:
            begin = time.perf_counter()
            try:
                if hashlib.sha256(path.read_bytes()).hexdigest() != case["sha256"]:
                    raise ValueError("input hash changed")
                meta = pdf_metadata(path, include_author_samples=True)
                samples = meta.pop("_author_text_samples", [])
                if meta.pop("_author_sample_error", ""):
                    raise ValueError("sample failed")
                identity = resolve_work_identity(meta.get("title") or "", path, samples)
                context = AuthorEvidenceContext.from_samples(
                    samples,
                    path=path,
                    title_hint=identity.title,
                    profile=identity.profile,
                )
                report = infer_author(context)
                resolved = resolve_author_from_metadata_and_inference(
                    meta.get("author") or "", report
                )
                cache[cache_key] = {
                    **resolved,
                    "inference": report,
                    "genre": identity.profile.kind,
                    "title": identity.title,
                    "title_source": identity.title_source,
                    "confidence": author_confidence(resolved),
                    "sample_pages": [s["page"] for s in samples],
                    "seconds": round(time.perf_counter() - begin, 4),
                }
            except Exception as exc:
                cache[cache_key] = {
                    "author": "",
                    "source": "error",
                    "error": str(exc),
                    "seconds": round(time.perf_counter() - begin, 4),
                }
        row = {**case, **cache[cache_key]}
        row["match"] = (
            matches(
                row["author"],
                case["expected_author"],
                case.get("policy", "surname_list"),
            )
            if case.get("scored", True)
            else None
        )
        row["complete_match"] = (
            complete_matches(row["author"], case["expected_author"])
            if case.get("scored", True)
            else None
        )
        rows.append(row)
    summary = {}
    for population in sorted({r["population"] for r in rows}):
        subset = [
            r for r in rows if r["population"] == population and r["match"] is not None
        ]
        summary[population] = {
            "correct": sum(r["match"] for r in subset),
            "total": len(subset),
            "complete_correct": sum(r["complete_match"] for r in subset),
            "unknown": sum(not r["author"] for r in subset),
            "wrong_nonempty": sum(bool(r["author"]) and not r["match"] for r in subset),
            "errors": sum("error" in r for r in subset),
            "seconds": round(sum(r["seconds"] for r in subset), 3),
        }
    result = {
        "mode": "native_sampled_metadata_no_grobid",
        "repo": args.repo,
        "ablation": args.ablation,
        "manifest_sha256": hashlib.sha256(Path(args.manifest).read_bytes()).hexdigest(),
        "summary": summary,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "rows": rows,
    }
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": args.output,
                "summary": summary,
                "elapsed_seconds": result["elapsed_seconds"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
