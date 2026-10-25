"""Collect all scheduled evidence, consult specialists, then adjudicate roles."""

from dataclasses import replace
import re
from .constants import TRUSTED_AUTHOR_INFERENCE_SOURCES
from .evidence_collection import collect, evidence_views
from .publishing_relations import claims, select, running_chapter_claims
from .structured_credit import _words
from .text_responsibility import claims as text_span_claims
from .contextual_roles import claims as role_claims


def _key(report):
    return _words(report.get("author", ""))


def infer(context):
    from . import dispatcher as strategies
    from .bounded_ocr import bundle_reports
    from .scholarly_article import _editorial_end_signature
    from .physical_work import opening_title, opening_title_hypotheses
    from .profile import classify_document
    from .publishing_acts import embedded_chapter, parent_editor_credit
    from . import structured_credit
    from .portable_book import credit_reports as book_credit_reports
    from .book_chapter import infer_reports as chapter_credit_reports

    context = collect(context)
    opening_plan = (context.evidence_bundle or {}).get("trace", {}).get("opening_plan")
    if (context.evidence_bundle or {}).get("trace", {}).get(
        "mode"
    ) == "supplied_samples_only":
        opening_plan = None
    views = evidence_views(context)
    proposals, attempts, assertions, regions = [], [], [], []
    title_hypotheses = opening_title_hypotheses(
        context.evidence_bundle,
        context.title_hint,
        work_scope=context.profile.work_scope,
    )
    original_views = list(views)
    for hypothesis in title_hypotheses:
        for view in original_views:
            if _words(hypothesis["title"]) == _words(view.title_hint):
                continue
            profile = classify_document(view.samples, title_hint=hypothesis["title"])
            if context.profile.work_scope == "chapter":
                profile = replace(profile, kind="book_chapter", work_scope="chapter")
            views.append(replace(view, title_hint=hypothesis["title"], profile=profile))
        attempts.append(
            {
                "expert": "opening_work_title_hypothesis",
                "status": "assessed",
                **hypothesis,
            }
        )
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
            view.samples,
            kind=view.profile.kind,
            work_scope=view.profile.work_scope,
            opening_pages=opening_plan,
        )
        assertions.extend(found)
        assertions.extend(
            text_span_claims(
                view.samples,
                kind=view.profile.kind,
                work_scope=view.profile.work_scope,
                title_hint=view.title_hint,
                opening_pages=opening_plan,
            )
        )
        assertions.extend(
            role_claims(
                view.samples,
                kind=view.profile.kind,
                work_scope=view.profile.work_scope,
                opening_pages=opening_plan,
            )
        )
        regions.extend(labels)
        for name, expert in experts:
            structured_names = {
                "structured_credit": "infer",
                "biography": "biographic_titlepage",
                "work_rights": "work_rights_credit",
            }
            enumerator = getattr(
                structured_credit, structured_names.get(name, name) + "_reports", None
            )
            if name == "portable_book":
                enumerator = book_credit_reports
            if name == "profile_strategy" and view.profile.kind == "book_chapter":
                enumerator = chapter_credit_reports
            if enumerator is not None:
                reports = list(enumerator(view))
                attempts.append(
                    {
                        "expert": name,
                        "view": index,
                        "status": "assessed",
                        "enumeration": "all_occurrences",
                        "proposals": len(reports),
                    }
                )
                proposals.extend(
                    {"expert": name, "view": index, **report}
                    for report in reports
                    if report.get("author")
                )
                continue
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
            # Compatibility experts return one result. Enumerate their local
            # page interpretations too, so a result on an earlier page cannot
            # suppress a later rights/title/signature proposal. Actual page
            # numbers and work scope are preserved, with no fake first page.
            if name in {
                "document_record",
                "credential_roster",
                "structured_credit",
                "publisher_titlepage",
                "work_rights",
                "whole_book_editors",
                "continued_byline",
                "biography",
                "profile_strategy",
            }:
                for sample in view.samples:
                    local = expert(replace(view, samples=(sample,)))
                    attempts.append(
                        {
                            "expert": name,
                            "view": index,
                            "projection": "page_local",
                            "page": sample.get("page", 0),
                            "status": "assessed",
                            "author": local.get("author", ""),
                            "source": local.get("source", ""),
                        }
                    )
                    if local.get("author"):
                        proposals.append(
                            {
                                "expert": name,
                                "view": index,
                                "projection": "page_local",
                                **local,
                            }
                        )
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
    from .native_article import (
        credit_reports,
        alternating_credit_reports,
        deck_credit_reports,
        email_roster_reports,
        mixed_image_line_views,
    )
    from .portable_layout import layout_credit_reports

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
            opening_pages=opening_plan,
        )
        assertions.extend(found)
        regions.extend(labels)
        if (
            (
                page["page"] in opening_plan
                if opening_plan is not None
                else page["page"] <= 5
            )
            and context.profile.kind
            in {"generic", "scholarly_article", "book_chapter", "web_article"}
            and context.profile.work_scope != "whole_issue"
        ):
            # Marker-cleaned baselines serve journal rosters; raw PDF spans
            # preserve display lettering and its original font hierarchy.
            # Both readings remain available to the layout specialist.
            for representation, geometry in (
                ("marker_cleaned", lines),
                ("native_spans", page.get("layout_lines", [])),
            ):
                reports = list(
                    layout_credit_reports(
                        geometry, page["width"], page["height"], page=page["page"]
                    )
                )
                attempts.append(
                    {
                        "expert": "portable_layout_all_occurrences",
                        "page": page["page"],
                        "representation": representation,
                        "status": "assessed",
                        "proposals": len(reports),
                    }
                )
                proposals.extend(
                    {
                        "expert": "portable_layout",
                        "representation": representation,
                        **report,
                    }
                    for report in reports
                )
        if (
            (
                page["page"] in opening_plan
                if opening_plan is not None
                else page["page"] <= 5
            )
            and context.profile.kind
            in {"generic", "scholarly_article", "book_chapter", "web_article"}
            and context.profile.work_scope != "whole_issue"
        ):
            for name, expert in [
                ("physical_byline", credit_reports),
                ("physical_affiliations", alternating_credit_reports),
                ("physical_feature_deck", deck_credit_reports),
                ("physical_email_roster", email_roster_reports),
            ]:
                reports = list(expert(lines, page["width"], page["height"]))
                attempts.append(
                    {
                        "expert": name,
                        "page": page["page"],
                        "status": "assessed",
                        "proposals": len(reports),
                    }
                )
                for report in reports:
                    proposals.append(
                        {"expert": "native_article", **report, "page": page["page"]}
                    )
                    if report.get("mechanism") in {
                        "complete_native_author_band",
                        "title_author_affiliation_pairs_before_abstract",
                        "title_author_affiliation_pairs_before_body",
                    }:
                        boxes = report.get("geometry", {}).get("byline", [])
                        band_lines = [
                            line
                            for line in lines
                            if any(
                                min(line["box"][2], b[2]) > max(line["box"][0], b[0])
                                and min(line["box"][3], b[3])
                                > max(line["box"][1], b[1])
                                for b in boxes
                            )
                        ]
                        if band_lines and all(
                            line.get("visible", True) for line in band_lines
                        ):
                            assertions.append(
                                {
                                    **report,
                                    "page": page["page"],
                                    "role": "author",
                                    "work": "selected work",
                                    "family": "physical_complete_native_roster",
                                    "priority": 109,
                                }
                            )
                    if report.get("mechanism") == "complete_title_bound_email_roster":
                        assertions.append(
                            {
                                **report,
                                "page": page["page"],
                                "role": "author",
                                "work": "selected work",
                                "family": "physical_complete_email_roster",
                                "priority": 109,
                            }
                        )
        if (
            (
                page["page"] in opening_plan
                if opening_plan is not None
                else page["page"] <= 5
            )
            and context.profile.kind
            in {"generic", "scholarly_article", "book_chapter", "web_article"}
            and context.profile.work_scope != "whole_issue"
        ):
            for mixed in mixed_image_line_views(
                page, context.evidence_bundle.get("ocr", [])
            ):
                reports = list(
                    credit_reports(mixed["lines"], page["width"], page["height"])
                )
                attempts.append(
                    {
                        "expert": "mixed_native_ocr_physical_byline",
                        "page": page["page"],
                        "alternate": mixed["alternate"],
                        "status": "assessed",
                        "proposals": len(reports),
                    }
                )
                for report in reports:
                    byline = report.get("geometry", {}).get("byline", [])
                    uses_image = any(
                        min(a[2], b[2]) > max(a[0], b[0])
                        and min(a[3], b[3]) > max(a[1], b[1])
                        for a in byline
                        for b in mixed["added_boxes"]
                    )
                    if not uses_image:
                        continue
                    report = {
                        **report,
                        "page": page["page"],
                        "source": "text_ocr_author_credit",
                        "mechanism": "mixed_native_ocr_complete_author_band",
                        "coordinate_space": "pdf_points",
                        "extraction_views": ["native baselines", "OCR image text"],
                    }
                    proposals.append({"expert": "mixed_native_ocr", **report})
                    assertions.append(
                        {
                            **report,
                            "role": "author",
                            "work": "selected work",
                            "family": "physical_complete_mixed_roster",
                            "priority": 107,
                        }
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
    parent_editor_established = any(
        parent_editor_credit(report) for report in [current, *proposals]
    )
    chapter = (
        current
        if current.get("mechanism")
        == "local_chapter_credit_with_repeated_running_author"
        and len(set(current.get("corroboration_pages", []))) >= 2
        else embedded_chapter(
            context, current, parent_editor_established=parent_editor_established
        )
    )
    attempts.append(
        {
            "expert": "local_chapter_boundary",
            "status": "assessed",
            "parent_editor_evidence_seen": parent_editor_established,
            "author": chapter.get("author", "") if chapter else "",
            "basis": "independent parent-role evidence enables physical chapter and running-head verification; provisional author does not gate it",
        }
    )
    if chapter:
        proposals.append({"expert": "local_chapter_boundary", **chapter})
        current = chapter
    # A complete catalog credit remains evidence for a chapter even when the
    # genre expert has no local byline. A bare surname is never expanded here.
    if context.path is not None and context.profile.work_scope != "whole_issue":
        from .filename import infer_author_from_filename
        from .names import split_author_line_candidates, looks_like_person_name

        filename = infer_author_from_filename(context.path, context.title_hint)
        complete_sources = {
            "filename_leading_name",
            "filename_leading_names",
            "filename_compact_name_before_year",
            "filename_name_before_section_label",
        }
        if filename.get("source") in complete_sources and (
            looks_like_person_name(filename.get("author", ""))
            or split_author_line_candidates(
                filename.get("author", ""), require_complete=True
            )
        ):
            proposals.append({"expert": "filename_catalog", **filename})
            # A complete credit in an explicit Name - Title filename can
            # identify an excerpt whose local byline is absent. For any other
            # genre, require its actual title to occur on opening pages.
            # This does not expand a bare surname or affect neutral names.
            catalog_title = re.split(r"\s+[-–—]{1,2}\s+", context.path.stem, maxsplit=1)
            title_words = _words(catalog_title[1]) if len(catalog_title) == 2 else ()
            visible_words = _words(
                " ".join(
                    str(s.get("text") or "")
                    for view in views
                    for s in view.samples
                    if int(s.get("page") or 0) <= 5
                )
            )
            length = min(4, len(title_words))
            title_visible = length >= 3 and any(
                visible_words[n : n + length] == title_words[:length]
                for n in range(max(0, len(visible_words) - length + 1))
            )
            if len(title_words) == 2:
                title_visible |= any(
                    _words(line) == title_words
                    for view in views
                    for sample in view.samples
                    if int(sample.get("page") or 0) <= 5
                    for line in str(sample.get("text") or "").splitlines()
                )
            if (
                not current.get("author")
                and current.get("source") != "unresolved_issue_editor_role"
                and (context.profile.kind in {"book", "book_chapter"} or title_visible)
            ):
                current = filename

    # Existing trust rules still apply. A new guess only fills unresolved or
    # filename-only evidence; stronger scoped responsibility below can revise
    # even an accepted native author. Agreement need not be independent.
    weak = not current.get("author") or str(current.get("source", "")).startswith(
        "filename_"
    )
    for proposal in proposals:
        if re.match(
            r"^\s*(?:coordinated\s+by|contributing\s+authors?\s*:)",
            str(proposal.get("author") or ""),
            re.I,
        ):
            proposal["eligible_for_selection"] = False
            proposal["eligibility_reason"] = (
                "A role clause is not itself a personal name"
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
            and p.get("eligible_for_selection", True)
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
    # A repository's broad Author(s) field may include a translator. Only
    # revise that roster when an explicit local byline supplies the remaining
    # people and a separately read translation credit explains the difference.
    # A translator can also be an author; the role label alone is no veto.
    from .publishing_relations import local_credit_role_claims, title_copyright_claims

    assertions.extend(local_credit_role_claims(proposals, assertions))
    assertions.extend(
        title_copyright_claims(proposals, context.samples, opening_pages=opening_plan)
    )
    from .publishing_relations import (
        opening_byline_claims,
        primary_creator_role_claims,
        visible_credit_truncation_claims,
    )

    assertions.extend(
        opening_byline_claims(
            proposals,
            context.evidence_bundle,
            kind=context.profile.kind,
            work_scope=context.profile.work_scope,
        )
    )
    kind, work_scope = context.profile.kind, context.profile.work_scope
    if chapter:
        kind, work_scope = "book_chapter", "chapter"
        assertions = [
            {
                **claim,
                "work": "parent book",
                "priority": 0,
                "scope_basis": "A local chapter credit and two subsequent author heads establish the selected contribution",
            }
            if claim.get("role") == "editor" and claim.get("work") == "selected work"
            else claim
            for claim in assertions
        ]
        assertions.append(
            {
                **chapter,
                "role": "author",
                "work": "selected chapter",
                "family": chapter["mechanism"],
                "priority": 116,
            }
        )
    assertions.extend(
        primary_creator_role_claims(proposals, kind=kind, work_scope=work_scope)
    )
    assertions.extend(
        visible_credit_truncation_claims(assertions, context.evidence_bundle)
    )
    from .embedded_credit_experts import assess as assess_embedded_credits

    embedded_proposals, embedded_attempts, embedded_assertions = assess_embedded_credits(
        context, views, responsibility_claims=assertions
    )
    proposals.extend(embedded_proposals)
    attempts.extend(embedded_attempts)
    assertions.extend(embedded_assertions)
    before = {"author": current.get("author", ""), "source": current.get("source", "")}
    from .publishing_relations import reconcile_report_foreword

    current, nested_scope = reconcile_report_foreword(
        current, context.evidence_bundle, proposals, assertions
    )
    current, decision = select(
        current,
        assertions,
        kind=kind,
        work_scope=work_scope,
    )
    keys = {_key(p) for p in proposals if p.get("author")}
    trace = {
        "version": 1,
        "work_title_hypotheses": title_hypotheses,
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
    if nested_scope is not None:
        trace["nested_contribution_scope"] = nested_scope
    if chapter or current.get("mechanism") in {
        "local_chapter_contributors",
        "local_chapter_with_two_later_author_heads",
    }:
        trace["scope_reconsideration"] = {
            "before": context.profile.work_scope,
            "after": "chapter",
            "reason": chapter["mechanism"] if chapter else current["mechanism"],
        }
    return {**current, "evidence_review": trace}
