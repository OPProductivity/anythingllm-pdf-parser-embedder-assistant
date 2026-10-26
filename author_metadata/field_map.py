"""Reversible, diagnostic page-field proposals for author metadata.

This module never rewrites source text or selects an author.  Every grouping
names positioned source atoms, so alternative layouts can be reconsidered.
"""

from __future__ import annotations

from collections import defaultdict
from bisect import bisect_left
from difflib import SequenceMatcher
import math
from pathlib import Path
import re
import unicodedata

import fitz
from .native_reads import native_measurement, native_read, review_scope
from .lexical_scope import lexical_scope

from .names import (has_author_affiliation_hint, looks_like_person_name,
                    normalize_author_candidate, split_author_line_candidates)
from .title_evidence import words as title_words


def _fold(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in value if not unicodedata.combining(char)).casefold()


def _tokens(value):
    return re.findall(r"[^\W_]+(?:[-’'][^\W_]+)*", _fold(value), re.UNICODE)


def _box(items):
    return [round(operation(float(atom[axis]) for atom in items), 2)
            for axis, operation in (("x0", min), ("y0", min), ("x1", max), ("y1", max))]


def _composition(atoms, indexes, kind):
    indexes = tuple(indexes)
    selected = [atoms[index] for index in indexes]
    return {
        "id": f"g{len(indexes)}-{indexes[0]}-{indexes[-1]}-{kind}",
        "kind": kind,
        "atom_ids": [atom["id"] for atom in selected],
        "text": " ".join(atom["text"] for atom in selected),
        "bbox": _box(selected),
        "font_size": round(max(float(atom.get("font_size") or 0) for atom in selected), 2),
    }


def native_page_atoms(page, page_number):
    return native_measurement(
        page, 'positioned_native_atoms_v1', (page_number,),
        lambda: _native_page_atoms(page, page_number),
    )


def _native_page_atoms(page, page_number):
    """Keep positioned words and their extractor references, without reflow."""
    # Both views use identical flags. Extract the page once, then derive each
    # existing representation; this TextPage never leaves the current call.
    flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
    textpage = page.get_textpage(flags=flags)
    data = page.get_text("dict", sort=False, flags=flags, textpage=textpage)
    blocks = data.get("blocks") or []
    atoms = []
    for index, word in enumerate(page.get_text("words", sort=False, textpage=textpage)):
        if len(word) < 8 or not str(word[4]).strip():
            continue
        x0, y0, x1, y1, value, block_no, line_no, word_no = word[:8]
        spans = []
        if 0 <= block_no < len(blocks):
            lines = blocks[block_no].get("lines") or []
            if 0 <= line_no < len(lines):
                spans = lines[line_no].get("spans") or []
        intersecting = [span for span in spans if
                        min(x1, span["bbox"][2]) > max(x0, span["bbox"][0]) and
                        min(y1, span["bbox"][3]) > max(y0, span["bbox"][1])]
        font = max(intersecting, key=lambda span: float(span.get("size") or 0), default={})
        atoms.append({
            "id": f"p{page_number}-n{index}", "source": "native_word", "text": str(value),
            "page": page_number, "x0": round(float(x0), 2), "y0": round(float(y0), 2),
            "x1": round(float(x1), 2), "y1": round(float(y1), 2),
            "source_block": int(block_no), "source_line": int(line_no),
            "source_word": int(word_no), "font_size": round(float(font.get("size") or 0), 2),
            "font": str(font.get("font") or ""),
        })
    return atoms


def _line_compositions(atoms, page_width):
    """Keep both extractor lines and geometry-based alternatives."""
    if not atoms:
        return []
    proposals = []
    seen = set()
    membership_geometry = {}
    atom_index = {atom["id"]: index for index, atom in enumerate(atoms)}

    def add(indexes, kind):
        indexes = tuple(indexes)
        if not indexes or (indexes, kind) in seen:
            return
        seen.add((indexes, kind))
        # Distinct layout interpretations may have identical membership. Their
        # geometry and text are the same facts, while kind/ID and mutable lists
        # must remain independent for each expert's interpretation.
        if indexes not in membership_geometry:
            group = _composition(atoms, indexes, kind)
            membership_geometry[indexes] = group
        else:
            source = membership_geometry[indexes]
            group = {
                **source,
                "id": f"g{len(indexes)}-{indexes[0]}-{indexes[-1]}-{kind}",
                "kind": kind,
                "atom_ids": list(source["atom_ids"]),
                "bbox": list(source["bbox"]),
            }
        proposals.append(group)

    native_lines = defaultdict(list)
    for index, atom in enumerate(atoms):
        native_lines[(atom["source_block"], atom["source_line"])].append(index)
    for indexes in native_lines.values():
        indexes.sort(key=lambda index: atoms[index]["x0"])
        add(indexes, "extractor_line")
        runs = [[indexes[0]]]
        for index in indexes[1:]:
            previous = atoms[runs[-1][-1]]
            gap = atoms[index]["x0"] - previous["x1"]
            height = max(1.0, previous["y1"] - previous["y0"])
            if gap > max(1.8 * height, page_width * .025):
                runs.append([])
            runs[-1].append(index)
        for run in runs:
            add(run, "gap_split_line")
        for index in range(1, len(indexes)):
            if _fold(atoms[indexes[index]]["text"]).strip(".:--") == "by":
                add(indexes[:index], "before_by")
                add(indexes[index:], "byline_tail")

    # Independent alignment proposal: lines in separate PDF blocks may sit on
    # one visual baseline.  The extractor's block boundaries remain intact.
    ordered = sorted(range(len(atoms)), key=lambda index: (
        (atoms[index]["y0"] + atoms[index]["y1"]) / 2, atoms[index]["x0"]))
    baselines = []
    baseline_centers = []
    finite_centers = True
    for index in ordered:
        atom = atoms[index]
        center = (atom["y0"] + atom["y1"]) / 2
        height = max(1.0, atom["y1"] - atom["y0"])
        start = 0
        if finite_centers and math.isfinite(center) and math.isfinite(height):
            # Centers are appended in sorted order and never moved. Anything
            # below this conservative bound cannot pass the original predicate.
            # Keep its exact first-match order and floating-point comparison.
            extent = height * .36
            slack = max(1.0, abs(center), extent) * 1e-12
            start = bisect_left(baseline_centers, center - extent - slack)
        match = next((row for row in baselines[start:] if abs(center - row["center"]) <= min(height, row["height"]) * .36), None)
        if match is None:
            baselines.append({"center": center, "height": height, "indexes": [index]})
            baseline_centers.append(center)
            finite_centers = finite_centers and math.isfinite(center)
        else:
            match["indexes"].append(index)
    for row in baselines:
        indexes = sorted(row["indexes"], key=lambda index: atoms[index]["x0"])
        runs = [[indexes[0]]]
        for index in indexes[1:]:
            prior = atoms[runs[-1][-1]]
            gap = atoms[index]["x0"] - prior["x1"]
            height = max(1.0, prior["y1"] - prior["y0"])
            if gap > max(1.8 * height, page_width * .025):
                runs.append([])
            runs[-1].append(index)
        for run in runs:
            add(run, "aligned_line")

    # Alternative two- and three-line compositions.  These are references,
    # not a destructive merger or an asserted reading order for the page.
    lines = []
    line_memberships = set()
    for group in proposals:
        if group["kind"] not in {"gap_split_line", "aligned_line"}:
            continue
        membership = tuple(group["atom_ids"])
        if membership in line_memberships:
            continue
        line_memberships.add(membership)
        lines.append(group)
    lines.sort(key=lambda group: (group["bbox"][1], group["bbox"][0]))
    for position, first in enumerate(lines):
        chain = [first]
        for next_line in lines[position + 1:]:
            previous = chain[-1]
            left, top, right, bottom = next_line["bbox"]
            gap = top - previous["bbox"][3]
            h = max(1.0, previous["bbox"][3] - previous["bbox"][1])
            if gap > h * 2.2:
                break
            aligned = abs(left - previous["bbox"][0]) <= max(h * 1.5, page_width * .035)
            overlaps = min(right, previous["bbox"][2]) > max(left, previous["bbox"][0])
            font_ratio = next_line["font_size"] / max(1.0, previous["font_size"])
            if not (-h * .30 <= gap <= h * 1.9 and (aligned or overlaps)
                    and .82 <= font_ratio <= 1.22):
                continue
            chain.append(next_line)
            indexes = sorted({atom_index[atom_id]
                              for group in chain for atom_id in group["atom_ids"]},
                             key=lambda index: (atoms[index]["y0"], atoms[index]["x0"]))
            add(indexes, "stacked_lines")
            if len(chain) >= 5:
                break
    return proposals


def _title_overlap(text, title_hint, *, expected_words=None):
    expected = title_words(title_hint) if expected_words is None else expected_words
    observed = title_words(text)
    if len(expected) < 2 or not observed:
        return 0.0
    if len(expected) == 2:
        return 1.0 if observed == expected else 0.0
    return SequenceMatcher(None, expected, observed, autojunk=False).ratio()


def map_atoms(atoms, page_number, width, height, title_hint=""):
    width, height = float(width), float(height)
    groups = _line_compositions(atoms, width)
    groups_by_id = {}
    for group in groups:
        # Preserve the original first-match semantics even for colliding IDs.
        groups_by_id.setdefault(group["id"], group)
    expected_title_words = title_words(title_hint)
    title_scores = {}
    fields = []
    for group in groups:
        content = group["text"].strip()
        top = group["bbox"][1]
        if content not in title_scores:
            title_scores[content] = _title_overlap(
                content, title_hint, expected_words=expected_title_words
            )
        title_score = title_scores[content]
        if title_score >= .68 and top <= height * .82:
            fields.append({"role": "work_title", "group_id": group["id"],
                           "score": round(title_score, 3), "reason": "visible_title_overlap"})
        if re.match(r"(?i)^\s*(?:abstract|summary)\b", content):
            fields.append({"role": "abstract", "group_id": group["id"], "reason": "heading"})
        if re.search(r"(?i)(?:©|\bcopyright\b|\ball rights reserved\b)", content):
            fields.append({"role": "copyright", "group_id": group["id"], "reason": "printed_notice"})
        if group["kind"] in {"gap_split_line", "aligned_line"} and has_author_affiliation_hint(content):
            fields.append({"role": "affiliation", "group_id": group["id"],
                           "reason": "institution_or_department_line"})
        if re.search(r"(?i)\b(?:doi\s*:|https?://doi\.org/)\s*10\.\d", content):
            fields.append({"role": "doi", "group_id": group["id"],
                           "reason": "printed_identifier"})
        if re.match(r"(?i)^\s*(?:by|author(?:s)?\s*:)\s+", content):
            candidate = normalize_author_candidate(content)
            listed = split_author_line_candidates(candidate, require_complete=True)
            if listed or looks_like_person_name(candidate):
                fields.append({"role": "author_credit", "group_id": group["id"],
                               "value": ", ".join(listed) if listed else candidate,
                               "reason": "explicit_byline"})
        elif group["kind"] in {"gap_split_line", "aligned_line", "byline_tail"} and top < height * .65:
            # A superscript affiliation marker can be fused into the last
            # extracted word (e.g. García-Louis1). Only the proposed value is
            # cleaned; the atom and its full source text remain untouched.
            credit_value = re.sub(r"(?<=\w)[*†‡¹²³\d]$", "", content).strip()
            # The embedded title can itself be a polluted filename such as
            # "Adam Anzel - [article title]". It must not veto a visibly
            # separate author line; spatial work linking is checked below.
            if looks_like_person_name(credit_value):
                fields.append({"role": "possible_person_credit", "group_id": group["id"],
                               "value": normalize_author_candidate(credit_value),
                               "reason": ("terminal_affiliation_marker_proposed"
                                          if credit_value != content else "name_shape_needs_work_link")})
    if not any(field["role"] == "work_title" for field in fields):
        # Bad embedded titles are common. This is a candidate from local
        # typography, not an automatic replacement for the work title.
        opening_lines = [group for group in groups
                         if group["kind"] in {"aligned_line", "gap_split_line"}
                         and 0 <= group["bbox"][1] < height * .42
                         and group["bbox"][0] < width * .72
                         and len(group["text"]) >= 8]
        largest = max((group["font_size"] for group in opening_lines), default=0)
        body_sizes = sorted(float(atom.get("font_size") or 0) for atom in atoms
                            if 0 < atom["y0"] < height * .85 and atom.get("font_size"))
        typical = body_sizes[len(body_sizes) // 2] if body_sizes else 0
        if largest >= max(12, typical * 1.35):
            prominent = [group for group in opening_lines
                         if group["font_size"] >= largest * .92]
            for group in prominent[:6]:
                fields.append({"role": "work_title", "group_id": group["id"],
                               "score": .45, "reason": "prominent_opening_typography_unverified"})
    for field in list(fields):
        if field["role"] != "work_title" or field.get("reason") != "visible_title_overlap":
            continue
        base = groups_by_id.get(field["group_id"])
        if base is None:
            continue
        base_ids = set(base["atom_ids"])
        extensions = [group for group in groups
                      if group["kind"] == "stacked_lines"
                      and base_ids < set(group["atom_ids"])
                      and group["font_size"] >= base["font_size"] * .9
                      and group["bbox"][3] - base["bbox"][1] <= height * .22]
        if extensions:
            extended = max(extensions, key=lambda group: len(group["atom_ids"]))
            fields.append({"role": "work_title", "group_id": extended["id"],
                           "score": min(.81, field["score"] - .01),
                           "reason": "typographic_title_extension"})
    contents_headings = [group for group in groups
                         if re.fullmatch(r"(?i)(?:table of )?contents", group["text"].strip())
                         and 0 <= group["bbox"][1] < height * .35]
    if contents_headings:
        heading_bottom = min(group["bbox"][3] for group in contents_headings)
        for field in fields:
            group = groups_by_id.get(field["group_id"])
            if field["role"] == "work_title" and group and group["bbox"][1] > heading_bottom:
                field["role"] = "contents_entry_title"
                field["reason"] = "title_words_inside_contents_entry"
    title_group_ids = {row["group_id"] for row in fields if row["role"] == "work_title"}
    title_groups = [group for group in groups if group["id"] in title_group_ids]
    for row in fields:
        if row["role"] not in {"author_credit", "possible_person_credit"}:
            continue
        candidate = groups_by_id[row["group_id"]]
        row["near_title"] = False
        for title in title_groups:
            below_aligned = (
                0 <= candidate["bbox"][1] - title["bbox"][3] <= height * .16
                and abs(candidate["bbox"][0] - title["bbox"][0]) <= width * .28
            )
            right_adjacent = (
                candidate["bbox"][0] >= title["bbox"][2] - width * .025
                and candidate["bbox"][2] >= width * .72
                and title["bbox"][1] <= candidate["bbox"][1] <= title["bbox"][3] + height * .08
            )
            if below_aligned or right_adjacent:
                row["near_title"] = True
                row["work_link"] = ("below_title_aligned" if below_aligned
                                    else "right_of_title_same_opening")
                break
    linked_people = [row for row in fields
                     if row["role"] == "possible_person_credit" and row.get("near_title")]
    for lower in linked_people:
        lower_group = _field_group({"groups": groups}, lower)
        if not lower_group or "," not in lower_group["text"]:
            continue
        for upper in linked_people:
            if upper is lower:
                continue
            upper_group = _field_group({"groups": groups}, upper)
            if not upper_group or "," in upper_group["text"]:
                continue
            line_height = max(1.0, upper_group["bbox"][3] - upper_group["bbox"][1])
            if (0 <= lower_group["bbox"][1] - upper_group["bbox"][3] <= line_height * 1.5
                    and abs(lower_group["bbox"][0] - upper_group["bbox"][0]) <= width * .04):
                lower["competing_role"] = "affiliation_or_location"
                lower["reason"] = "person_shaped_line_below_prior_credit"
                fields.append({"role": "affiliation", "group_id": lower["group_id"],
                               "reason": "location_below_person_credit"})
                break
    text = " ".join(atom["text"] for atom in atoms[:800]).casefold()
    roles = []
    for role, pattern in (
        ("dedication", r"\bdedicated to\b"),
        ("copyright_page", r"(?:©|\bcopyright\b|\ball rights reserved\b|\bisbn\b)"),
        ("foreword", r"\bforeword\b"),
        ("preface", r"\bpreface\b"),
        ("repository_wrapper", r"\b(?:repository|downloaded from|researchgate)\b"),
    ):
        if re.search(pattern, text):
            roles.append(role)
    if contents_headings:
        roles.append("contents")
    if any(re.fullmatch(r"(?i)index", group["text"].strip())
           and 0 <= group["bbox"][1] < height * .35 for group in groups):
        roles.append("index")
    if title_groups:
        roles.append("visible_work_title")
    if "visible_work_title" in roles and any(field["role"] == "abstract" for field in fields):
        roles.append("article_opening")
    elif "visible_work_title" in roles and len(atoms) < 100:
        roles.append("sparse_title_page_candidate")
    return {"page": page_number, "width": round(width, 2), "height": round(height, 2),
            "atoms": atoms, "groups": groups, "field_candidates": fields,
            "page_roles": roles}


def map_page(page, page_number, title_hint=""):
    atoms = native_page_atoms(page, page_number)
    result = map_atoms(atoms, page_number, page.rect.width, page.rect.height, title_hint)
    result["visual_object_count"] = (
        len(native_read(page, 'get_images', full=True)) + (len(native_read(page, 'get_drawings')) if not atoms else 0)
    )
    return result


@lexical_scope()
def map_pdf_opening(pdf_path: Path, title_hint="", max_frontmatter_pages=12):
    """Read four pages, extending bounded front matter until title and credit link."""
    pages = []
    def work_page_found(page):
        verified_title = any(field["role"] == "work_title"
                             and field.get("reason") == "visible_title_overlap"
                             for field in page["field_candidates"])
        linked_credit = any(field["role"] in {"author_credit", "possible_person_credit"}
                            and field.get("near_title") for field in page["field_candidates"])
        return verified_title and linked_credit

    with fitz.open(pdf_path) as document:
        first_count = min(4, len(document))
        for index in range(first_count):
            pages.append(map_page(document[index], index + 1, title_hint))
        work_found = any(work_page_found(page) for page in pages)
        frontmatter = any(set(page["page_roles"]) & {
            "dedication", "copyright_page", "contents", "foreword", "preface", "repository_wrapper"
        } for page in pages)
        if not work_found and frontmatter:
            for index in range(first_count, min(max_frontmatter_pages, len(document))):
                pages.append(map_page(document[index], index + 1, title_hint))
                if work_page_found(pages[-1]):
                    break
    return pages


def render_field_overlay(pdf_path, page_map, output_path, *, dpi=130):
    """Draw selected proposals over a page image without editing the PDF."""
    from PIL import Image, ImageDraw

    page_number = int(page_map["page"])
    with fitz.open(pdf_path) as document:
        page = document[page_number - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
        width, height = float(page.rect.width), float(page.rect.height)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    draw = ImageDraw.Draw(image)
    colors = {"work_title": "#1464c8", "author_credit": "#159447",
              "possible_person_credit": "#46a84e", "contents_entry_title": "#d48620",
              "abstract": "#9c45bd", "affiliation": "#c07a18", "copyright": "#818181"}
    selected = []
    for role in colors:
        choices = [field for field in page_map["field_candidates"] if field["role"] == role]
        if role == "work_title":
            best_score = max((field.get("score", 0) for field in choices), default=0)
            choices = [field for field in choices
                       if field.get("score", 0) >= best_score - .08]
            choices.sort(key=lambda field: len((_field_group(page_map, field) or {}).get("atom_ids") or []),
                         reverse=True)
            choices = choices[:1]
        elif role in {"author_credit", "possible_person_credit"}:
            choices = [field for field in choices if field.get("near_title")
                       and field.get("competing_role") != "affiliation_or_location"][:3]
        else:
            choices = choices[:1]
        selected.extend(choices)
    seen = set()
    for field in selected:
        if (field["role"], field["group_id"]) in seen:
            continue
        seen.add((field["role"], field["group_id"]))
        group = _field_group(page_map, field)
        if not group:
            continue
        x0, y0, x1, y1 = group["bbox"]
        box = (int(x0 / width * pix.width), int(y0 / height * pix.height),
               int(x1 / width * pix.width), int(y1 / height * pix.height))
        color = colors[field["role"]]
        draw.rectangle(box, outline=color, width=3)
        label = field["role"]
        if field.get("value"):
            label += ": " + field["value"][:55]
        draw.text((box[0], max(0, box[1] - 16)), label, fill=color,
                  stroke_width=1, stroke_fill="white")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    image.close()
    return output_path


def compact_field_review(report):
    """Persist field evidence, not every body-text word in sampled pages."""
    compact = {key: value for key, value in report.items() if key != "pages"}
    compact["pages"] = []
    for page in report.get("pages") or []:
        fields = page["field_candidates"]
        ids = {field["group_id"] for field in fields}
        groups = [group for group in page["groups"] if group["id"] in ids]
        atom_ids = {atom_id for group in groups for atom_id in group["atom_ids"]}
        compact["pages"].append({
            "page": page["page"], "width": page["width"], "height": page["height"],
            "page_roles": page["page_roles"],
            "visual_object_count": page.get("visual_object_count"),
            "source_atom_count": len(page["atoms"]),
            "atoms": [atom for atom in page["atoms"] if atom["id"] in atom_ids],
            "groups": groups, "field_candidates": fields,
        })
    return compact


def _author_key(value):
    """A comparison key, not an author inference or name normalization."""
    parts = _tokens(normalize_author_candidate(value))
    return parts[-1] if parts else ""


def _author_keys(value):
    listed = split_author_line_candidates(value, require_complete=True)
    return {_author_key(part) for part in (listed or [value]) if _author_key(part)}


def _visible_token(value, text):
    token = _author_key(value)
    return bool(token and token in _tokens(text))


def _field_group(page_map, field):
    return next((group for group in page_map["groups"]
                 if group["id"] == field["group_id"]), None)


def _focus_crops(page_map, author=""):
    """Try a tight visual composition, then wider alternatives on conflict."""
    width, height = page_map["width"], page_map["height"]
    titles = sorted((field for field in page_map["field_candidates"]
                     if field["role"] == "work_title"),
                    key=lambda field: field.get("score", 0), reverse=True)
    credits = [field for field in page_map["field_candidates"]
               if field["role"] in {"author_credit", "possible_person_credit"}
               and (field.get("near_title") or _author_key(field.get("value")) == _author_key(author))]
    anchors = [_field_group(page_map, field) for field in titles[:1] + credits[:2]]
    anchors = [group for group in anchors if group]
    crops = []
    if anchors:
        left, top, right, bottom = _box([
            {"x0": group["bbox"][0], "y0": group["bbox"][1],
             "x1": group["bbox"][2], "y1": group["bbox"][3]} for group in anchors
        ])
        box = [max(0.0, (left - width * .05) / width),
               max(0.0, (top - height * .045) / height),
               min(1.0, (right + width * .05) / width),
               min(1.0, (bottom + height * .07) / height)]
        # Short title-only crops often exclude an unrecognized author below.
        box[3] = max(box[3], min(.60, box[1] + .18))
        if box[2] - box[0] >= .12 and box[3] - box[1] >= .08:
            crops.append({"composition": "title_and_credit_region", "fraction": box, "psm": 6})
    crops.extend((
        {"composition": "wide_opening", "fraction": [0.02, 0.02, .98, .68], "psm": 4},
        {"composition": "full_page", "fraction": [0, 0, 1, 1], "psm": 3},
    ))
    return crops


def ocr_page_crop(pdf_path, page_number, fraction, psm, *, tesseract):
    """Use the existing crop OCR and retain its word geometry and provenance."""
    from PIL import Image, ImageOps
    from rag_pdf_tools import _ocr_photographed_crop_with_layout

    if not tesseract or not Path(tesseract).is_file():
        return {"status": "unavailable", "reason": "tesseract_missing", "text": "", "atoms": []}
    with fitz.open(pdf_path) as document:
        page = document[page_number - 1]
        width, height = page.rect.width, page.rect.height
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    recognition = {}
    text, rows = _ocr_photographed_crop_with_layout(
        image, tuple(fraction), str(tesseract), ImageOps, psm=psm,
        recognition_evidence=recognition,
    )
    image.close()
    atoms = []
    for index, row in enumerate(rows):
        crop_width = float(row.get("crop_width") or 0)
        crop_height = float(row.get("crop_height") or 0)
        if crop_width <= 0 or crop_height <= 0:
            continue
        x0 = (fraction[0] + float(row["left"]) / crop_width * (fraction[2] - fraction[0])) * width
        y0 = (fraction[1] + float(row["top"]) / crop_height * (fraction[3] - fraction[1])) * height
        x1 = (fraction[0] + float(row["left"] + row["width"]) / crop_width * (fraction[2] - fraction[0])) * width
        y1 = (fraction[1] + float(row["top"] + row["height"]) / crop_height * (fraction[3] - fraction[1])) * height
        atoms.append({
            "id": f"p{page_number}-o{index}", "source": "tesseract_crop_word",
            "text": row["text"], "page": page_number,
            "x0": round(x0, 2), "y0": round(y0, 2), "x1": round(x1, 2), "y1": round(y1, 2),
            "source_block": int(row["block"]), "source_line": int(row["line"]),
            "source_word": int(row["word"]), "font_size": 0, "font": "",
            "ocr_confidence": row.get("confidence"),
        })
    return {"status": "ok" if text else "empty", "text": text, "atoms": atoms,
            "width": width, "height": height, "recognition": recognition}


def _check_ocr(author, text, ocr_map=None, title_hint="", native_map=None):
    """Compare role evidence; mere occurrence in body text is not agreement."""
    credits = []
    explicit_credits = []
    if ocr_map:
        credits = [field for field in ocr_map["field_candidates"]
                   if field["role"] in {"author_credit", "possible_person_credit"}
                   and field.get("near_title")
                   and field.get("competing_role") != "affiliation_or_location"]
        explicit_credits = [field for field in credits if field["role"] == "author_credit"]
    for match in re.finditer(r"(?im)^\s*(?:by|authors?\s*:)\s+([^\n]{5,85})\s*$", text or ""):
        candidate = normalize_author_candidate(match.group(1))
        listed = split_author_line_candidates(candidate, require_complete=True)
        if listed or looks_like_person_name(candidate):
            credit = {"value": ", ".join(listed) if listed else candidate,
                      "reason": "explicit_ocr_byline"}
            explicit_credits.append(credit)
            credits.append(credit)
    keys = set().union(*(_author_keys(row.get("value")) for row in credits)) if credits else set()
    expected_keys = _author_keys(author)
    expected = _author_key(author)
    if ocr_map is None or not ocr_map.get("atoms"):
        if not expected and len(keys) > 1:
            return {"status": "inconclusive", "reason": "multiple_ocr_credits_work_scope_ambiguous",
                    "ocr_credit_candidates": sorted({row["value"] for row in credits})}
        if expected_keys & keys and not expected_keys <= keys:
            return {"status": "inconclusive", "reason": "only_some_author_surnames_corroborated",
                    "ocr_credit_candidates": sorted({row["value"] for row in credits})}
        # A flattened OCR byline is useful evidence, but its work/title
        # boundary cannot be verified without word positions. Keep it in the
        # ledger and let the positioned crop make the decisive comparison.
        return {"status": "inconclusive", "reason": "field_boundary_unresolved_flattened_ocr",
                "ocr_credit_candidates": sorted({row["value"] for row in credits}),
                "textual_surname_match": bool(expected_keys and expected_keys <= keys)}
    if ocr_map and native_map:
        # Native geometry supplies the *proposed* title-to-credit relation;
        # independent OCR must read the name at that same physical location.
        # A footer occurrence elsewhere cannot corroborate this proposal.
        linked = [field for field in native_map["field_candidates"]
                  if field["role"] in {"author_credit", "possible_person_credit"}
                  and field.get("near_title")
                  and field.get("competing_role") != "affiliation_or_location"]
        localized = []
        localized_matches = set()
        native_credit_candidates = []
        for field in linked:
            group = _field_group(native_map, field)
            if group is None:
                continue
            x0, y0, x1, y1 = group["bbox"]
            margin_x = native_map["width"] * .018
            margin_y = max(4.0, (y1 - y0) * .4)
            local_words = [atom["text"] for atom in ocr_map["atoms"]
                           if x0 - margin_x <= (atom["x0"] + atom["x1"]) / 2 <= x1 + margin_x
                           and y0 - margin_y <= (atom["y0"] + atom["y1"]) / 2 <= y1 + margin_y]
            local_text = " ".join(local_words)
            native_key = _author_key(field.get("value"))
            if not expected_keys and native_key and local_text:
                local_key = _author_key(local_text)
                if (native_key in _tokens(local_text)
                        or (SequenceMatcher(None, native_key, local_key).ratio() >= .72
                            and len(native_key) >= 4)):
                    native_credit_candidates.append({"native": field["value"],
                                                     "ocr_reading": local_text})
            if native_key in expected_keys and native_key in _tokens(local_text):
                localized_matches.add(native_key)
                continue
            if native_key in expected_keys and local_text:
                possible = normalize_author_candidate(local_text.rstrip("!.,;:*†‡"))
                if looks_like_person_name(possible) and _author_key(possible) != expected:
                    localized.append(possible)
        if expected_keys and localized_matches == expected_keys:
            return {"status": "agrees", "reason": "ocr_names_at_native_title_linked_credits",
                    "ocr_credit_candidates": sorted({row["value"] for row in linked
                                                     if _author_key(row.get("value")) in localized_matches})}
        if localized_matches:
            return {"status": "inconclusive", "reason": "only_some_author_surnames_corroborated",
                    "ocr_credit_candidates": sorted({row["value"] for row in linked
                                                     if _author_key(row.get("value")) in localized_matches})}
        if not expected_keys and native_credit_candidates:
            distinct = {row["native"] for row in native_credit_candidates}
            if len(distinct) == 1:
                return {"status": "candidate_for_review",
                        "reason": "ocr_near_match_native_title_linked_credit",
                        "ocr_credit_candidates": sorted(distinct),
                        "ocr_readings": sorted({row["ocr_reading"] for row in native_credit_candidates})}
            return {"status": "inconclusive", "reason": "multiple_native_title_linked_credits",
                    "ocr_credit_candidates": sorted(distinct)}
        if localized:
            # A one-letter OCR surname variant is a recognition ambiguity,
            # even when every crop repeats it. Do not call the author wrong.
            if any(SequenceMatcher(None, expected, _author_key(value)).ratio() >= .72
                   for value in localized):
                return {"status": "inconclusive", "reason": "ocr_spelling_variant_at_credit",
                        "ocr_credit_candidates": sorted(set(localized))}
            return {"status": "disagrees", "reason": "ocr_different_name_at_native_credit_region",
                    "ocr_credit_candidates": sorted(set(localized))}
    if expected_keys and expected_keys <= keys:
        return {"status": "agrees", "reason": "same_surname_in_explicit_credit",
                "ocr_credit_candidates": sorted({row["value"] for row in credits})}
    if expected_keys & keys:
        return {"status": "inconclusive", "reason": "only_some_author_surnames_corroborated",
                "ocr_credit_candidates": sorted({row["value"] for row in credits})}
    explicit_keys = set().union(*(_author_keys(row.get("value")) for row in explicit_credits)) if explicit_credits else set()
    if expected and explicit_keys and not expected_keys & explicit_keys:
        return {"status": "disagrees", "reason": "different_explicit_credit",
                "ocr_credit_candidates": sorted({row["value"] for row in explicit_credits})}
    if not expected and len(keys) > 1:
        return {"status": "inconclusive", "reason": "multiple_ocr_credits_work_scope_ambiguous",
                "ocr_credit_candidates": sorted({row["value"] for row in credits})}
    if not expected and keys:
        return {"status": "candidate_for_review", "reason": "ocr_explicit_credit_native_unresolved",
                "ocr_credit_candidates": sorted({row["value"] for row in credits})}
    if expected and _visible_token(author, text):
        return {"status": "inconclusive", "reason": "name_visible_without_verified_credit",
                "ocr_credit_candidates": []}
    return {"status": "inconclusive", "reason": "no_matching_credit_in_this_composition",
            "ocr_credit_candidates": []}


@review_scope
def review_author_field_map(pdf_path, *, title_hint="", author="", author_source="",
                            ocr_peer_pages=(), tesseract="", max_ocr_calls=3,
                            ocr_runner=None, recognition_effort="thorough"):
    """Bounded evidence cycle. No returned candidate changes final metadata."""
    from .context import AuthorEvidenceContext
    from .dispatcher import infer_author
    from recognition_effort import normalize_effort

    recognition_effort = normalize_effort(recognition_effort).value
    pdf_path = Path(pdf_path)
    pages = map_pdf_opening(pdf_path, title_hint)
    report = {"status": "reviewed", "mode": "shadow_v1", "title_hint": title_hint,
              "current_author": author, "current_author_source": author_source,
              "pages": pages, "checks": [], "reconsiderations": [],
              "final_review": "unresolved", "changes_final_metadata": False,
              "recognition_effort": recognition_effort}
    if not pages:
        report["status"] = "no_pages"
        return report
    current_profile = AuthorEvidenceContext.from_samples(
        [{"page": page["page"], "text": " ".join(atom["text"] for atom in page["atoms"])}
         for page in pages[:4]], title_hint=title_hint, path=pdf_path, recognition_effort=recognition_effort,
    ).profile
    report["native_profile"] = current_profile.kind
    peer_samples = [{"page": int(page.get("page") or 0), "text": str(page.get("text") or "")}
                    for page in ocr_peer_pages if 1 <= int(page.get("page") or 0) <= 4]
    if peer_samples:
        peer_context = AuthorEvidenceContext.from_samples(
            peer_samples, title_hint=title_hint, path=pdf_path, recognition_effort=recognition_effort,
        )
        peer_author = infer_author(peer_context)
        peer_text = "\n".join(sample["text"] for sample in peer_samples)
        peer_check = _check_ocr(author, peer_text, title_hint=title_hint)
        report["checks"].append({"source": "existing_ocr_candidate", **peer_check,
                                 "profile": peer_context.profile.kind,
                                 "inferred_author": peer_author.get("author") or ""})
        if peer_context.profile.kind != current_profile.kind:
            report["reconsiderations"].append({"reason": "genre_disagreement_with_ocr_peer",
                                               "native": current_profile.kind,
                                               "ocr": peer_context.profile.kind})
        if peer_author.get("author") and _author_key(peer_author["author"]) != _author_key(author):
            report["reconsiderations"].append({"reason": "author_strategy_disagreement_with_ocr_peer",
                                               "ocr_candidate": peer_author["author"]})

    # A failed OCR composition does not blacklist the page or candidate. A
    # different crop/layout mode gets another chance within the same budget.
    target = max(pages, key=lambda page: (
        max((field.get("score", 0) for field in page["field_candidates"]
             if field["role"] == "work_title"), default=0),
        any(field["role"] == "author_credit" for field in page["field_candidates"]),
        -page["page"],
    ))
    runner = ocr_runner or ocr_page_crop
    if not target["atoms"] and target.get("visual_object_count") == 0 and not peer_samples:
        report["checks"].append({"source": "targeted_ocr", "status": "not_applicable",
                                 "reason": "blank_page_no_native_or_visual_marks"})
    elif not tesseract and ocr_runner is None:
        report["checks"].append({"source": "targeted_ocr", "status": "unavailable",
                                 "reason": "tesseract_missing"})
    else:
        for crop in _focus_crops(target, author)[:max(0, max_ocr_calls)]:
            result = runner(pdf_path, target["page"], crop["fraction"], crop["psm"],
                            tesseract=tesseract)
            if result.get("status") != "ok":
                report["checks"].append({"source": "targeted_ocr", "page": target["page"],
                                         "composition": crop["composition"],
                                         "status": result.get("status") or "failed",
                                         "reason": result.get("reason") or "ocr_returned_no_text"})
                continue
            with lexical_scope():
                ocr_map = map_atoms(result.get("atoms") or [], target["page"],
                                    target["width"], target["height"], title_hint)
            comparison = _check_ocr(author, result.get("text") or "", ocr_map, title_hint,
                                    native_map=target)
            crop_sample = [{"page": target["page"], "text": result.get("text") or ""}]
            crop_context = AuthorEvidenceContext.from_samples(
                crop_sample, title_hint=title_hint, path=pdf_path, profile=current_profile, recognition_effort=recognition_effort,
            )
            parser_credit = infer_author(crop_context)
            parser_source = str(parser_credit.get("source") or "")
            text_parser_author = (parser_credit.get("author") or "") if parser_source.startswith("text_") else ""
            independent_context = AuthorEvidenceContext.from_samples(
                crop_sample, title_hint=title_hint, path=pdf_path, recognition_effort=recognition_effort,
            )
            independent_credit = infer_author(independent_context)
            independent_source = str(independent_credit.get("source") or "")
            independent_text_author = ((independent_credit.get("author") or "")
                                       if independent_source.startswith("text_") else "")
            if independent_context.profile.kind != current_profile.kind:
                report["reconsiderations"].append({
                    "reason": "genre_disagreement_with_ocr_crop",
                    "composition": crop["composition"],
                    "native": current_profile.kind,
                    "ocr": independent_context.profile.kind,
                })
            if (independent_text_author and text_parser_author
                    and _author_keys(independent_text_author) != _author_keys(text_parser_author)):
                report["reconsiderations"].append({
                    "reason": "author_strategy_changes_with_ocr_genre",
                    "composition": crop["composition"],
                    "native_genre_candidate": text_parser_author,
                    "ocr_genre_candidate": independent_text_author,
                })
            if (independent_text_author and _author_keys(author)
                    and _author_keys(independent_text_author) != _author_keys(author)):
                report["reconsiderations"].append({
                    "reason": "independent_ocr_author_strategy_disagreement",
                    "composition": crop["composition"],
                    "ocr_genre_candidate": independent_text_author,
                })
            useful_ocr_fields = [field for field in ocr_map["field_candidates"]
                                 if field["role"] in {"author_credit", "possible_person_credit", "work_title"}
                                 and (field["role"] == "work_title" or field.get("near_title"))]
            report["checks"].append({"source": "targeted_ocr", "page": target["page"],
                                     "composition": crop["composition"],
                                     "fraction": crop["fraction"],
                                     "recognized_word_count": len(result.get("atoms") or []),
                                     "candidate_fields": useful_ocr_fields[:30],
                                     "recognition": result.get("recognition") or {},
                                     "author_strategy_candidate": text_parser_author,
                                     "author_strategy_source": parser_source,
                                     "independent_ocr_profile": independent_context.profile.kind,
                                     "independent_author_strategy_candidate": independent_text_author,
                                     "independent_author_strategy_source": independent_source,
                                     **comparison})
            parser_conflict = bool(text_parser_author and _author_keys(author)
                                   and _author_keys(text_parser_author) != _author_keys(author))
            if parser_conflict:
                report["reconsiderations"].append({
                    "reason": "ocr_crop_author_strategy_disagreement",
                    "composition": crop["composition"],
                    "ocr_author": text_parser_author, "source": parser_source,
                })
            if comparison["status"] == "disagrees":
                report["reconsiderations"].append({"reason": "ocr_explicit_credit_conflicts_with_native",
                                                   "composition": crop["composition"]})
                continue
            if comparison["status"] == "agrees" and not parser_conflict:
                break
    statuses = {check.get("status") for check in report["checks"]}
    if "agrees" in statuses and "disagrees" in statuses:
        report["final_review"] = "conflicting_ocr_compositions"
    elif "agrees" in statuses and any(
        row["reason"] in {"ocr_crop_author_strategy_disagreement",
                          "independent_ocr_author_strategy_disagreement"}
        for row in report["reconsiderations"]
    ):
        report["final_review"] = "module_disagreement_review_required"
    elif "agrees" in statuses:
        report["final_review"] = "ocr_corroborates_current_author"
    elif "disagrees" in statuses:
        report["final_review"] = "ocr_disagrees_review_required"
    elif "candidate_for_review" in statuses:
        report["final_review"] = "ocr_candidate_native_unresolved"
    elif "unavailable" in statuses:
        report["final_review"] = "ocr_unavailable"
    return report
