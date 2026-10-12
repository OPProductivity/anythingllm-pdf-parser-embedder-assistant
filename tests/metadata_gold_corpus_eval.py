"""Offline, hash-bound comparison of the production metadata preflight against manual PDF labels.

Run with: python tests/metadata_gold_corpus_eval.py --manifest PATH --output PATH
The manifest is local and is intentionally not included in the repository.
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
from author_metadata.identity import resolve_author_from_metadata_and_inference
from author_metadata.work_identity import resolve_work_identity


GOLD = Path(__file__).parent / "fixtures" / "manual_metadata_genre_172.jsonl"


def key(value):
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return re.sub(r"[^a-z0-9]", "", value)


def evaluate(manifest_path):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    gold = [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines()]
    if len(gold) != 172 or len({row["index"] for row in gold}) != 172:
        raise ValueError("Expected 172 unique manual labels")
    rows = []
    for expected in gold:
        index = expected["index"]
        source = Path(manifest[index]["path"])
        expected_digest = expected["sha256"].replace(":", "")
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected_digest:
            raise ValueError(f"Missing or changed PDF at manifest index {index}")
        metadata = pdf_metadata(source, include_author_samples=True)
        sample_error = metadata.pop("_author_sample_error", "")
        samples = metadata.pop("_author_text_samples", [])
        if sample_error:
            raise ValueError(f"Author sample failed at manifest index {index}: {sample_error}")
        identity = resolve_work_identity(metadata.get("title") or "", source, samples)
        context = AuthorEvidenceContext.from_samples(
            samples, path=source, title_hint=identity.title, profile=identity.profile,
        )
        inferred = infer_author(context)
        author = resolve_author_from_metadata_and_inference(
            metadata.get("author") or "", inferred,
        )["author"]
        rows.append({
            "index": index,
            "file": expected["file"],
            "expected_genre": expected["genre"],
            "genre": identity.profile.kind,
            "genre_match": identity.profile.kind == expected["genre"],
            "expected_author": expected["author"],
            "author": author,
            "author_match": key(author) == key(expected["author"]),
            "author_source": inferred.get("source"),
            "genre_cues": identity.profile.cues,
            "sample_pages": [row["page"] for row in samples],
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows = evaluate(args.manifest)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps({
        "total": len(rows),
        "genre_mismatches": sum(not row["genre_match"] for row in rows),
        "author_mismatches": sum(not row["author_match"] for row in rows),
        "empty_authors": sum(not row["author"] for row in rows),
        "output": str(output),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
