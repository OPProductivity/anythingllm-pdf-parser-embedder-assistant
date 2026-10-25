"""Bind an author-above-title opening to its later paired running heads."""

import math
import re

from .ocr_geometry import region_in_pdf_points
from .publishing_relations import _claim
from .structured_credit import _names, _words


_CHAPTER = re.compile(r"^(?:chapter|chapitre|kapitel|cap[ií]tulo|hoofdstuk)\s+(?:\d{1,3}|[IVXLCDM]{1,8})[.:]?\s*$", re.I)
_CHAPTER_IN_TEXT = re.compile(_CHAPTER.pattern, re.I | re.M)


def _topic_use(text, name_words):
    words = re.findall(r"[^\W\d_]+", text, re.U)
    width = len(name_words)
    return any(tuple(_words(" ".join(words[i:i + width]))) == name_words
               and words[i + width - 1][0].islower()
               for i in range(len(words) - width + 1))


def _views(bundle):
    dimensions = {page["page"]: (page.get("width"), page.get("height"))
                  for page in bundle.get("native", [])}
    for page in bundle.get("native", []):
        if page.get("horizontal_lines") and page.get("width") and page.get("height"):
            yield {**page, "lines": page["horizontal_lines"], "coordinate_space": "page_points"}
    for primary in bundle.get("ocr", []):
        for region in (primary, primary.get("alternate")):
            if not region or not region.get("lines"):
                continue
            try:
                view = region_in_pdf_points(region)
                width, height = dimensions.get(view["page"], (None, None))
                clip = region.get("clip")
                if (any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                        for value in (width, height))
                        or not isinstance(clip, (list, tuple)) or len(clip) != 4
                        or any(type(value) not in (int, float) or not math.isfinite(value) for value in clip)
                        or not (0 <= clip[0] < clip[2] <= width + .1
                                and 0 <= clip[1] < clip[3] <= height + .1)):
                    continue
                # A crop's first line is not necessarily a page running head.
                # Retain its local-column scale, but anchor positions and the
                # header band to the actual page before interpreting roles.
                for line in view["lines"]:
                    x0, y0, x1, y1 = line["box"]
                    line["box"] = [x0 + clip[0], y0 + clip[1], x1 + clip[0], y1 + clip[1]]
                view.update(crop_width=view["width"], width=width, height=height,
                            source_crop=list(clip), coordinate_space="page_points")
                yield view
            except (TypeError, ValueError):
                # Incomplete historical/stub geometry cannot justify a new
                # structural claim. Other experts retain their raw evidence.
                continue


def claims(bundle, *, kind, work_scope):
    """Require chapter/name/title geometry and separate later title/name heads.

    The author is read from the source. Two distinct later PDF pages must
    repeat the full name, and a different header must match the following
    local title. Frequent ordinary-prose occurrences make a purported name
    ambiguous with a topic; properly cased self-citations remain compatible
    with authorship. These guards do not veto other experts.
    """
    if work_scope == "whole_issue" or kind not in {"generic", "book_chapter"}:
        return []
    # Most articles have no chapter opening. Do not copy/normalize every OCR
    # line for a specialist whose required publishing act is absent.
    raw = [*bundle.get("native", []), *(region for primary in bundle.get("ocr", [])
           for region in (primary, primary.get("alternate")) if region)]
    if not any(region.get("page", 0) <= 5 and _CHAPTER_IN_TEXT.search(
        region.get("text") or "\n".join(line["text"] for line in region.get("lines", [])))
        for region in raw):
        return []
    views = list(_views(bundle))
    headers, body = [], {}
    for view in views:
        for line in view["lines"]:
            text = re.sub(r"^\s*\d{1,4}\s+|\s+\d{1,4}\s*$", "", line["text"]).strip()
            if line["box"][1] < view["height"] * .13:
                headers.append((view["page"], text, tuple(_words(text))))
            elif .16 * view["height"] <= line["box"][1] <= .9 * view["height"]:
                body[view["page"], tuple(_words(line["text"]))] = line["text"]
    found, seen = [], set()
    for view in views:
        if view["page"] > 5:
            continue
        lines = sorted(view["lines"], key=lambda line: (line["box"][1], line["box"][0]))
        for marker in lines:
            if not _CHAPTER.fullmatch(marker["text"].strip()) or marker["box"][1] > view["height"] * .3:
                continue
            # The previous facing leaf may contribute text before/alongside
            # the opening. Follow the marker's local column, not list order.
            column = [line for line in lines
                      if marker["box"][3] < line["box"][1] < marker["box"][3] + 140
                      and abs(line["box"][0] - marker["box"][0]) < view.get("crop_width", view["width"]) * .15]
            for byline in column:
                names = _names(byline["text"])
                if len(names) != 1 or not 2 <= len(_words(names[0])) <= 5:
                    continue
                name = names[0]
                key = tuple(_words(name))
                later = [(page, raw, words) for page, raw, words in headers if view["page"] < page <= view["page"] + 3]
                matches = [(page, raw) for page, raw, words in later if words == key]
                pages = sorted({page for page, _raw in matches})
                if len(pages) < 2:
                    continue
                topic_occurrences = {(page, words) for (page, words), text in body.items()
                                     if view["page"] < page <= view["page"] + 3
                                     and _topic_use(text, key)}
                if len(topic_occurrences) >= 3:
                    continue
                titles = [line for line in column if byline["box"][3] < line["box"][1]
                          and len(line["text"].split()) <= 16
                          and not _CHAPTER.fullmatch(line["text"].strip())]
                title = " ".join(line["text"] for line in titles[:4])
                title_words = tuple(_words(title))
                if len(title_words) < 3:
                    continue
                title_heads = [(page, raw) for page, raw, words in later
                               if 2 <= len(words) <= 10 and words != key
                               and any(title_words[i:i + len(words)] == words
                                       for i in range(len(title_words) - len(words) + 1))]
                if not title_heads or (view["page"], key) in seen:
                    continue
                seen.add((view["page"], key))
                claim = _claim(name, "author", {"page": view["page"]},
                    " / ".join([marker["text"], byline["text"], title]),
                    "author_above_chapter_title_with_paired_running_heads", 116, work="selected chapter")
                claim.update(corroboration_pages=pages, corroboration=[raw for _page, raw in matches],
                    title_corroboration=[raw for _page, raw in title_heads],
                    geometry_coordinate_space=view["coordinate_space"],
                    geometry_source_crop=view.get("source_crop"),
                    geometry={"chapter": marker["box"], "byline": byline["box"],
                              "title": [line["box"] for line in titles[:4]]},
                    scope_reason="local chapter marker, author above title and distinct repeated name/title heads")
                found.append(claim)
    return found
