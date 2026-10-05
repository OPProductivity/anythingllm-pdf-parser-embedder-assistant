"""Conservative page-local column evidence for native PDF text lines."""

import re
import statistics

from rag_pdf_tools import normalize_text


def _prose(row, width):
    text = row["text"]
    return (len(re.findall(r"[^\W\d_]", text, re.UNICODE)) >= 25
            and len(text.split()) >= 5
            and row["x1"] - row["x0"] >= width * .20)


def _coverage(rows):
    return max(row["y1"] for row in rows) - min(row["y0"] for row in rows) if rows else 0


def _body_prose(rows, width, height):
    return [row for row in rows if _prose(row, width) and height * .08 < row["y0"] < height * .91]


def _three_prose_tracks(rows, width, height):
    ordered = sorted(_body_prose(rows, width, height), key=lambda row: row["x0"])
    bands = []
    for row in ordered:
        if not bands or row["x0"] - bands[-1][-1]["x0"] > width * .095:
            bands.append([row])
        else:
            bands[-1].append(row)
    substantial = [band for band in bands if len(band) >= 8 and _coverage(band) >= height * .20]
    if len(substantial) < 3:
        return False
    tracks = substantial[:3]
    starts = [statistics.median(row["x0"] for row in band) for band in tracks]
    if not all(width * .14 < right - left < width * .38 for left, right in zip(starts, starts[1:])):
        return False
    shared = min(max(row["y1"] for row in band) for band in tracks) - max(
        min(row["y0"] for row in band) for band in tracks
    )
    return shared >= height * .18


def _measured_gutter(rows, width, height):
    prose = _body_prose(rows, width, height)
    full = [row for row in prose if row["x1"] - row["x0"] >= width * .55]
    if len(full) >= 8 and len(full) >= len(prose) * .25:
        return None
    proposals = []
    for split in sorted({row["x0"] for row in prose}):
        if not width * .30 <= split <= width * .70:
            continue
        left = [row for row in prose if row["x1"] <= split - width * .005]
        right = [row for row in prose if row["x0"] >= split]
        if min(len(left), len(right)) < 8 or min(_coverage(left), _coverage(right)) < height * .23:
            continue
        shared_top = max(min(row["y0"] for row in left), min(row["y0"] for row in right))
        shared_bottom = min(max(row["y1"] for row in left), max(row["y1"] for row in right))
        overlap = shared_bottom - shared_top
        if overlap < height * .18:
            continue
        inner_left = max(row["x1"] for row in left)
        inner_right = min(row["x0"] for row in right)
        gutter = inner_right - inner_left
        if gutter < width * .006:
            continue
        crossing = [row for row in prose if row["y0"] < shared_bottom and row["y1"] > shared_top
                    and row["x0"] < inner_left and row["x1"] > inner_right]
        if len(crossing) > max(2, len(prose) * .08):
            continue
        proposals.append((min(len(left), len(right)), overlap, gutter, inner_left, inner_right))
    if not proposals:
        return None
    _, _, _, left_edge, right_edge = max(proposals)
    return (left_edge + right_edge) / 2


def _sustained_single_column(rows, width, height):
    full = [row for row in _body_prose(rows, width, height)
            if row["x1"] - row["x0"] >= width * .55]
    thirds = [sum(lower * height <= row["y0"] < upper * height for row in full)
              for lower, upper in ((.08, .35), (.35, .60), (.60, .91))]
    return len(full) >= 8 and min(thirds) >= 3


def _join_inline(rows, width):
    groups = []
    for row in sorted(rows, key=lambda item: (item["y0"], item["x0"])):
        match = None
        for group in groups[-3:]:
            first = group[0]
            height = max(first["y1"] - first["y0"], row["y1"] - row["y0"])
            if abs(row["y0"] - first["y0"]) <= min(3.5, height * .25):
                match = group
                break
        if match is None:
            groups.append([row])
        else:
            match.append(row)
    runs = []
    for group in groups:
        group.sort(key=lambda row: row["x0"])
        run = [group[0]]
        for row in group[1:]:
            previous = run[-1]
            size = statistics.median(previous.get("font_sizes") or row.get("font_sizes") or [10])
            gap = row["x0"] - previous["x1"]
            if -size * .25 <= gap <= min(width * .035, size * 1.5):
                run.append(row)
            else:
                runs.append(run)
                run = [row]
        runs.append(run)
    merged = []
    for run in runs:
        if len(run) == 1:
            merged.append(run[0])
            continue
        joined_text = run[0]["text"]
        for previous, following in zip(run, run[1:]):
            left, right = joined_text.rstrip(), following["text"].lstrip()
            gap = following["x0"] - previous["x1"]
            size = statistics.median(previous.get("font_sizes") or following.get("font_sizes") or [10])
            if left.endswith(("(", "[", "{", "\u201c", "\u2018")) or right.startswith((")", "]", "}", "\u201d", "\u2019", ",", ".", ";", ":", "!", "?")):
                separator = ""
            elif (not joined_text[-1].isspace() and not following["text"][:1].isspace()
                  and gap <= max(1, size * .14) and left[-1:].isalnum() and right[:1].isalnum()):
                separator = ""
            else:
                separator = " "
            joined_text = left + separator + right
        merged.append({
            **run[0], "text": joined_text, "normalized": normalize_text(joined_text),
            "x0": min(row["x0"] for row in run), "x1": max(row["x1"] for row in run),
            "y0": min(row["y0"] for row in run), "y1": max(row["y1"] for row in run),
            "font_sizes": [size for row in run for size in row.get("font_sizes", [])],
            "fonts": [font for row in run for font in row.get("fonts", [])],
            "spans": [span for row in run for span in row.get("spans", [])],
            "native_ligature_space_repairs": sum(row.get("native_ligature_space_repairs", 0) for row in run),
        })
    return sorted(merged, key=lambda row: (row["y0"], row["x0"]))


def verified_page_order(rows, width, height, baseline, baseline_kind):
    """Change order only for measured columns or sustained single-column prose."""
    if _three_prose_tracks(rows, width, height):
        return baseline, baseline_kind, "three_column_layout_needs_review", "three_column"
    split = _measured_gutter(rows, width, height)
    if split is not None and baseline_kind == "two_column_column_first":
        left = [row for row in rows if row["x1"] <= split]
        right = [row for row in rows if row["x0"] >= split]
        other = [row for row in rows if row not in left and row not in right]
        first_body = min(row["y0"] for row in right)
        preamble = [row for row in rows if row["y0"] < first_body]
        def key(row):
            return row["y0"], row["x0"]
        ordered = (sorted(preamble, key=key)
                   + sorted((row for row in other if row not in preamble), key=key)
                   + sorted((row for row in left if row not in preamble), key=key)
                   + sorted((row for row in right if row not in preamble), key=key))
        return ordered, baseline_kind, "measured_column_gutter", "two_column"
    if split is None and _sustained_single_column(rows, width, height):
        merged = _join_inline(rows, width)
        if "\n".join(row["text"] for row in merged) != "\n".join(row["text"] for row in baseline):
            return merged, "visual_line_order", "sustained_single_column_inline_join", "single_column"
    return baseline, baseline_kind, "", "unresolved"
