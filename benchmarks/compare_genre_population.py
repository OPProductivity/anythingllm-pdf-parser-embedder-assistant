"""Compare identical hash-bound PDF inputs across metadata parser versions."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import unicodedata


def _read_jsonl(path):
    return {row["sha256"]: row for line in Path(path).read_text(encoding="utf-8").splitlines()
            if (row := json.loads(line))}


def _key(value):
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(char for char in value if char.isalnum())


def _surname(value):
    words = re.findall(r"[^\W_]+(?:[-’'][^\W_]+)*", str(value or ""), re.UNICODE)
    while words and words[-1].casefold().strip(".") in {"jr", "sr", "ii", "iii"}:
        words.pop()
    return _key(words[-1]) if words else ""


def _match(predicted, expected):
    if expected is None:
        return None
    if _key(predicted) == _key(expected):
        return True
    if not predicted or not expected:
        return False
    expected_people = [part.strip() for part in expected.split(";") if part.strip()]
    predicted_people = [part.strip() for part in re.split(r";|,(?!\s*(?:Jr|Sr)\.?)", predicted)
                        if part.strip()]
    if len(expected_people) != len(predicted_people):
        return False
    # Prior user-approved scoring accepts surname-only credits. A surname
    # cannot excuse a different full given name; permit it only when each
    # predicted person is exactly one token (possibly a hyphenated surname).
    if any(len(re.findall(r"\S+", part)) != 1 for part in predicted_people):
        return False
    return all(_key(actual) == _surname(gold)
               for actual, gold in zip(predicted_people, expected_people))


def compare(cases_path, baseline_path, current_path, native_path=None):
    catalog = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    versions = {"baseline": _read_jsonl(baseline_path),
                "current": _read_jsonl(current_path)}
    if native_path:
        versions["current_native"] = _read_jsonl(native_path)
    records = []
    source_stats = defaultdict(lambda: {name: Counter() for name in versions})
    label_record_stats = defaultdict(lambda: {name: Counter() for name in versions})
    group_stats = defaultdict(lambda: {name: Counter() for name in versions})
    transitions = Counter()
    missing_results = []
    for case in catalog["cases"]:
        digest = case["sha256"]
        if any(digest not in rows for rows in versions.values()):
            missing_results.append(digest)
            continue
        predictions = {name: rows[digest] for name, rows in versions.items()}
        record = {"sha256": digest, "file": Path(case["path"]).name,
                  "corpora": case["corpora"], "labels": case["labels"],
                  "versions": {name: {"author": row.get("author"),
                                      "author_source": row.get("author_source"),
                                      "genre": row.get("genre"),
                                      "error": row.get("error"),
                                      "seconds": row.get("seconds")}
                               for name, row in predictions.items()}}
        scored = [label for label in case["labels"] if label.get("expected_author") is not None]
        for label in scored:
            for name, row in predictions.items():
                counts = label_record_stats[label["source"]][name]
                counts["scored"] += 1
                if row.get("error"):
                    counts["errors"] += 1
                elif _match(row.get("author") or "", label["expected_author"]):
                    counts["correct"] += 1
                if not row.get("author"):
                    counts["empty_outputs"] += 1
        preferred = next((label for source in ("manual_172", "public_27", "corporate_43", "thesis_first3")
                          for label in scored if label["source"] == source), None)
        if preferred:
            expected = preferred["expected_author"]
            record["primary_label"] = preferred
            record["label_conflicts"] = [label for label in scored
                                         if _key(label["expected_author"]) != _key(expected)]
            for name, row in predictions.items():
                match = None if row.get("error") else _match(row.get("author") or "", expected)
                record["versions"][name]["author_match"] = match
                source_stats[preferred["source"]][name]["scored"] += 1
                group = preferred.get("group") or preferred["source"]
                group_stats[group][name]["scored"] += 1
                if match is True:
                    source_stats[preferred["source"]][name]["correct"] += 1
                    group_stats[group][name]["correct"] += 1
                if row.get("error"):
                    source_stats[preferred["source"]][name]["errors"] += 1
                    group_stats[group][name]["errors"] += 1
                if not row.get("author"):
                    source_stats[preferred["source"]][name]["empty_outputs"] += 1
                    group_stats[group][name]["empty_outputs"] += 1
                if expected == "" and row.get("author"):
                    source_stats[preferred["source"]][name]["false_person_credit"] += 1
                    group_stats[group][name]["false_person_credit"] += 1
            old = record["versions"]["baseline"]["author_match"]
            new = record["versions"]["current"]["author_match"]
            if old is False and new is True:
                transitions["improved"] += 1
            elif old is True and new is False:
                transitions["regressed"] += 1
            elif old is True and new is True:
                transitions["stable_correct"] += 1
            elif old is False and new is False:
                transitions["stable_incorrect"] += 1
            else:
                transitions["error_or_unscored"] += 1
        records.append(record)
    return {"coverage": {"catalog_unique_pdfs": len(catalog["cases"]),
                         "evaluated_unique_pdfs": len(records),
                         "missing_result_hashes": missing_results,
                         "missing_input_pdfs": catalog["missing"]},
            "source_stats": {key: {version: dict(counts) for version, counts in values.items()}
                             for key, values in source_stats.items()},
            "label_record_stats": {key: {version: dict(counts) for version, counts in values.items()}
                                   for key, values in label_record_stats.items()},
            "group_stats": {key: {version: dict(counts) for version, counts in values.items()}
                            for key, values in group_stats.items()},
            "transitions": dict(transitions), "rows": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--current-native", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = compare(args.cases, args.baseline, args.current, args.current_native)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"coverage": result["coverage"],
                      "source_stats": result["source_stats"],
                      "label_record_stats": result["label_record_stats"],
                      "transitions": result["transitions"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
