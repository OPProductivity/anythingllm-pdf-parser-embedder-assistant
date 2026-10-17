"""Read the complete physical byline band without affiliation or sidebar text."""

import re
import unicodedata

from .structured_credit import _names, _words
from .title_evidence import not_found

_CACHE = {}
_ROLE = re.compile(
    r"\b(?:editor|edited|translator|translated|reviewed|correspondence|contact|copyright|contents|references)\b",
    re.I,
)
_INSTITUTION = re.compile(
    r"\b(?:university|universit[eé]|department|institute|laboratory|laboratoire|school|college|centre|center|hospital|faculty)\b",
    re.I,
)


def positioned_lines(page):
    """Keep name letters at their own baseline; discard smaller footnote markers."""
    import fitz

    fragments = []
    for block in page.get_text(
        "dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
    )["blocks"]:
        for line in block.get("lines", []):
            if abs(line.get("dir", (1, 0))[0]) < 0.9:
                continue
            letters = [
                s for s in line["spans"] if sum(c.isalpha() for c in s["text"]) >= 2
            ]
            separators = [
                s for s in line["spans"] if re.fullmatch(r"\s*[,;]\s*", s["text"])
            ]
            if not letters and not separators:
                continue
            core = letters or separators
            size = max(s["size"] for s in core)
            baseline = max(core, key=lambda s: len(s["text"]))["origin"][1]
            spans = [
                s
                for s in line["spans"]
                if s["size"] >= size * 0.75 and not (s["flags"] & 1)
            ]
            text = "".join(s["text"] for s in spans)
            text = "".join(
                c for c in text if unicodedata.category(c) != "Cf" and c not in "*†‡§¶∗"
            )
            text = " ".join(text.split())
            if text:
                fragments.append(
                    {
                        "text": text,
                        "size": size,
                        "baseline": baseline,
                        "box": list(line["bbox"]),
                    }
                )
    return sorted(fragments, key=lambda v: (v["baseline"], v["box"][0]))


def _column_lines(fragments, left, right):
    groups = []
    for line in fragments:
        if line["box"][0] < left - 8 or line["box"][2] > right + 8:
            continue
        if (
            groups
            and abs(groups[-1]["baseline"] - line["baseline"]) < 2
            and abs(groups[-1]["size"] - line["size"]) < 1
        ):
            previous = groups[-1]
            previous["text"] += " " + line["text"]
            previous["box"] = [
                min(previous["box"][0], line["box"][0]),
                min(previous["box"][1], line["box"][1]),
                max(previous["box"][2], line["box"][2]),
                max(previous["box"][3], line["box"][3]),
            ]
        else:
            groups.append({**line, "box": list(line["box"])})
    return groups


def credit(lines, width, height):
    eligible = [
        v
        for v in lines
        if 3 <= len(_words(v["text"])) <= 20
        and v["box"][1] < height * 0.45
        and not _ROLE.search(v["text"])
    ]
    largest = max((v["size"] for v in eligible), default=0)
    headings = [v for v in eligible if v["size"] >= largest * 0.9]
    if not headings:
        return not_found()
    start = headings[0]
    cluster = []
    for v in lines[lines.index(start) :]:
        if (
            cluster
            and _names(v["text"])
            and v["box"][1] - cluster[-1]["box"][3] > largest * 0.5
        ):
            break
        if (
            v["size"] < largest * 0.85
            or cluster
            and v["box"][1] - cluster[-1]["box"][3] > 40
        ):
            break
        cluster.append(v)
    title = " ".join(v["text"] for v in cluster)
    if not 3 <= len(_words(title)) <= 45 or re.search(
        r"\b(?:figure|table|plate|praise|reviewed|edited|translated)\b", title, re.I
    ):
        return not_found()
    left = min(v["box"][0] for v in cluster)
    following = _column_lines(
        [v for v in lines if v["box"][1] >= cluster[-1]["box"][3] - 2],
        left,
        width * 0.98,
    )
    for index, first in enumerate(following[:12]):
        if any(_ROLE.search(v["text"]) for v in following[:index]):
            continue
        if first["box"][1] - cluster[-1]["box"][3] > 140 or _ROLE.search(first["text"]):
            continue
        if first["size"] >= largest or not re.match(
            r"^(?:by\s+)?[^\W\d_]", first["text"], re.I
        ):
            continue
        band = []
        for v in following[index : index + 8]:
            if (
                abs(v["size"] - first["size"]) > first["size"] * 0.12
                or _INSTITUTION.search(v["text"])
                or _ROLE.search(v["text"])
                or v["text"].casefold().startswith(("abstract", "summary"))
            ):
                break
            if band and v["box"][1] - band[-1]["box"][3] > first["size"] * 1.5:
                break
            band.append(v)
        joined = " ".join(v["text"] for v in band)
        joined = re.sub(r",\s*(?:and|&)\s+", " and ", joined, flags=re.I)
        names = _names(joined)
        if not names:
            continue
        tail = following[index + len(band) :]
        nearby = " ".join(
            v["text"] for v in tail if v["box"][1] - band[-1]["box"][3] < height * 0.65
        )
        if not re.search(r"\b(?:abstract|summary)\b", nearby, re.I):
            continue
        if not (
            _INSTITUTION.search(nearby) or re.match(r"^\s*Abstract\b", nearby, re.I)
        ):
            continue
        return {
            "author": ", ".join(names),
            "source": "text_structured_opening_credit",
            "page": 1,
            "evidence": title + " / " + joined,
            "mechanism": "complete_native_author_band",
            "geometry": {
                "title": [v["box"] for v in cluster],
                "byline": [v["box"] for v in band],
            },
        }
    return not_found()


def alternating_credit(lines, width, height):
    """A title, complete author/affiliation pairs, then an abstract.

    Equal font sizes do not erase these textual role boundaries. A name-like
    subtitle is not a person credit unless it has its own affiliation.
    """
    lines = _column_lines(lines, 0, width)
    stop = next(
        (
            i
            for i, line in enumerate(lines[:24])
            if re.fullmatch(r"(?i)abstract", line["text"].strip())
        ),
        None,
    )
    if stop is None:
        return not_found()
    pairs = []
    for i in range(stop - 1):
        raw = lines[i]["text"]
        names = _names(raw)
        if names and _INSTITUTION.search(lines[i + 1]["text"]):
            pairs.append((i, names))
    if not pairs or len(pairs) > 6:
        return not_found()
    first = pairs[0][0]
    heading = [
        line
        for line in lines[max(0, first - 4):first]
        if len(_words(line["text"])) >= 3 and not _ROLE.search(line["text"])
    ]
    title = " ".join(line["text"] for line in heading)
    if not 4 <= len(_words(title)) <= 45 or _INSTITUTION.search(title):
        return not_found()
    # Every line between the first credit and the abstract must have a role;
    # do not quietly drop unparsed coauthors, editors or a department's staff.
    covered = {i for index, _names_at_index in pairs for i in (index, index + 1)}
    if any(i not in covered for i in range(first, stop)):
        return not_found()
    names = [name for _, people in pairs for name in people]
    return {
        "author": ", ".join(names),
        "source": "text_structured_opening_credit",
        "page": 1,
        "evidence": title + " / " + " / ".join(lines[i]["text"] for i, _ in pairs),
        "mechanism": "title_author_affiliation_pairs_before_abstract",
        "geometry": {"byline": [lines[i]["box"] for i, _ in pairs]},
    }



def deck_credit(lines, width, height):
    """A feature/commentary byline may follow a summary deck, above body prose."""
    if not any(re.fullmatch(r"FEATURE ARTICLE|INSIGHT", line["text"], re.I)
               for line in lines[:8]):
        return not_found()
    eligible = [line for line in lines if line["box"][1] < height * .45
                and 2 <= len(_words(line["text"])) <= 25
                and not line["text"].isupper() and not _ROLE.search(line["text"])]
    if not eligible:
        return not_found()
    largest = max(line["size"] for line in eligible)
    heading = [line for line in eligible if line["size"] >= largest * .9]
    bottom = max(line["box"][3] for line in heading)
    for index, line in enumerate(lines):
        if line["box"][1] <= bottom or line["box"][1] > height * .65:
            continue
        if not line["text"].isupper() or _ROLE.search(line["text"]):
            continue
        deck = ' '.join(v['text'] for v in lines[:index] if v['box'][1] >= bottom)
        if len(deck.split()) < 12:
            continue
        band = [line]
        for following in lines[index+1:index+4]:
            if (not following['text'].isupper() or _ROLE.search(following['text'])
                    or abs(following['size']-line['size']) > line['size']*.15
                    or following['box'][1]-band[-1]['box'][3] > line['size']*1.5):
                break
            band.append(following)
        joined = ' '.join(v['text'] for v in band)
        names = _names(joined)
        tail = ' '.join(v['text'] for v in lines[index+len(band):index+len(band)+5])
        if not names or len(tail.split()) < 12 or not any(c.islower() for c in tail):
            continue
        return {'author': ', '.join(names), 'source': 'text_structured_opening_credit',
                'page':1, 'evidence': ' / '.join(v['text'] for v in heading)+' / '+joined,
                'mechanism':'feature_deck_then_author_byline',
                'geometry':{'byline':[v['box'] for v in band]}}
    return not_found()


def infer(context):
    if (
        context.path is None
        or context.profile.kind not in {"generic", "scholarly_article"}
        or context.profile.work_scope == "whole_issue"
    ):
        return not_found()
    import fitz

    try:
        stat = context.path.stat()
        key = (str(context.path), stat.st_size, stat.st_mtime_ns)
        if key in _CACHE:
            return dict(_CACHE[key])
        with fitz.open(context.path) as doc:
            result = not_found()
            for page_index in range(min(2, len(doc))):
                page = doc[page_index]
                lines = positioned_lines(page)
                result = credit(lines, page.rect.width, page.rect.height)
                if not result.get("author"):
                    result = alternating_credit(
                        lines, page.rect.width, page.rect.height
                    )
                if not result.get("author"):
                    result = deck_credit(lines, page.rect.width, page.rect.height)
                if result.get("author"):
                    result["page"] = page_index + 1
                    break
                # Only a sparse wrapper warrants a second opening page.
                if sum(len(line["text"].split()) for line in lines) >= 30:
                    break
        if len(_CACHE) > 256:
            _CACHE.clear()
        if result.get("author"):
            _CACHE[key] = dict(result)
        return result
    except OSError, ValueError, RuntimeError:
        return not_found()
