"""Find GROBID author candidates whose first-page occurrence is in an editor/reviewer section."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import unicodedata

import fitz


ROLE = re.compile(r"(?im)^\s*(?:edited(?:\s+and\s+reviewed)?\s+by|reviewed\s+by)\s*$")
TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)


def _fold(text):
    return "".join(char for char in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(char))


def name_positions(text, name):
    indexed = [(match.group(), match.start()) for match in TOKEN.finditer(_fold(text))]
    wanted = [match.group() for match in TOKEN.finditer(_fold(name))]
    if not wanted:
        return []
    return [indexed[index][1] for index in range(len(indexed) - len(wanted) + 1)
            if [word for word, _ in indexed[index:index + len(wanted)]] == wanted]


def audit(path, names):
    with fitz.open(path) as document:
        pages = [document[index].get_text("text") for index in range(min(3, len(document)))]
    results = []
    for page_number, text in enumerate(pages, 1):
        barrier = ROLE.search(text)
        if not barrier:
            continue
        positions = {name: name_positions(text, name) for name in names}
        before = [name for name, spots in positions.items() if any(pos < barrier.start() for pos in spots)]
        after_only = [name for name, spots in positions.items()
                      if spots and not any(pos < barrier.start() for pos in spots)]
        results.append({"page": page_number, "marker": barrier.group().strip(),
                        "before": before, "after_only": after_only,
                        "unlocated": [name for name, spots in positions.items() if not spots]})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    args = parser.parse_args()
    paths = {row["sha256"]: row["path"] for row in json.loads(args.cases.read_text(encoding="utf-8"))["cases"]}
    for line in args.results.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        names = row.get("grobid_authors") or []
        if row.get("grobid_status") != "ok" or not names:
            continue
        matches = audit(paths[row["sha256"]], names)
        if matches:
            print(json.dumps({"file": row["file"], "authors": names,
                              "role_sections": matches}, ensure_ascii=False))


if __name__ == "__main__":
    main()
