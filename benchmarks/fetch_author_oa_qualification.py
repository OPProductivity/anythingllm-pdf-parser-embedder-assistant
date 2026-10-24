"""Fetch the frozen public author-qualification cases and neutral-name copies.

Author labels go into evaluation manifests only, never into PDF metadata or
inference arguments. This does not upload or embed PDFs.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--split", choices=["development", "holdout", "all"], default="all"
    )
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    source = (
        Path(__file__).parent
        / "results"
        / "author-oa-qualification-20261015-cases.json"
    )
    cases = json.loads(source.read_text(encoding="utf-8"))["cases"]
    cases = [r for r in cases if args.split == "all" or r["split"] == args.split]
    if args.limit is not None:
        if args.limit < 1:
            parser.error("--limit must be positive")
        cases = cases[: args.limit]
    root = args.output_dir.resolve()
    originals, neutral = root / "original", root / "neutral"
    originals.mkdir(parents=True, exist_ok=True)
    neutral.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, case in enumerate(cases, 1):
        original = originals / (case["id"] + "-" + Path(case["original_filename"]).name)
        if not original.exists():
            request = urllib.request.Request(
                case["pdf_url"], headers={"User-Agent": "AuthorQualification/1.0"}
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                content = response.read(64 * 1024 * 1024 + 1)
            if not content.startswith(b"%PDF") or len(content) > 64 * 1024 * 1024:
                raise ValueError("Not a supported PDF response for " + case["id"])
            original.write_bytes(content)
        content = original.read_bytes()
        if not content.startswith(b"%PDF"):
            raise ValueError("Existing input is not a PDF: " + str(original))
        copy = neutral / f"document-{index:04d}.pdf"
        if copy.exists() and copy.read_bytes() != content:
            raise ValueError(
                "Refusing to replace a different neutral input: " + str(copy)
            )
        shutil.copyfile(original, copy)
        digest = hashlib.sha256(content).hexdigest()
        for view, path in [("original", original), ("neutral", copy)]:
            rows.append(
                {
                    "id": case["id"],
                    "population": case["split"] + "_" + view,
                    "path": str(path),
                    "sha256": digest,
                    "scored": True,
                    "expected_author": case["expected_author"],
                    "policy": "complete_names",
                    "ground_truth": {
                        "publisher_url": case["url"],
                        "doi": case["doi"],
                        "method": "Publisher author metadata and visually checked printed credit; frozen before inference",
                    },
                }
            )
        print(case["id"], flush=True)
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps({"cases": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps({"pdfs": len(cases), "views": len(rows), "manifest": str(manifest)})
    )


if __name__ == "__main__":
    main()
