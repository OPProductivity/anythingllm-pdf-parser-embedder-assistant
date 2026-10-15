"""Compare current and GROBID-assisted author labels against manual PDF labels.

Example: python tests/grobid_shadow_eval.py --pdf-dir PATH --output PATH
Only hash-matching PDFs from manual_metadata_genre_172.jsonl are assessed.
"""

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_anythingllm_pipeline import pdf_metadata
from author_metadata.context import AuthorEvidenceContext
from author_metadata.dispatcher import infer_author
from author_metadata.grobid import analyze, reconcile
from author_metadata.identity import resolve_author_from_metadata_and_inference
from author_metadata.work_identity import resolve_work_identity


GOLD = Path(__file__).parent / "fixtures" / "manual_metadata_genre_172.jsonl"


def key(value):
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return re.sub(r"[^a-z0-9]", "", value)


def evaluate(directory):
    labels = [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines()]
    by_hash = {row["sha256"].replace(":", ""): row for row in labels}
    rows = []
    for source in sorted(Path(directory).glob("*.pdf")):
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        expected = by_hash.get(digest)
        if expected is None:
            continue
        metadata = pdf_metadata(source, include_author_samples=True)
        sample_error = metadata.pop("_author_sample_error", "")
        if sample_error:
            raise ValueError(f"Author sampling failed: {source.name}: {sample_error}")
        samples = metadata.pop("_author_text_samples", [])
        identity = resolve_work_identity(metadata.get("title") or "", source, samples)
        context = AuthorEvidenceContext.from_samples(
            samples, path=source, title_hint=identity.title, profile=identity.profile,
        )
        baseline = infer_author(context)
        baseline_author = resolve_author_from_metadata_and_inference(
            metadata.get("author") or "", baseline,
        )["author"]
        grobid = analyze(source, context)
        assisted, decision = reconcile(context, baseline, grobid)
        assisted_author = resolve_author_from_metadata_and_inference(
            metadata.get("author") or "", assisted,
        )["author"]
        rows.append({
            "file": source.name, "genre": identity.profile.kind,
            "expected_genre": expected["genre"], "expected_author": expected["author"],
            "baseline_author": baseline_author, "assisted_author": assisted_author,
            "baseline_match": key(baseline_author) == key(expected["author"]),
            "assisted_match": key(assisted_author) == key(expected["author"]),
            "grobid_status": grobid.get("status"), "grobid_decision": decision,
            "grobid_author": ", ".join(a["name"] for a in grobid.get("authors", [])),
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = evaluate(args.pdf_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                           encoding="utf-8")
    print(json.dumps({
        "total": len(rows),
        "scholarly": sum(row["genre"] == "scholarly_article" for row in rows),
        "genre_mismatches": sum(row["genre"] != row["expected_genre"] for row in rows),
        "baseline_author_mismatches": sum(not row["baseline_match"] for row in rows),
        "assisted_author_mismatches": sum(not row["assisted_match"] for row in rows),
        "changed": sum(row["baseline_author"] != row["assisted_author"] for row in rows),
        "grobid_unavailable": sum(row["grobid_status"] == "unavailable" for row in rows),
        "output": str(args.output),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
