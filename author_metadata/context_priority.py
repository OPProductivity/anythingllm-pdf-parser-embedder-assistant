"""Context-dependent priorities for specialists sharing native/OCR field evidence.

Scores are decision points, not probabilities. Region identity and authorship
are separate hypotheses: a footer or copyright region can credit an author.
"""

from __future__ import annotations

import re
from .identity import author_confidence
from .structured_credit import _names, _words
from .title_evidence import not_found


def region_scores(group, page):
    """Overlapping region hypotheses; these scores are not author scores."""
    text = group["text"]
    result = []
    if group["bbox"][1] >= page["height"] * 0.90:
        result.append(("footer", 0.80))
    if re.search(r"(?i)(?:©|\bcopyright\b|\ball rights reserved\b)", text):
        result.append(("copyright_region", 0.98))
    if group["bbox"][1] >= page["height"] * 0.68 and re.match(
        r"^\s*(?:\d{1,3}|[*†‡])\s+", text
    ):
        result.append(("footnote", 0.80))
    return result


def _key(value):
    return tuple(sorted(tuple(_words(name)) for name in _names(value)))


def rank_candidates(current, proposals, *, role_evidence=(), whole_book=False):
    """Combine expert interpretations, with situation-specific influence.

    A specialist's confidence describes its own task. Its contribution to the
    authorship decision is confidence times a contextual weight, never a veto
    merely because another region has a larger confidence value.
    """
    records = {}
    for expert, report, base in [
        ("classic_author", current, author_confidence(current)["score"])
    ] + proposals:
        if not report.get("author"):
            continue
        key = _key(report["author"])
        if not key:
            # Corporate/legacy credits retain their established path.
            continue
        row = records.setdefault(key, {"report": report, "experts": [], "base": base})
        if base > row["base"]:
            row.update(report=report, base=base)
        row["experts"].append(expert)
    current_key = _key(current.get("author", ""))
    for key, row in records.items():
        # Agreement is allowed even when specialists used the same atoms.
        agreement = 12 if len(set(row["experts"])) > 1 else 0
        contributions = []
        for evidence in role_evidence:
            if _key(evidence.get("author", "")) != key:
                continue
            role = evidence["role"]
            confidence = min(1.0, max(0.0, evidence.get("confidence", 0.0)))
            linked = "title_linked_author" in row["experts"]
            if role in {"footer", "footnote", "copyright_region"}:
                # A region label says where, not whose work this is.
                weight = 0 if linked else -3
            elif role == "editor" and whole_book:
                weight = 0
            elif role in {"translator", "editor", "foreword", "institutional_staff"}:
                # An explicit author credit at a separate title-linked region
                # outranks another occurrence of the same person's role.
                weight = -12 if linked else -100
            elif role == "reference_to_other_work":
                weight = -4 if linked else -90
            else:
                weight = 0
            contributions.append(
                {
                    **evidence,
                    "weight": weight,
                    "contribution": round(weight * confidence, 2),
                }
            )
        row["score"] = round(
            row["base"] + agreement + sum(c["contribution"] for c in contributions), 2
        )
        row["contributions"] = contributions
    ranked = sorted(records.values(), key=lambda row: row["score"], reverse=True)
    incumbent = records.get(current_key)
    selected = current
    if ranked:
        best = ranked[0]
        runner = ranked[1]["score"] if len(ranked) > 1 else 0
        if (
            best["score"] >= 80
            and best["score"] - runner >= 10
            and (
                incumbent is None
                or best is incumbent
                or best["score"] >= incumbent["score"] + 10
            )
        ):
            selected = best["report"]
        elif incumbent and incumbent["score"] < 40:
            selected = not_found()
    ledger = {
        "mode": "context_priority_v1",
        "score_kind": "heuristic_decision_points",
        "shared_inputs_allowed": True,
        "candidates": [
            {
                "author": row["report"]["author"],
                "experts": sorted(set(row["experts"])),
                "base": row["base"],
                "score": row["score"],
                "contributions": row["contributions"],
            }
            for row in ranked
        ],
    }
    if selected != current or any(row["contributions"] for row in ranked):
        selected = {**selected, "context_priority": ledger}
    return selected


def infer(context, current):
    """One bounded native-field pass. No network, OCR call or corpus lookup."""
    if context.path is None or context.profile.work_scope == "whole_issue":
        return current
    opening_text = "\n".join(str(s.get("text") or "") for s in context.opening_pages)
    if (
        current.get("author")
        and not str(current.get("source", "")).startswith("filename_")
        and not re.search(
            r"(?i)translated by|edited by|foreword by|department chair", opening_text
        )
    ):
        return current
    import fitz
    from .field_map import map_page

    proposals, roles = [], []
    try:
        with fitz.open(context.path) as document:
            for index in range(min(4, len(document))):
                page = map_page(document[index], index + 1, context.title_hint)
                fields = page["field_candidates"]
                groups = {g["id"]: g for g in page["groups"]}
                title_verified = any(
                    f["role"] == "work_title"
                    and f.get("reason") == "visible_title_overlap"
                    for f in fields
                )
                people = [
                    f
                    for f in fields
                    if f["role"] in {"author_credit", "possible_person_credit"}
                    and f.get("near_title")
                    and not f.get("competing_role")
                ]
                # Multiple separate person fields can be an incomplete roster,
                # so only the existing complete-roster specialist may merge it.
                keys = {_key(f.get("value", "")) for f in people}
                keys.discard(())
                if title_verified and len(keys) == 1:
                    for field in people[:1]:
                        names = _names(field.get("value", ""))
                        if not names:
                            continue
                        group = groups[field["group_id"]]
                        if any(
                            set(group["atom_ids"])
                            & set(groups[f["group_id"]]["atom_ids"])
                            for f in fields
                            if f["role"] in {"affiliation", "copyright"}
                        ):
                            continue
                        report = {
                            "author": ", ".join(names),
                            "source": "text_structured_opening_credit",
                            "page": index + 1,
                            "evidence": group["text"],
                            "mechanism": "title_linked_field_consensus",
                            "geometry": {"byline": group["bbox"]},
                        }
                        proposals.append(
                            (
                                "title_linked_author",
                                report,
                                94 if field["role"] == "author_credit" else 58,
                            )
                        )
                # Role specialists share these exact native lines. Keep their
                # text and page in the decision trace rather than a hard veto.
                for group in page["groups"]:
                    if group["kind"] != "extractor_line":
                        continue
                    text = group["text"]
                    for report in [current] + [item[1] for item in proposals]:
                        candidate_words = _words(report.get("author", ""))
                        if (
                            candidate_words
                            and len(_names(report["author"])) == 1
                            and " ".join(candidate_words) in " ".join(_words(text))
                        ):
                            for role, confidence in region_scores(group, page):
                                roles.append(
                                    {
                                        "author": report["author"],
                                        "role": role,
                                        "confidence": confidence,
                                        "page": index + 1,
                                        "evidence": text,
                                    }
                                )
                    match = re.fullmatch(
                        r"(?i)\s*(translated by|edited by|foreword by|series editor:?|department chair:?)\s+(.{5,90})",
                        text,
                    )
                    if match and _names(match[2]):
                        role = {
                            "translated by": "translator",
                            "edited by": "editor",
                            "foreword by": "foreword",
                        }.get(match[1].casefold(), "institutional_staff")
                        roles.append(
                            {
                                "author": ", ".join(_names(match[2])),
                                "role": role,
                                "confidence": 0.98,
                                "page": index + 1,
                                "evidence": text,
                            }
                        )
    except OSError, ValueError, RuntimeError:
        return current
    return rank_candidates(
        current,
        proposals,
        role_evidence=roles,
        whole_book=context.profile.kind == "book",
    )
