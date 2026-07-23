"""Deterministic local boundary scoring and page-transition detection.

This module makes local splitting repeatable from extracted page text and a
``segmentation_policy``.  It does not inspect or alter AnythingLLM's later
internal splitter, and callers must not infer final native-vector boundaries
from its output alone.  Keep scoring decisions explainable: a candidate
boundary should carry a concrete textual reason rather than a model-only
prediction that cannot be reconstructed in a run artifact.
"""

from __future__ import annotations

import hashlib
import re

from segmentation_policy import policy_for


ABBREVIATIONS = {"u.s.", "u.k.", "dr.", "mr.", "mrs.", "ms.", "prof.", "ed.", "eds.", "vol.", "no.", "fig."}


def _candidate_kind(text, position):
    before = text[:position].rstrip()
    after = text[position:].lstrip()
    if before.endswith("\n\n"):
        return "paragraph", 100
    token = re.search(r"([A-Za-z.]+)$", before)
    if before.endswith((".", "?", "!")) and (not token or token.group(1).casefold() not in ABBREVIATIONS):
        return "sentence", 80
    if before.endswith((";", ":", "—", "–")):
        return "clause", 48
    if before.endswith(","):
        return "comma", 25
    if before and after:
        return "whitespace", 5
    return "fallback", 0


def boundary_candidates(text, start, target, hard_limit, drift):
    preferred_min = max(start + 1, start + int(target * (1 - drift)))
    preferred_max = min(len(text), start + int(target * (1 + drift)), start + hard_limit)
    scan_max = min(len(text), start + hard_limit)
    points = {scan_max}
    for match in re.finditer(r"\n\n|[.!?;:,—–]\s+|\s+", text[start:scan_max]):
        point = start + match.end()
        if point > start:
            points.add(point)
    rows = []
    for point in sorted(points):
        left = text[start:point].strip()
        right = text[point:scan_max].strip()
        if not left:
            continue
        kind, strength = _candidate_kind(text, point)
        distance = abs(len(left) - target) / max(target, 1)
        score = strength - distance * 35
        positives = [kind] if strength else []
        penalties = []
        if point < preferred_min or point > preferred_max:
            score -= 18
            penalties.append("outside_preferred_drift")
        if left.endswith("-") and re.search(r"[A-Za-z]-$", left):
            score -= 90
            penalties.append("hyphen_carry_over")
        if right and right[0].islower():
            score -= 35
            penalties.append("lowercase_continuation")
        if re.fullmatch(r"[\s\d()[\]:;,.–—-]+", right or ""):
            score -= 45
            penalties.append("citation_only_tail")
        rows.append({
            "position": point,
            "kind": kind,
            "score": round(score, 3),
            "positive_signals": positives,
            "penalties": penalties,
            "distance_fraction": round(distance, 4),
        })
    return rows


def _last_complete_sentence_span(text):
    """Return the final complete sentence span on a page, ignoring trailing fragments."""
    matches = list(re.finditer(r"(?s)(\S.*?[.!?][\"'”’)]?)(?=\s+|$)", text or ""))
    if not matches:
        return None
    match = matches[-1]
    return match.start(1), match.end(1)


def _retain_last_complete_sentence(text, result, target, hard_limit, policy):
    """Repair the final split so the last complete page sentence survives in the final segment.

    This is deliberately page-local and bounded: it only moves the final cut inside
    the already-produced final pair, never crosses the page, and never violates the
    hard limit. It is a scoring repair for weak dangling tails, not an absolute rule.
    """
    if len(result) < 2:
        return result
    span = _last_complete_sentence_span(text)
    if not span:
        return result
    sentence_start, sentence_end = span
    final = result[-1]
    if final["char_start_page"] <= sentence_start and final["char_end_page"] >= sentence_end:
        return result
    affected_index = None
    for index, row in enumerate(result):
        if row["char_start_page"] <= sentence_start < row["char_end_page"]:
            affected_index = index
            break
    if affected_index is None or affected_index == len(result) - 1:
        return result
    affected = result[affected_index]
    combined_start = affected["char_start_page"]
    combined_end = final["char_end_page"]
    if not (combined_start <= sentence_start < sentence_end <= combined_end):
        return result
    right_raw = text[sentence_start:combined_end]
    left_raw = text[combined_start:sentence_start]
    left = left_raw.strip()
    right = right_raw.strip()
    if not right:
        return result
    if len(right) > hard_limit or (left and len(left) > hard_limit):
        return result
    if left and len(left) < target * max(0.20, policy.small_tail_fraction / 2):
        return result
    right_leading = len(right_raw) - len(right_raw.lstrip())
    replacement = []
    if left:
        replacement.append({
            "text": left,
            "char_start_page": combined_start,
            "char_end_page": combined_start + len(left_raw.rstrip()),
            "boundary_debug": {
                **affected.get("boundary_debug", {}),
                "page_end_retention_repaired": True,
                "reason": "moved final suffix cut to retain last complete page sentence",
            },
        })
    replacement.append({
        "text": right,
        "char_start_page": sentence_start + right_leading,
        "char_end_page": combined_end,
        "boundary_debug": {
            **final.get("boundary_debug", {}),
            "page_end_retention_repaired": True,
            "retained_last_complete_sentence": True,
            "reason": "retained last complete page sentence in final segment",
        },
    })
    return result[:affected_index] + replacement


def split_semantic_page(text, target, hard_limit, mode="page_limit", diagnostic=False):
    if not text:
        return []
    policy = policy_for(mode)
    target = max(1, int(target))
    hard_limit = max(1, int(hard_limit or target))
    if mode == "none":
        # The caller explicitly asked for a single prepared content record.
        # This does not constrain the downstream AnythingLLM splitter.
        return [{
            "text": text.strip(),
            "char_start_page": len(text) - len(text.lstrip()),
            "char_end_page": len(text.rstrip()),
            "boundary_debug": {"reason": "no_local_segmentation"},
        }]
    if mode in {"page", "page_limit"} and len(text) <= hard_limit:
        return [{
            "text": text.strip(),
            "char_start_page": 0,
            "char_end_page": len(text.rstrip()),
            "boundary_debug": {"reason": "whole_page_within_safety_ceiling"},
        }]
    result = []
    start = 0
    while start < len(text):
        # Page-preserving automatic mode uses the target only to make a
        # high-quality safety split when a page is too large.  The explicit
        # page_passages mode, in contrast, applies its target within each
        # source page while still enforcing the same hard ceiling.
        remaining = len(text) - start
        target_driven_split = mode == "page_passages" and remaining > target
        if remaining <= hard_limit and not target_driven_split:
            end = len(text)
            candidates = []
            winner = {"kind": "page_end", "score": 100, "positive_signals": ["page_end"], "penalties": []}
        else:
            candidates = boundary_candidates(text, start, target, hard_limit, policy.target_drift_fraction)
            for candidate in candidates:
                semantic_component = {
                    "paragraph": 100,
                    "sentence": 80,
                    "clause": 48,
                    "comma": 25,
                    "whitespace": 5,
                    "fallback": 0,
                }.get(candidate["kind"], 0)
                candidate["score"] = round(
                    candidate["score"]
                    + semantic_component * (policy.semantic_priority - 1.0)
                    - candidate["distance_fraction"] * 55 * policy.size_uniformity_priority,
                    3,
                )
            winner = max(candidates, key=lambda row: (row["score"], row["position"]))
            end = winner["position"]
        raw = text[start:end]
        left_trim = len(raw) - len(raw.lstrip())
        right_trim = len(raw.rstrip())
        piece = raw.strip()
        if piece:
            debug = {
                "winner_position": end,
                "winner_kind": winner["kind"],
                "positive_signals": winner["positive_signals"],
                "penalties": winner["penalties"],
                "reason": f"selected {winner['kind']} boundary",
            }
            if diagnostic:
                debug["candidates"] = candidates
            result.append({
                "text": piece,
                "char_start_page": start + left_trim,
                "char_end_page": start + right_trim,
                "boundary_debug": debug,
            })
        start = max(end, start + 1)
        while start < len(text) and text[start].isspace():
            start += 1

    # Rebalance a tiny final sibling by re-splitting the combined final pair.
    if len(result) >= 2 and len(result[-1]["text"]) < target * policy.small_tail_fraction:
        combined_start = result[-2]["char_start_page"]
        combined_end = result[-1]["char_end_page"]
        combined = text[combined_start:combined_end]
        desired = max(1, len(combined) // 2)
        candidates = boundary_candidates(combined, 0, desired, hard_limit, policy.target_drift_fraction)
        legal = [row for row in candidates if row["position"] < len(combined) and len(combined) - row["position"] <= hard_limit]
        if legal:
            winner = max(legal, key=lambda row: row["score"])
            cut = winner["position"]
            left = combined[:cut].strip()
            right = combined[cut:].strip()
            if left and right and len(left) <= hard_limit and len(right) <= hard_limit:
                result[-2] = {
                    "text": left,
                    "char_start_page": combined_start,
                    "char_end_page": combined_start + len(combined[:cut].rstrip()),
                    "boundary_debug": {
                        "winner_kind": winner["kind"],
                        "positive_signals": winner["positive_signals"],
                        "penalties": winner["penalties"],
                        "rebalanced": True,
                        "reason": "rebalanced tiny final sibling",
                    },
                }
                right_leading = len(combined[cut:]) - len(combined[cut:].lstrip())
                result[-1] = {
                    "text": right,
                    "char_start_page": combined_start + cut + right_leading,
                    "char_end_page": combined_end,
                    "boundary_debug": {"rebalanced": True, "reason": "rebalanced tiny final sibling"},
                }
    return _retain_last_complete_sentence(text, result, target, hard_limit, policy)


def detect_page_transition(left_text, right_text, left_page, right_page, source_label="document"):
    left = (left_text or "").strip()
    right = (right_text or "").strip()
    boundary_id = f"{source_label}-b{int(left_page):03d}-{int(right_page):03d}-tr01"
    evidence = []
    score = 0
    left_fragment = re.split(r"(?<=[.!?])\s+", left)[-1] if left else ""
    right_fragment = re.split(r"(?<=[.!?])\s+", right, maxsplit=1)[0] if right else ""
    if left_fragment and not re.search(r"[.!?][\"'”’)]?$", left_fragment):
        score += 2
        evidence.append("left_without_terminal_punctuation")
    if right_fragment and (right_fragment[0].islower() or right_fragment.startswith(("’", "”", "'", '"'))):
        score += 2
        evidence.append("right_continuation_start")
    if re.search(r"[A-Za-z]-$", left_fragment) and re.match(r"[a-z]", right_fragment):
        score += 3
        evidence.append("hyphenated_lexical_continuation")
    if re.match(r"(?i)(chapter|part|notes|references|bibliography|index)\b", right_fragment):
        score -= 4
        evidence.append("right_heading_or_end_matter")
    detected = score >= 3
    if re.search(r"[A-Za-z]-$", left_fragment) and re.match(r"[a-z]", right_fragment):
        reconstructed = left_fragment[:-1] + right_fragment
    else:
        reconstructed = (left_fragment + " " + right_fragment).strip()
    return {
        "schema_version": 1,
        "boundary_id": boundary_id,
        "left_page": int(left_page),
        "right_page": int(right_page),
        "left_span": [max(0, len(left) - len(left_fragment)), len(left)],
        "right_span": [0, len(right_fragment)],
        "left_fragment": left_fragment,
        "right_fragment": right_fragment,
        "reconstructed_text": reconstructed if detected else "",
        "continuation_detected": detected,
        "confidence_score": score,
        "evidence": evidence,
        "sentence_hash": hashlib.sha256(reconstructed.encode("utf-8")).hexdigest() if detected else "",
        "upload_eligible": False,
    }
