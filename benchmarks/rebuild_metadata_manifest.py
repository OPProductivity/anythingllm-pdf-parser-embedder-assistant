"""Locate the 172 hash-bound manual PDFs after source-folder moves.

Only exact SHA-256 matches are accepted. This writes paths for the existing
read-only metadata_gold_corpus_eval.py; it does not change the source PDFs.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path


GOLD = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "manual_metadata_genre_172.jsonl"


def rebuild(roots):
    gold = [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines()]
    by_name = defaultdict(list)
    for root in roots:
        for path in Path(root).rglob("*.pdf"):
            if path.is_file():
                by_name[path.name.casefold()].append(path)
    digest_cache = {}
    rows = [None] * (max(label["index"] for label in gold) + 1)
    missing = []
    for label in gold:
        expected = label["sha256"].replace(":", "").casefold()
        match = None
        for path in by_name[label["file"].casefold()]:
            digest = digest_cache.get(path)
            if digest is None:
                h = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        h.update(chunk)
                digest = digest_cache[path] = h.hexdigest()
            if digest == expected:
                match = path
                break
        if match is None:
            missing.append({"index": label["index"], "file": label["file"],
                            "candidates": len(by_name[label["file"].casefold()])})
        rows[label["index"]] = {"path": str(match) if match else "", "sha256": expected,
                                "file": label["file"], "group": label["group"]}
    return {"manifest": rows, "missing": missing,
            "indexed_pdf_paths": sum(map(len, by_name.values())),
            "labeled_pdfs": len(gold)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = rebuild(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result["manifest"], ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(json.dumps({"matched": result["labeled_pdfs"] - len(result["missing"]),
                      "expected": result["labeled_pdfs"],
                      "missing": result["missing"],
                      "indexed_pdf_paths": result["indexed_pdf_paths"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
