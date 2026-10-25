"""Publishing-act evidence for bounded, revisable author attribution.

No corpus labels, filenames, external lookup or new OCR. Shared cues remain
available to different experts; each assertion records work, role and occurrence.
"""

import re
from .structured_credit import _names, _words
from .title_evidence import not_found

_RIGHTS = (
    re.compile(
        r"\bThe rights? of (.{5,180}?) to be identified as (?:the )?authors? of this work",
        re.I,
    ),
    re.compile(
        r"\bThe rights? of (.{5,180}?) as authors? ha(?:s|ve) been asserted", re.I
    ),
)


def frontmatter_samples(context):
    """Acquire first five native pages, plus a bounded delayed-frontmatter request."""
    samples = {
        int(s.get("page") or 0): dict(s)
        for s in context.samples
        if 1 <= int(s.get("page") or 0) <= 8
    }
    acquired = []
    native_pages = {
        int(s.get("page") or 0): s
        for s in (context.evidence_bundle or {}).get("native", [])
    }
    if context.path is not None:
        import fitz

        try:
            with fitz.open(context.path) as doc:
                for index in range(min(5, len(doc))):
                    page_evidence = native_pages.get(index + 1)
                    native = (
                        page_evidence["text"]
                        if page_evidence is not None and "text" in page_evidence
                        else doc[index].get_text("text", sort=True)
                    )
                    if (
                        index + 1 not in samples
                        or samples[index + 1].get("text") != native
                    ):
                        acquired.append(index + 1)
                    samples[index + 1] = {"page": index + 1, "text": native}
                # A visible navigation page justifies looking for its delayed title/rights,
                # rather than increasing every document's sample window indefinitely.
                opening = "\n".join(s["text"] for s in samples.values())
                delayed = context.profile.kind == "book" and bool(
                    re.search(
                        r"(?im)^\s*(?:contents|title page|endorsements)\s*$", opening
                    )
                )
                if delayed and not any(
                    pattern.search(" ".join(opening.split())) for pattern in _RIGHTS
                ):
                    for index in range(5, min(8, len(doc))):
                        if index + 1 not in samples:
                            samples[index + 1] = {
                                "page": index + 1,
                                "text": doc[index].get_text("text"),
                            }
                            acquired.append(index + 1)
        except OSError, ValueError, RuntimeError:
            pass
    return tuple(samples[k] for k in sorted(samples)), acquired


def rights_claims(samples):
    claims = []
    for sample in samples:
        text = " ".join(str(sample.get("text") or "").split())
        # A line beginning a quotation about another named work is not a current
        # responsibility statement. Require an actual publication-rights context.
        if not re.search(
            r"\b(?:ISBN|copyright|all rights reserved|copyright act)\b|©", text, re.I
        ):
            continue
        for pattern in _RIGHTS:
            for match in pattern.finditer(text):
                names = _names(match[1])
                if names:
                    claims.append(
                        {
                            "author": ", ".join(names),
                            "role": "author",
                            "work": "this work",
                            "speaker": "publication rights statement",
                            "page": int(sample["page"]),
                            "evidence": match[0],
                        }
                    )
    return claims


def parent_editor_credit(report):
    return report.get("source") == "text_edited_by" or bool(
        re.search(r"\bedited\s+by\b", report.get("evidence", ""), re.I)
    )


def embedded_chapter(context, current, *, parent_editor_established=False):
    """A local chapter plus repeated author running heads outranks parent editors.

    Examine physical leaves separately, so a preceding bibliography on the left
    cannot be joined to a right-leaf chapter byline.
    """
    parent_editor = parent_editor_credit(current) or parent_editor_established
    if (
        context.path is None
        or not parent_editor
        or context.profile.work_scope == "whole_issue"
    ):
        return None
    import fitz

    try:
        with fitz.open(context.path) as doc:
            hits = []
            for index in range(1, min(5, len(doc) - 2)):
                page = doc[index]
                r = page.rect
                leaves = (
                    [r]
                    if r.width < r.height * 1.15
                    else [
                        fitz.Rect(0, 0, r.width / 2, r.height),
                        fitz.Rect(r.width / 2, 0, r.width, r.height),
                    ]
                )
                for leaf in leaves:
                    text = page.get_text("text", clip=leaf, sort=True)
                    lines = [
                        " ".join(v.split()) for v in text.splitlines() if v.strip()
                    ]
                    marker = next(
                        (
                            i
                            for i, v in enumerate(lines[:20])
                            if re.search(r"\bCHAPTER\s*\d{1,3}\b", v)
                        ),
                        None,
                    )
                    if marker is None:
                        continue
                    for i in range(marker + 2, min(marker + 16, len(lines))):
                        names = _names(lines[i])
                        if len(names) != 1:
                            continue
                        key = _words(names[0])
                        corroboration = []
                        for following in (index + 1, index + 2):
                            header = _words(
                                doc[following].get_text(
                                    "text",
                                    clip=fitz.Rect(
                                        0,
                                        0,
                                        doc[following].rect.width,
                                        doc[following].rect.height * 0.13,
                                    ),
                                    sort=True,
                                )
                            )
                            if any(
                                header[j : j + len(key)] == key
                                for j in range(len(header) - len(key) + 1)
                            ):
                                corroboration.append(following + 1)
                        if len(corroboration) == 2:
                            hits.append(
                                {
                                    "author": names[0],
                                    "source": "text_strict_credit_block",
                                    "page": index + 1,
                                    "evidence": " / ".join(lines[marker : i + 1]),
                                    "mechanism": "local_chapter_credit_with_repeated_running_author",
                                    "corroboration_pages": corroboration,
                                    "geometry": {"chapter_leaf": list(leaf)},
                                }
                            )
            keys = {_words(h["author"]) for h in hits}
            return hits[0] if len(keys) == 1 else None
    except OSError, ValueError, RuntimeError:
        return None


def reconcile(context, current):
    """Compare current attribution with explicit current-work responsibility.

    Region labels cannot veto authorship. A different current-work author-rights
    assertion can challenge an accepted name, but conflicting assertions defer.
    """
    if context.profile.work_scope == "whole_issue":
        return current
    chapter = embedded_chapter(context, current)
    if chapter:
        return {
            **chapter,
            "publishing_logic": {
                "mode": "publishing_acts_v1",
                "shared_inputs_allowed": True,
                "previous_author": current.get("author", ""),
                "previous_source": current.get("source", ""),
                "genre_before": context.profile.kind,
                "genre_hypothesis": "book_chapter",
                "verdict": "local chapter byline with two following author running heads outranks parent editors",
            },
        }
    samples, acquired = frontmatter_samples(context)
    claims = rights_claims(samples)
    trace = {
        "mode": "publishing_acts_v1",
        "shared_inputs_allowed": True,
        "score_kind": "qualitative_priority_not_probability",
        "acquired_native_pages": acquired,
        "claims": claims,
        "previous_author": current.get("author", ""),
        "previous_source": current.get("source", ""),
    }
    keys = {tuple(tuple(_words(n)) for n in _names(c["author"])) for c in claims}
    eligible = (
        context.profile.kind in {"book", "generic"}
        and context.profile.work_scope != "chapter"
    )
    if claims and eligible and len(keys) == 1:
        claim = claims[0]
        report = {
            "author": claim["author"],
            "source": "text_work_rights_credit",
            "page": claim["page"],
            "evidence": claim["evidence"],
            "mechanism": "explicit_current_work_author_rights",
        }
        trace["verdict"] = (
            "explicit current-work author responsibility outranks region location and unsupported name shape"
        )
        trace["genre_before"] = context.profile.kind
        trace["genre_hypothesis"] = "book"
        return {**report, "publishing_logic": trace}
    if claims and len(keys) > 1:
        trace["verdict"] = (
            "conflicting author-rights claims; retain incumbent and defer arbitration"
        )
    elif claims and not eligible:
        trace["verdict"] = (
            "parent rights do not settle selected chapter or other contribution"
        )
    else:
        trace["verdict"] = (
            "no applicable explicit author-rights claim; retain incumbent"
        )
    # Challenge only an exact numbered heading selected through a weak text
    # guess. Do not treat affiliation superscripts or arbitrary digits as a veto.
    weak = current.get("source") in {
        "text_opening_title_block_byline",
        "text_title_adjacent_byline",
        "text_title_window_adjacent_byline",
    }
    if weak and current.get("author"):
        target = tuple(w for w in _words(current["author"]) if w != "and")
        for sample in samples:
            match = next(
                (
                    m
                    for m in re.finditer(
                        r"(?m)^\s*\d{1,2}\.\s+([^\n]{5,110})", sample["text"]
                    )
                    if tuple(w for w in _words(m[1]) if w != "and") == target
                ),
                None,
            )
            if (
                match
                and "?" in sample["text"][: match.start()]
                and len(sample["text"][match.end() :].split()) >= 60
            ):
                trace.update(
                    verdict="teaching-question box followed by exact numbered section heading; defer author rather than promote heading",
                    heading_page=int(sample["page"]),
                    heading=match[0].strip(),
                )
                return {**not_found(), "publishing_logic": trace}
    return {**current, "publishing_logic": trace}
