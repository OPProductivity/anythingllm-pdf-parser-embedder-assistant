"""Confirm corrupt rotated running furniture without rewriting body text."""

from collections import defaultdict
import re
import statistics

import fitz

from rag_pdf_tools import ensure_tesseract_runtime, _ocr_photographed_crop_with_layout


def margin_candidates(layout):
    rows, width, height = layout["rows"], layout["width"], layout["height"]
    long = [
        r
        for r in rows
        if sum(c.isalpha() for c in r["text"]) >= 35
        and r["x1"] - r["x0"] >= width * 0.45
    ]
    if len(long) < 12:
        return [], []
    left = statistics.median(r["x0"] for r in long)
    right = statistics.median(r["x1"] for r in long)
    outside = [
        (i, r)
        for i, r in enumerate(rows)
        if (r["x1"] < left - width * 0.025 or r["x0"] > right + width * 0.025)
        and height * 0.1 < r["y0"] < height * 0.85
    ]
    narrow = [
        (i, r)
        for i, r in outside
        if r["x1"] - r["x0"] < width * 0.06 and len(r["text"].split()) <= 3
    ]
    if len(narrow) < len(outside) * 0.8:
        return [], []
    candidates = []
    for side in ("left", "right"):
        lane = sorted(
            [
                (i, r)
                for i, r in narrow
                if ("left" if r["x1"] < left else "right") == side
            ],
            key=lambda ir: ir[1]["y0"],
        )
        groups = []
        for item in lane:
            if not groups or item[1]["y0"] - groups[-1][-1][1]["y1"] > height * 0.023:
                groups.append([item])
            else:
                groups[-1].append(item)
        for group in groups:
            values = [r for _, r in group]
            box = [
                min(r["x0"] for r in values),
                min(r["y0"] for r in values),
                max(r["x1"] for r in values),
                max(r["y1"] for r in values),
            ]
            if (
                len(group) < 4
                or box[3] - box[1] < height * 0.08
                or box[2] - box[0] >= width * 0.06
                or not (box[2] <= width * 0.15 or box[0] >= width * 0.85)
            ):
                continue
            candidates.append(
                {
                    "row_indices": [i for i, _ in group],
                    "side": side,
                    "bbox": box,
                    "normalized_bbox": [
                        box[0] / width,
                        box[1] / height,
                        box[2] / width,
                        box[3] / height,
                    ],
                }
            )
    numbers = [
        {
            "row_index": i,
            "value": int(r["text"]),
            "side": "left" if r["x1"] < left else "right",
            "normalized_y": r["y0"] / height,
            "center_x": (r["x0"] + r["x1"]) / 2,
        }
        for i, r in outside
        if re.fullmatch(r"\d{1,4}", r["text"])
    ]
    return candidates, numbers


def recognize_margin(pdf_path, page_number, bbox, runtime):
    try:
        from PIL import Image, ImageOps

        with fitz.open(pdf_path) as document:
            page = document[page_number - 1]
            clip = (
                fitz.Rect(bbox[0] - 3, bbox[1] - 3, bbox[2] + 3, bbox[3] + 3)
                & page.rect
            )
            pix = page.get_pixmap(matrix=fitz.Matrix(4, 4), clip=clip, alpha=False)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        alternatives = []
        for angle in (90, 270):
            measurement = {}
            _, words = _ocr_photographed_crop_with_layout(
                image.rotate(angle, expand=True),
                (0, 0, 1, 1),
                runtime["executable"],
                ImageOps,
                psm=7,
                recognition_evidence=measurement,
            )
            if measurement.get("subprocess_outcome") in (
                "timeout",
                "launch_error",
                "nonzero_exit",
            ):
                return [], "ocr_failed"
            text = " ".join(r["text"] for r in words)
            weight = sum(len(r["text"]) for r in words)
            confidence = sum(r["confidence"] * len(r["text"]) for r in words) / max(
                1, weight
            )
            alternatives.append(
                {
                    "text": text,
                    "confidence": round(confidence, 3),
                    "angle": angle,
                    "subprocess_seconds": measurement.get("subprocess_seconds", 0),
                }
            )
        return alternatives, "observed"
    except ImportError, OSError, RuntimeError, ValueError:
        return [], "ocr_unavailable"


def plan_rotated_running_furniture(
    pdf_path, layouts, *, progress_callback=None, max_candidate_pages=64
):
    plans = {}
    for number, layout in layouts.items():
        candidates, numbers = margin_candidates(layout)
        if candidates:
            plans[number] = {
                "status": "unconfirmed",
                "candidates": candidates,
                "number_candidates": numbers,
                "excluded_row_indices": [],
                "removed": [],
                "confirmed_labels": {},
                "page_number_sequences": {},
                "unresolved_candidate_count": len(candidates),
                "unresolved_page_number_count": 0,
            }
    if not plans:
        return plans
    runtime = ensure_tesseract_runtime()
    if not runtime.get("available"):
        for plan in plans.values():
            plan["status"] = "ocr_unavailable"
        return plans
    observations = []
    failed = False
    for index, (number, plan) in enumerate(plans.items()):
        if failed or index >= max_candidate_pages:
            plan["status"] = "ocr_failed" if failed else "candidate_page_budget_reached"
            continue
        for candidate in plan["candidates"]:
            alternatives, status = recognize_margin(
                pdf_path, number, candidate["bbox"], runtime
            )
            candidate.update(ocr_status=status, ocr_alternatives=alternatives)
            if status != "observed":
                plan["status"] = status
                failed = True
                break
            for alternative in alternatives:
                text = alternative["text"]
                if (
                    sum(c.isalpha() for c in text) < 10
                    or not 2 <= len(text.split()) <= 12
                    or len(text) > 100
                    or re.search(r"[.!?]$", text)
                ):
                    continue
                observations.append(
                    {"page": number, "candidate": candidate, **alternative}
                )
        if progress_callback:
            progress_callback(number, len(layouts), "rotated_margin_confirmation")
    seeds = defaultdict(list)
    for item in observations:
        if item["confidence"] >= 75:
            key = (item["text"].casefold(), item["candidate"]["side"], item["angle"])
            seeds[key].append(item)
    confirmed = defaultdict(list)
    for item in observations:
        key = (item["text"].casefold(), item["candidate"]["side"], item["angle"])
        # Repetition includes stable placement, not merely a repeated phrase.
        support = sorted(
            {
                v["page"]
                for v in seeds[key]
                if max(
                    abs(a - b)
                    for a, b in zip(
                        v["candidate"]["normalized_bbox"],
                        item["candidate"]["normalized_bbox"],
                    )
                )
                <= 0.04
            }
        )
        if item["confidence"] < 60 or len(support) < 2:
            continue
        number, candidate = item["page"], item["candidate"]
        plan = plans[number]
        if any(i in plan["excluded_row_indices"] for i in candidate["row_indices"]):
            continue
        plan["status"] = "confirmed"
        plan["excluded_row_indices"].extend(candidate["row_indices"])
        label_id = "label-" + str(len(plan["confirmed_labels"]) + 1)
        plan["confirmed_labels"][label_id] = {
            "recognized_label": item["text"],
            "confidence": item["confidence"],
            "rotation": item["angle"],
            "supporting_pages": support,
            "bbox": candidate["bbox"],
            "side": candidate["side"],
        }
        for i in candidate["row_indices"]:
            row = layouts[number]["rows"][i]
            plan["removed"].append(
                {
                    "text": row.get("normalized", row["text"]),
                    "reason": "ocr_confirmed_repeated_rotated_running_label",
                    "bbox": [row[k] for k in ("x0", "y0", "x1", "y1")],
                    "evidence_ref": "rotated_running_furniture.confirmed_labels."
                    + label_id,
                }
            )
        confirmed[number].append(candidate)
    number_groups = defaultdict(list)
    associated_numbers = defaultdict(set)
    for number, candidates in confirmed.items():
        for item in plans[number]["number_candidates"]:
            if any(
                item["side"] == c["side"]
                and abs(item["center_x"] - (c["bbox"][0] + c["bbox"][2]) / 2)
                <= layouts[number]["width"] * 0.08
                for c in candidates
            ):
                number_groups[item["value"] - number].append({"page": number, **item})
                associated_numbers[number].add(item["row_index"])
    for offset, values in number_groups.items():
        for item in values:
            support = sorted(
                {
                    v["page"]
                    for v in values
                    if abs(v["normalized_y"] - item["normalized_y"]) <= 0.03
                }
            )
            if len(support) < 3:
                continue
            number, i = item["page"], item["row_index"]
            row = layouts[number]["rows"][i]
            sequence_id = "offset-" + str(offset)
            plans[number]["page_number_sequences"][sequence_id] = {
                "printed_page_offset": offset,
                "supporting_pages": support,
                "normalized_y": item["normalized_y"],
            }
            plans[number]["excluded_row_indices"].append(i)
            plans[number]["removed"].append(
                {
                    "text": row.get("normalized", row["text"]),
                    "reason": "confirmed_outer_lane_page_number_sequence",
                    "bbox": [row[k] for k in ("x0", "y0", "x1", "y1")],
                    "evidence_ref": "rotated_running_furniture.page_number_sequences."
                    + sequence_id,
                }
            )
    for number, plan in plans.items():
        plan["excluded_row_indices"] = sorted(set(plan["excluded_row_indices"]))
        plan["unresolved_candidate_count"] = sum(
            not set(c["row_indices"]).issubset(plan["excluded_row_indices"])
            for c in plan["candidates"]
        )
        plan["unresolved_page_number_count"] = len(
            associated_numbers[number] - set(plan["excluded_row_indices"])
        )
        if plan["status"] == "confirmed" and (
            plan["unresolved_candidate_count"] or plan["unresolved_page_number_count"]
        ):
            plan["status"] = "partial_confirmation"
    return plans
