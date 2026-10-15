"""Offline, read-only shadow evaluation of reversible metadata field proposals.

This reads reviewed author labels and local PDFs. It does not run preparation,
upload, embeddings, GROBID, or change any selected metadata.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import time

import fitz

from author_metadata.field_map import render_field_overlay, review_author_field_map
from rag_pdf_tools import ensure_tesseract_runtime


def run(review_path, sources_path, *, limit=0, max_ocr_calls=3,
        overlay_dir=None, overlay_ids=()):
    review = json.loads(Path(review_path).read_text(encoding="utf-8"))
    sources = json.loads(Path(sources_path).read_text(encoding="utf-8"))
    pdf_paths = {}
    for row in sources:
        uri = str(row.get("document_source") or "")
        paths = row.get("paths") or []
        if uri.startswith("local-pdf://sha256/") and paths:
            pdf_paths[uri.rsplit("/", 1)[-1]] = Path(paths[0])
    selected = [row for row in review.get("rows") or []
                if row.get("scored") and row.get("pdf_sha256") in pdf_paths
                and pdf_paths[row["pdf_sha256"]].is_file()]
    if limit:
        selected = selected[:limit]
    if not selected:
        raise ValueError("no reviewed local PDFs matched the review and sources inputs")
    tesseract = ensure_tesseract_runtime().get("executable") or ""
    results = []
    for row in selected:
        pdf_path = pdf_paths[row["pdf_sha256"]]
        started = time.perf_counter()
        try:
            with fitz.open(pdf_path) as document:
                embedded_title = str((document.metadata or {}).get("title") or "")
            evidence = review_author_field_map(
                pdf_path,
                title_hint=embedded_title or pdf_path.stem,
                author=row.get("after_author") or "",
                author_source=row.get("after_author_source") or "",
                tesseract=tesseract,
                max_ocr_calls=max_ocr_calls,
            )
            result = {
                "id": row["id"], "genre": row.get("after_genre"),
                "reviewed_author": row.get("expected_author"),
                "current_author": row.get("after_author"),
                "current_author_matches_review": bool(row.get("after_match")),
                "field_review": evidence["final_review"],
                "mapped_pages": [page["page"] for page in evidence["pages"]],
                "ocr_checks": [{"page": check.get("page"),
                                "composition": check.get("composition"),
                                "status": check.get("status"),
                                "reason": check.get("reason"),
                                "credit_candidates": check.get("ocr_credit_candidates") or []}
                               for check in evidence["checks"]],
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
            if overlay_dir is not None and row["id"] in set(overlay_ids):
                target_page = next((check.get("page") for check in evidence["checks"]
                                    if check.get("page")), evidence["pages"][0]["page"])
                page_map = next(page for page in evidence["pages"] if page["page"] == target_page)
                output_path = Path(overlay_dir) / f"{row['id']}-p{target_page}-field-overlay.png"
                render_field_overlay(pdf_path, page_map, output_path)
                result["overlay_path"] = str(output_path)
        except (OSError, ValueError, RuntimeError, fitz.FileDataError) as exc:
            result = {"id": row["id"], "error_type": type(exc).__name__,
                      "elapsed_seconds": round(time.perf_counter() - started, 3)}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    summary = {
        "sampled_local_pdfs": len(results),
        "review_verdicts": dict(Counter(row.get("field_review", "error") for row in results)),
        "ocr_checks": sum(len(row.get("ocr_checks") or []) for row in results),
        "elapsed_seconds": round(sum(row["elapsed_seconds"] for row in results), 3),
        "metadata_outputs_changed": 0,
        "coverage": "field mapping and targeted OCR only; no full preparation or author promotion",
    }
    return {"summary": summary, "rows": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-ocr-calls", type=int, default=3)
    parser.add_argument("--overlay-dir", type=Path)
    parser.add_argument("--overlay-id", action="append", default=[])
    args = parser.parse_args()
    result = run(args.review, args.sources, limit=args.limit,
                 max_ocr_calls=args.max_ocr_calls,
                 overlay_dir=args.overlay_dir, overlay_ids=args.overlay_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SUMMARY", json.dumps(result["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
