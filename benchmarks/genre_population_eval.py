"""Hash-bound, paired metadata preflight for the moved genre corpora.

Build one case catalog, then run it against separate parser checkouts. This
exercises metadata sampling, work identity, genre strategy, and final
metadata-author resolution. It does not prepare
segments, run the shadow field map, upload, or embed PDFs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_cases(repo_root, archive_root, gold_manifest_path):
    repo_root = Path(repo_root)
    archive_root = Path(archive_root)
    gold_manifest = _read(gold_manifest_path)
    gold = [json.loads(line) for line in (
        repo_root / "tests/fixtures/manual_metadata_genre_172.jsonl"
    ).read_text(encoding="utf-8").splitlines()]
    entries = {}
    missing = []

    def add(path, expected_digest, corpus, label=None):
        path = Path(path)
        if not path.is_file():
            missing.append({"corpus": corpus, "file": path.name, "reason": "missing"})
            return
        actual = _digest(path)
        if actual != expected_digest.lower():
            raise ValueError(f"PDF hash differs: {path}")
        row = entries.setdefault(actual, {"sha256": actual, "path": str(path),
                                          "corpora": [], "labels": [], "aliases": []})
        if corpus not in row["corpora"]:
            row["corpora"].append(corpus)
        if str(path) not in row["aliases"]:
            row["aliases"].append(str(path))
        if label is not None and label not in row["labels"]:
            row["labels"].append(label)

    for label in gold:
        index = label["index"]
        source = gold_manifest[index] if index < len(gold_manifest) else None
        path = Path(source["path"]) if source and source.get("path") else None
        if path is None:
            missing.append({"corpus": "manual_172", "file": label["file"],
                            "reason": "hash_bound_source_not_found"})
            continue
        add(path, label["sha256"].replace(":", ""), "manual_172",
            {"source": "manual_172", "id": index, "file": label["file"],
             "group": label["group"], "expected_author": label["author"],
             "expected_genre": label["genre"]})

    archive_manifest = _read(archive_root / "manifest.json")
    archive_pdfs = {}
    for corpus in archive_manifest["corpora"]:
        corpus_name = corpus["name"]
        for item in corpus["files"]:
            if not item["path"].casefold().endswith(".pdf"):
                continue
            path = archive_root / "raw" / Path(item["path"])
            add(path, item["sha256"], corpus_name)
            archive_pdfs.setdefault(path.name.casefold(), []).append(path)

    public_root = archive_root / "raw/_anythingllm-public-metadata-corpus-20261005"  # pragma: allowlist secret
    for label in _read(public_root / "labels.json")["items"]:
        path = public_root / label["file"]
        add(path, label["sha256"], "public_27",
            {"source": "public_27", "id": label["file"],
             "file": label["file"], "group": label["category"],
             "creator_role": label.get("role"),
             "expected_author": label.get("creator") or "",
             "expected_genre": ""})

    corporate_root = archive_root / "raw/_anythingllm-corporate-genre-20261005"
    for label in _read(corporate_root / "genre-v2-final-results.json"):
        possibilities = archive_pdfs.get(label["file"].casefold(), [])
        if len(possibilities) != 1:
            missing.append({"corpus": "corporate_43", "file": label["file"],
                            "reason": f"ambiguous_or_missing_{len(possibilities)}"})
            continue
        path = possibilities[0]
        digest = _digest(path)
        row = entries[digest]
        if "corporate_43" not in row["corpora"]:
            row["corpora"].append("corporate_43")
        row["labels"].append({"source": "corporate_43", "id": label["file"],
                              "file": label["file"], "group": label.get("gold"),
                              "split": label.get("split"),
                              # The six "other" controls only tested corporate
                              # routing. Their empty author slot was not an
                              # independently reviewed author gold label.
                              "expected_author": (None if label.get("gold") == "other"
                                                  else label.get("expected_author")),
                              "expected_genre": "",
                              "historical_baseline_author": (label.get("baseline") or {}).get("author")})

    thesis_root = archive_root / "raw/_anythingllm-thesis-qualification-20261005"
    for label in _read(thesis_root / "first3-metadata-results.json"):
        possibilities = archive_pdfs.get(label["file"].casefold(), [])
        if len(possibilities) == 1:
            row = entries[_digest(possibilities[0])]
            row["labels"].append({"source": "thesis_first3", "id": label["file"],
                                  "file": label["file"], "group": "thesis",
                                  "expected_author": label.get("expected_author"),
                                  "expected_genre": "thesis_dissertation"})
    return {"schema": "genre-population-cases-v1", "cases": sorted(entries.values(), key=lambda row: row["sha256"]),
            "missing": missing, "source_counts": {
                "manual_gold_labels": len(gold), "archive_physical_pdfs": len(archive_pdfs),
                "public_labels": 27, "corporate_result_rows": 43,
            }}


def evaluate(case_path, output_path, code_root):
    sys.path.insert(0, str(Path(code_root).resolve()))
    from auto_anythingllm_pipeline import pdf_metadata
    from author_metadata.context import AuthorEvidenceContext
    from author_metadata.dispatcher import infer_author
    from author_metadata.identity import resolve_author_from_metadata_and_inference
    from author_metadata.work_identity import resolve_work_identity

    cases = _read(case_path)["cases"]
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    done = {}
    if output_path.is_file():
        for line in output_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["sha256"]] = row
    with output_path.open("a", encoding="utf-8") as out:
        for ordinal, case in enumerate(cases, 1):
            if case["sha256"] in done:
                continue
            path = Path(case["path"])
            start = time.perf_counter()
            row = {"sha256": case["sha256"], "file": path.name,
                   "code_root": str(Path(code_root).resolve())}
            try:
                metadata = pdf_metadata(path, include_author_samples=True)
                sample_error = metadata.pop("_author_sample_error", "")
                samples = metadata.pop("_author_text_samples", [])
                if sample_error:
                    raise RuntimeError(f"author_sample_error: {sample_error}")
                identity = resolve_work_identity(metadata.get("title") or "", path, samples)
                context = AuthorEvidenceContext.from_samples(
                    samples, path=path, title_hint=identity.title, profile=identity.profile)
                inferred = infer_author(context)
                row["native_author"] = inferred.get("author") or ""
                row["native_author_source"] = inferred.get("source") or ""
                resolved = resolve_author_from_metadata_and_inference(
                    metadata.get("author") or "", inferred)
                row.update({"author": resolved["author"], "author_source": resolved["source"],
                            "genre": identity.profile.kind,
                            "work_scope": identity.profile.work_scope,
                            "sample_pages": [item.get("page") for item in samples]})
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            row["seconds"] = round(time.perf_counter() - start, 3)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            print(json.dumps({"done": ordinal, "total": len(cases),
                              "file": path.name, "genre": row.get("genre"),
                              "author": row.get("author"), "error": row.get("error"),
                              "seconds": row["seconds"]}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_subparsers(dest="action", required=True)
    build = action.add_parser("build")
    build.add_argument("--repo-root", required=True, type=Path)
    build.add_argument("--archive-root", required=True, type=Path)
    build.add_argument("--gold-manifest", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    run = action.add_parser("evaluate")
    run.add_argument("--cases", required=True, type=Path)
    run.add_argument("--code-root", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "build":
        result = build_cases(args.repo_root, args.archive_root, args.gold_manifest)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"unique_pdfs": len(result["cases"]),
                          "missing": result["missing"],
                          "source_counts": result["source_counts"]}, ensure_ascii=False))
    else:
        evaluate(args.cases, args.output, args.code_root)


if __name__ == "__main__":
    main()
