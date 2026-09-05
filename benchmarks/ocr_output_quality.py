"""Opt-in, local-only real-page checks through the production OCR region path.

No API calls, run history, timing calibration, baseline rewriting, or OCR cache.
Source PDFs, verified passages, and raw results belong in benchmarks/private.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

import fitz
import rag_pdf_tools as tools


def normalize(text):
    # Only presentation equivalence; never forgive misspelled or missing words.
    return re.sub(r"\s+", " ", str(text)).strip()


def text_hash(text):
    return hashlib.sha256(normalize(text).encode("utf8")).hexdigest()


def assess(case, text, regions):
    content = normalize(text)
    failures = []
    for passage in case.get("required_passages", []):
        if normalize(passage) not in content:
            failures.append({"kind": "missing_verified_passage", "passage": passage})
    for fragment in case.get("forbidden_fragments", []):
        if normalize(fragment) in content:
            failures.append({"kind": "forbidden_fragment", "fragment": fragment})
    cursor = 0
    for anchor in case.get("ordered_anchors", []):
        position = content.find(normalize(anchor), cursor)
        if position < 0:
            failures.append({"kind": "missing_or_reordered_anchor", "anchor": anchor})
        else:
            cursor = position + len(normalize(anchor))
    if not case.get("required_passages") or not case.get("review_note"):
        failures.append({"kind": "human_reference_not_established"})
    if case.get("region_count") != len(regions):
        failures.append({"kind": "region_count_changed", "actual": len(regions)})
    expected = case.get("baseline_text_sha256")
    if not expected:
        failures.append({"kind": "baseline_not_established"})
    elif expected != text_hash(content):
        failures.append({"kind": "output_changed_requires_review"})
    return failures


def run_pack(manifest_path, tesseract):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf8"))
    cases = manifest.get("cases", [])
    if manifest.get("schema_version") != 1 or not cases:
        raise ValueError("A nonempty schema_version=1 fixture manifest is required")
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Duplicate fixture IDs")
    results, fingerprints = [], {}
    for case in cases:
        started = time.perf_counter()
        result = {
            "id": case["id"],
            "review_note": case.get("review_note"),
            "failures": [],
        }
        try:
            source = (manifest_path.parent / case["source"]).resolve()
            if source not in fingerprints:
                with source.open("rb") as stream:
                    fingerprints[source] = hashlib.file_digest(
                        stream, "sha256"
                    ).hexdigest()
            result["source_sha256"] = fingerprints[source]
            if case.get("source_sha256") != fingerprints[source]:
                raise ValueError(
                    "Source fingerprint mismatch or unset; explicit fixture review required"
                )
            with fitz.open(source) as doc:
                page = int(case["page"])
                if not 1 <= page <= len(doc):
                    raise ValueError("Fixture page outside source PDF")
                regions = tools.photographed_page_ocr_regions(
                    doc[page - 1],
                    {"tesseract_executable": str(tesseract)},
                    page_number=page,
                )
            text = "\n\n".join(r["text"] for r in regions)
            result.update(text=text, text_sha256=text_hash(text), regions=regions)
            result["failures"] = assess(case, text, regions)
        except Exception as exc:
            result["failures"].append(
                {
                    "kind": "fixture_execution_failed",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            )
        result["seconds"] = round(time.perf_counter() - started, 3)
        result["passed"] = not result["failures"]
        results.append(result)
    return {
        "schema_version": 1,
        "pack_id": manifest.get("pack_id"),
        "scope": "production_page_ocr_not_embedding_or_full_pipeline",
        "baseline_meaning": "change detection, not proof that every baseline word is correct",
        "passed": all(r["passed"] for r in results),
        "cases": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Private JSON result; not run history",
    )
    parser.add_argument("--tesseract", default=shutil.which("tesseract"))
    args = parser.parse_args()
    if not args.tesseract or not Path(args.tesseract).is_file():
        parser.error("An installed Tesseract executable is required")
    if args.output.resolve() == args.manifest.resolve():
        parser.error("Output must not overwrite the fixture manifest")
    manifest = json.loads(args.manifest.read_text(encoding="utf8"))
    sources = {
        (args.manifest.parent / c["source"]).resolve()
        for c in manifest.get("cases", [])
    }
    if args.output.resolve() in sources:
        parser.error("Output must not overwrite a source PDF")
    report = run_pack(args.manifest, args.tesseract)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf8"
    )
    for case in report["cases"]:
        print(
            case["id"],
            "PASS" if case["passed"] else "REVIEW",
            case["seconds"],
            [f["kind"] for f in case["failures"]],
        )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
