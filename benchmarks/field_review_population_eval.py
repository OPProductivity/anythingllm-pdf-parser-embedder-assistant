"""Evaluate shadow field/OCR review on each PDF in a paired author run.

This only measures the diagnostic review. It does not promote its candidates
or change the parser's selected author metadata.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from auto_anythingllm_pipeline import pdf_metadata
from author_metadata.field_map import review_author_field_map
from author_metadata.work_identity import resolve_work_identity
from rag_pdf_tools import ensure_tesseract_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--authors", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-ocr-calls", type=int, default=3)
    args = parser.parse_args()
    catalog = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    authors = {row["sha256"]: row for line in args.authors.read_text(encoding="utf-8").splitlines()
               if (row := json.loads(line))}
    tesseract = ensure_tesseract_runtime().get("executable") or ""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if args.output.is_file():
        done = {json.loads(line)["sha256"] for line in args.output.read_text(encoding="utf-8").splitlines()
                if line.strip()}
    with args.output.open("a", encoding="utf-8") as out:
        for ordinal, case in enumerate(catalog, 1):
            digest = case["sha256"]
            if digest in done:
                continue
            selected = authors.get(digest) or {}
            path = Path(case["path"])
            start = time.perf_counter()
            row = {"sha256": digest, "file": path.name,
                   "selected_author": selected.get("author") or "",
                   "selected_genre": selected.get("genre")}
            try:
                metadata = pdf_metadata(path, include_author_samples=True)
                samples = metadata.pop("_author_text_samples", [])
                identity = resolve_work_identity(metadata.get("title") or "", path, samples)
                review = review_author_field_map(
                    path, title_hint=identity.title,
                    author=row["selected_author"],
                    author_source=selected.get("author_source") or "",
                    tesseract=tesseract, max_ocr_calls=args.max_ocr_calls)
                row.update({"final_review": review["final_review"],
                            "mapped_pages": [page["page"] for page in review["pages"]],
                            "checks": [{"page": check.get("page"),
                                        "composition": check.get("composition"),
                                        "status": check.get("status"),
                                        "reason": check.get("reason"),
                                        "ocr_credit_candidates": check.get("ocr_credit_candidates")}
                                       for check in review["checks"]],
                            "reconsiderations": review["reconsiderations"]})
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            row["seconds"] = round(time.perf_counter() - start, 3)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            print(json.dumps({"done": ordinal, "total": len(catalog), "file": path.name,
                              "review": row.get("final_review"), "error": row.get("error"),
                              "seconds": row["seconds"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
