"""Collect all scheduled evidence, consult specialists, then adjudicate roles."""

from dataclasses import replace
import re
from .constants import TRUSTED_AUTHOR_INFERENCE_SOURCES
from .evidence_collection import collect, evidence_views
from .publishing_relations import claims, select, running_chapter_claims
from .structured_credit import _words


def _key(report):
    return _words(report.get("author", ""))


def infer(context):
    from . import dispatcher as strategies
    from .bounded_ocr import bundle_reports
    from .scholarly_article import _editorial_end_signature
    from .physical_work import opening_title
    from .profile import classify_document
    from .publishing_acts import embedded_chapter

    context = collect(context)
    views = evidence_views(context)
    proposals, attempts, assertions, regions = [], [], [], []
    # The historical selector is one expert, not the final verdict. Its internal
    # fallback order cannot prevent the following independent expert calls.
    current = strategies._infer_before_collection(context)
    experts = [
        ("document_record", strategies.document_record),
        ("native_article", strategies.infer_native_article),
        ("credential_roster", strategies.credential_roster),
        ("structured_credit", strategies.infer_structured_credit),
        ("positioned_opening", strategies.positioned_opening),
        ("publisher_titlepage", strategies.publisher_titlepage),
        ("work_rights", strategies.work_rights_credit),
        ("whole_book_editors", strategies.whole_book_editors),
        ("continued_byline", strategies.continued_byline),
        ("biography", strategies.biographic_titlepage),
        ("portable_book", strategies.infer_portable_book),
        ("portable_layout", strategies.infer_portable_layout),
        ("platform_credit", strategies.infer_repeated_platform_credit),
        ("editorial_signature", _editorial_end_signature),
    ]
    strategy = strategies._STRATEGIES.get(context.profile.kind)
    if strategy:
        experts.append(("profile_strategy", strategy))
    for index, view in enumerate(views):
        found, labels = claims(
            view.samples, kind=view.profile.kind, work_scope=view.profile.work_scope
        )
        assertions.extend(found)
        regions.extend(labels)
        for name, expert in experts:
            report = expert(view)
            attempts.append(
                {
                    "expert": name,
                    "view": index,
                    "status": "assessed",
                    "author": report.get("author", ""),
                    "source": report.get("source", ""),
                }
            )
            if report.get("author"):
                proposals.append({"expert": name, "view": index, **report})
        # A different genre is a testable interpretation of acquired structure,
        # never inferred just because a parser happened to produce a name.
        profile = classify_document(view.samples, title_hint=view.title_hint)
        if profile.kind != view.profile.kind:
            alternative = strategies._STRATEGIES.get(profile.kind)
            if alternative:
                report = alternative(replace(view, profile=profile))
                attempts.append(
                    {
                        "expert": "genre_reconsideration",
                        "view": index,
                        "status": "assessed",
                        "before": view.profile.kind,
                        "after": profile.kind,
                        "reason": list(profile.cues),
                        "author": report.get("author", ""),
                    }
                )
                if report.get("author"):
                    proposals.append(
                        {"expert": "genre_reconsideration", "view": index, **report}
                    )
        first = next(
            (
                str(s.get("text") or "")
                for s in view.samples
                if int(s.get("page") or 0) == 1
            ),
            "",
        )
        title = opening_title(first.splitlines())
        if title and _words(title) != _words(view.title_hint):
            retry = strategies._select_author(replace(view, title_hint=title))
            attempts.append(
                {
                    "expert": "physical_title_alternative",
                    "view": index,
                    "status": "assessed",
                    "title": title,
                    "author": retry.get("author", ""),
                }
            )
            if retry.get("author"):
                proposals.append(
                    {"expert": "physical_title_alternative", "view": index, **retry}
                )
    # Read horizontal text separately from rotated export margins, and retain
    # complete page-level geometric proposals even after an earlier success.
    from .native_article import credit, alternating_credit, deck_credit

    for page in context.evidence_bundle.get("native", []):
        lines = page.get("horizontal_lines")
        if lines is None:
            continue
        for line in lines:
            top = line["box"][1] / max(1, page["height"])
            role = "header" if top < 0.08 else "footer" if top >= 0.90 else None
            if role:
                regions.append(
                    {
                        "role": role,
                        "page": page["page"],
                        "evidence": line["text"],
                        "basis": "relative page position; compatible with author credit",
                    }
                )
            if top >= 0.68 and re.match(r"^\s*(?:\d{1,3}|[*†‡])\s+", line["text"]):
                regions.append(
                    {
                        "role": "footnote",
                        "page": page["page"],
                        "evidence": line["text"],
                        "basis": "numbered lower-page line; does not veto authorship",
                    }
                )
        horizontal = {"page": page["page"], "text": "\n".join(v["text"] for v in lines)}
        found, labels = claims(
            [horizontal],
            kind=context.profile.kind,
            work_scope=context.profile.work_scope,
        )
        assertions.extend(found)
        regions.extend(labels)
        if (
            page["page"] <= 5
            and context.profile.kind in {"generic", "scholarly_article"}
            and context.profile.work_scope != "whole_issue"
        ):
            for name, expert in [
                ("physical_byline", credit),
                ("physical_affiliations", alternating_credit),
                ("physical_feature_deck", deck_credit),
            ]:
                report = expert(lines, page["width"], page["height"])
                attempts.append(
                    {
                        "expert": name,
                        "page": page["page"],
                        "status": "assessed",
                        "author": report.get("author", ""),
                    }
                )
                if report.get("author"):
                    proposals.append(
                        {"expert": "native_article", **report, "page": page["page"]}
                    )
    ocr = bundle_reports(context)
    attempts.append(
        {
            "expert": "all_acquired_ocr_regions",
            "status": "assessed",
            "regions": len((context.evidence_bundle or {}).get("ocr", [])),
            "proposals": len(ocr),
        }
    )
    proposals.extend({"expert": "ocr", **report} for report in ocr)
    # Explicit local chapter evidence may challenge a parent editorial credit.
    chapter = embedded_chapter(context, current)
    if chapter:
        proposals.append({"expert": "local_chapter_boundary", **chapter})
        current = chapter
    # Existing trust rules still apply. A new guess only fills unresolved or
    # filename-only evidence; stronger scoped responsibility below can revise
    # even an accepted native author. Agreement need not be independent.
    weak = not current.get("author") or str(current.get("source", "")).startswith(
        "filename_"
    )
    if weak and context.profile.work_scope != "whole_issue":
        order = {
            "document_record": 100,
            "editorial_signature": 99,
            "native_article": 97,
            "continued_byline": 96,
            "publisher_titlepage": 95,
            "work_rights": 94,
            "credential_roster": 93,
            "structured_credit": 91,
            "profile_strategy": 90,
            "portable_book": 88,
            "portable_layout": 87,
            "biography": 86,
            "ocr": 84,
            "physical_title_alternative": 83,
            "genre_reconsideration": 82,
            "positioned_opening": 81,
        }
        trusted = [
            p
            for p in proposals
            if p.get("source") in TRUSTED_AUTHOR_INFERENCE_SOURCES
            and not str(p.get("source", "")).startswith("filename_")
        ]
        if trusted:
            current = max(trusted, key=lambda p: order.get(p["expert"], 70))
    for proposal in proposals:
        if proposal.get("source") == "text_editorial_end_signature":
            assertions.append(
                {
                    "author": proposal["author"],
                    "role": "author",
                    "work": "selected work",
                    "page": proposal.get("page", 0),
                    "evidence": proposal.get("evidence", ""),
                    "family": "closing_affiliated_roster",
                    "priority": 108,
                    "source": proposal["source"],
                }
            )
    assertions.extend(
        running_chapter_claims(
            context.evidence_bundle,
            kind=context.profile.kind,
            work_scope=context.profile.work_scope,
        )
    )
    before = {"author": current.get("author", ""), "source": current.get("source", "")}
    current, decision = select(
        current,
        assertions,
        kind=context.profile.kind,
        work_scope=context.profile.work_scope,
    )
    keys = {_key(p) for p in proposals if p.get("author")}
    trace = {
        "version": 1,
        "score_kind": "contextual_priority_not_probability",
        "shared_inputs_allowed": True,
        "collection": context.evidence_bundle["trace"],
        "expert_attempts": attempts,
        "proposals": proposals,
        "claims": assertions,
        "region_hypotheses": regions,
        "competing_rosters": len(keys),
        "incumbent_before_scoped_claims": before,
        "decision": decision,
        "all_scheduled_experts_assessed": True,
    }
    if current.get("mechanism") in {
        "local_chapter_contributors",
        "local_chapter_with_two_later_author_heads",
    }:
        trace["scope_reconsideration"] = {
            "before": context.profile.work_scope,
            "after": "chapter",
            "reason": current["mechanism"],
        }
    return {**current, "evidence_review": trace}
