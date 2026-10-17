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
    r"\b(?:university|universit[eé]|department|institute|laboratory|laboratoire|school|college|centre|center|hospital|faculty|retired)\b",
    re.I,
)


def positioned_lines(page):
    """Keep name letters at their own baseline; discard smaller footnote markers."""
    import fitz

    from urllib.parse import urlsplit

    orcid_boxes = []
    try:
        for link in page.get_links():
            if urlsplit(link.get("uri", "")).hostname in {
                "orcid.org",
                "www.orcid.org",
            } and link.get("from"):
                orcid_boxes.append(fitz.Rect(link["from"]))
    except AttributeError, OSError, ValueError, RuntimeError:
        pass

    def identifier_icon(span, dominant_size):
        if span["text"].strip() not in {"ID", "iD", "ORCID"} or "bbox" not in span:
            return False
        box = fitz.Rect(span["bbox"])
        return any(
            (box & target).get_area() >= box.get_area() * 0.5 for target in orcid_boxes
        )

    fragments = []
    detached_identifiers = []
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
            dominant = max((s["size"] for s in letters), default=0)
            identifier_markers = [
                s for s in line["spans"] if identifier_icon(s, dominant)
            ]
            letters = [s for s in letters if s not in identifier_markers]
            if not letters and not separators:
                detached_identifiers.extend(identifier_markers)
                continue
            visible_letters = [s for s in letters if s.get("alpha", 255) > 0]
            core = visible_letters or letters or separators
            size = max(s["size"] for s in core)
            baseline = max(core, key=lambda s: len(s["text"]))["origin"][1]
            spans = [
                s
                for s in line["spans"]
                if s["size"] >= size * 0.75
                and s not in identifier_markers
                and (not (s["flags"] & 1) or not s["text"].strip())
                and (not visible_letters or s.get("alpha", 255) > 0)
            ]
            text = ""
            previous = None
            skipped_marker = False
            for span in line["spans"]:
                if span not in spans:
                    skipped_marker |= span in identifier_markers or bool(
                        re.search(r"[\d*†‡∗]", span["text"])
                    )
                    continue
                gap = (
                    span["bbox"][0] - previous["bbox"][2]
                    if previous and "bbox" in span and "bbox" in previous
                    else 0
                )
                boundary = skipped_marker and re.match(
                    r"^(?:and\b|&|[A-Z][a-z])", span["text"].lstrip()
                )
                separate_people = (
                    previous is not None
                    and gap > size * (0.8 if skipped_marker else 1.5)
                    and _names(text.rstrip(",; "))
                    and _names(span["text"].strip())
                    and not re.search(r"[,;&-]$", text.strip())
                    and not re.match(r"[,;&]|(?:and|&)\b", span["text"].strip(), re.I)
                )
                if separate_people:
                    text += ", "
                if (
                    text
                    and not text[-1].isspace()
                    and not span["text"].startswith(" ")
                    and (boundary or gap > size * 0.18)
                ):
                    text += " "
                text += span["text"]
                previous = span
                skipped_marker = False
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
                        "trailing_marker": skipped_marker,
                        "marker_exclusions": [
                            {
                                "text": s["text"],
                                "box": s.get("bbox"),
                                "role": "ORCID identifier icon",
                                "basis": "smaller identifier glyph inside its ORCID hyperlink rectangle",
                            }
                            for s in identifier_markers
                        ],
                        "visible": bool(
                            visible_letters
                            or (
                                not letters
                                and any(s.get("alpha", 255) > 0 for s in separators)
                            )
                        ),
                        "box": list(line["bbox"]),
                    }
                )
    for marker in detached_identifiers:
        if fragments:
            nearest = min(
                fragments, key=lambda v: abs(v["baseline"] - marker["origin"][1])
            )
            nearest.setdefault("marker_exclusions", []).append(
                {
                    "text": marker["text"],
                    "box": marker.get("bbox"),
                    "role": "ORCID identifier icon",
                    "basis": "linked identifier icon stored separately by the PDF, not a name suffix",
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
            # Distinct names in widely separated affiliation columns are
            # separate people. Adjacent pieces of one name still join with a
            # space, and explicit punctuation keeps its own delimiter.
            separate_people = (
                line["box"][0] - previous["box"][2]
                > line["size"] * (0.8 if previous.get("trailing_marker") else 1.5)
                and len(_names(previous["text"])) >= 1
                and len(_names(line["text"])) == 1
                and not re.search(r"[,;&]$", previous["text"].strip())
                and not re.match(r"[,;&]", line["text"].strip())
            )
            previous["text"] += (", " if separate_people else " ") + line["text"]
            previous["trailing_marker"] = line.get("trailing_marker", False)
            previous["marker_exclusions"] = previous.get(
                "marker_exclusions", []
            ) + line.get("marker_exclusions", [])
            previous["box"] = [
                min(previous["box"][0], line["box"][0]),
                min(previous["box"][1], line["box"][1]),
                max(previous["box"][2], line["box"][2]),
                max(previous["box"][3], line["box"][3]),
            ]
        else:
            groups.append({**line, "box": list(line["box"])})
    return groups


_DATE_WORD = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|Spring|Summer|Autumn|Fall|Winter)\b",
    re.I,
)


def _is_date_field(text):
    if len(text) > 120:
        return False
    named_date = _DATE_WORD.search(text) and re.search(r"\b\d{4}\b", text)
    numeric_date = re.fullmatch(
        r"\s*(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})\s*", text
    )
    return bool(named_date or numeric_date)


def _credit_roster(value):
    people = _names(value)
    if people:
        return people, []
    represented = re.fullmatch(
        r"(.+?)[,;]?\s+(on\s+behalf\s+of|representing|for)\s+(?:the\s+)?(.+)",
        value.strip(),
        re.I,
    )
    if not represented:
        return [], []
    people = _names(represented[1].rstrip(",; "))
    group = represented[3].strip(" ,;.")
    if (
        not people
        or not 2 <= len(group.split()) <= 14
        or not re.search(
            r"\b(?:consortium|collaboration|collaborative|network|team|investigators|study group|working group)\b",
            group,
            re.I,
        )
    ):
        return [], []
    return [*people, group], [
        {
            "name": group,
            "role": "represented author group",
            "relation": represented[2],
            "work": "selected article byline",
        }
    ]


def _roster_is_partial(names, complete):
    if not names or len(names) > len(complete):
        return False
    used = set()
    changed = len(names) < len(complete)
    for name in names:
        matching = next(
            (
                i
                for i, full in enumerate(complete)
                if i not in used
                and len(name) <= len(full)
                and full[-len(name) :] == name
            ),
            None,
        )
        if matching is None:
            return False
        used.add(matching)
        changed |= name != complete[matching]
    return changed


def _candidate_credit_reports(lines, width, height):
    eligible = [
        v
        for v in lines
        if 3 <= len(_words(v["text"])) <= 20
        and v["box"][1] < height * 0.45
        and (not _ROLE.search(v["text"]))
    ]
    largest = max((v["size"] for v in eligible), default=0)
    headings = [v for v in eligible if v["size"] >= largest * 0.9]
    if not headings:
        return
    start = headings[0]
    cluster = []
    for v in lines[lines.index(start) :]:
        if v["box"][2] < start["box"][0] or v["box"][0] > start["box"][2]:
            # Another column cannot terminate this title block.
            continue
        if (
            cluster
            and _names(v["text"])
            and (v["box"][1] - cluster[-1]["box"][3] > largest * 0.5)
        ):
            break
        if v["size"] < largest * 0.85 or (
            cluster and v["box"][1] - cluster[-1]["box"][3] > 40
        ):
            break
        cluster.append(v)
    title = " ".join((v["text"] for v in cluster))
    if not 3 <= len(_words(title)) <= 45 or re.search(
        "\\b(?:figure|table|plate|praise|reviewed|edited|translated)\\b", title, re.I
    ):
        return
    title_left = min(v["box"][0] for v in cluster)
    title_right = max(v["box"][2] for v in cluster)
    side_roles = [
        v
        for v in lines
        if _ROLE.search(v["text"])
        and v["size"] < largest * 0.85
        and (v["box"][2] < title_left or v["box"][0] > title_right)
    ]

    def in_responsibility_column(v):
        if v["box"][2] >= title_left and v["box"][0] <= title_right:
            return True
        return not any(
            v["box"][1] >= role["box"][1]
            and (
                (role["box"][2] < title_left and v["box"][2] < title_left)
                or (role["box"][0] > title_right and v["box"][0] > title_right)
            )
            for role in side_roles
        )

    following = _column_lines(
        [
            v
            for v in lines
            if v["box"][1] >= cluster[-1]["box"][3] - 2
            and in_responsibility_column(v)
            and not re.fullmatch(r"(?:[^\s<>]+@[^\s<>]+[\s,;]*)+", v["text"])
            and not (
                _ROLE.search(v["text"])
                and v["size"] < largest * 0.85
                and (
                    v["box"][2] < min(t["box"][0] for t in cluster)
                    or v["box"][0] > max(t["box"][2] for t in cluster)
                )
            )
        ],
        0,
        width * 0.98,
    )
    boundary = next(
        (
            i
            for i, v in enumerate(following)
            if re.fullmatch(
                r"abstract|summary|(?:1[. ]+)?introduction", v["text"].strip(), re.I
            )
            or re.match(r"^(?:abstract|summary)[:.]\s", v["text"], re.I)
        ),
        len(following),
    )
    # Prefix facts depend on the evidence, not the candidate or an accepted name.
    # Compute them once instead of re-parsing every previous body line for
    # every candidate. The previous author/affiliation-pair rule is unchanged.
    prior_pairs, prior_roles = [], []
    seen_pair = seen_role = False
    for index, line in enumerate(following):
        if index >= 2:
            seen_pair = seen_pair or bool(
                _INSTITUTION.search(following[index - 1]["text"])
                and _names(following[index - 2]["text"])
            )
        prior_pairs.append(seen_pair)
        prior_roles.append(seen_role)
        seen_role = seen_role or bool(_ROLE.search(line["text"]))
    for index, first in enumerate(following[:boundary]):
        if prior_pairs[index]:
            # A preceding author/affiliation pair belongs to the same title.
            # Its later pair is not an independent complete author roster.
            continue
        if index:
            previous = following[index - 1]
            if (
                abs(previous["size"] - first["size"]) <= first["size"] * 0.12
                and first["box"][1] - previous["box"][3] <= first["size"] * 1.5
                and _names(
                    re.sub(r"(?:\band|&)\s*$", "", previous["text"], flags=re.I).rstrip(
                        ",; "
                    )
                )
                and not _ROLE.search(previous["text"])
                and not _INSTITUTION.search(previous["text"])
            ):
                # This is a continuation inside one physical roster, not a
                # second independent proposal for its suffix alone.
                continue
        if prior_roles[index]:
            continue
        if first["box"][1] - cluster[-1]["box"][3] > 140 or _ROLE.search(first["text"]):
            continue
        if first["size"] >= largest or not re.match(
            "^(?:by\\s+)?[^\\W\\d_]", first["text"], re.I
        ):
            continue
        band = []
        for v in following[index:]:
            if (
                abs(v["size"] - first["size"]) > first["size"] * 0.12
                or _INSTITUTION.search(v["text"])
                or _ROLE.search(v["text"])
                or v["text"].casefold().startswith(("abstract", "summary"))
                or _is_date_field(v["text"].strip())
                or re.match(r"^\d{1,3}(?=\s|[^\W\d_])", v["text"])
            ):
                break
            if band and v["box"][1] - band[-1]["box"][3] > first["size"] * 1.5:
                break
            band.append(v)
        joined = ""
        for v in band:
            text = v["text"]
            # Separate stacked complete names, but retain ordinary wrapping of
            # comma-delimited rosters and fragments of an individual name.
            separator = (
                ", "
                if (
                    joined
                    and _names(text)
                    and _names(joined)
                    and not re.search(r"[,;&-]$", joined)
                    and not re.match(r"^(?:and|&)\b", text, re.I)
                )
                else " "
            )
            joined = (joined + separator + text).strip()
        joined = re.sub(",\\s*(?:and|&)\\s+", " and ", joined, flags=re.I)
        names, represented_groups = _credit_roster(joined)
        if not names:
            continue
        tail = following[index + len(band) :]
        nearby = " ".join(
            (
                v["text"]
                for v in tail
                if v["box"][1] - band[-1]["box"][3] < height * 0.65
            )
        )
        labelled_summary = bool(re.search("\\b(?:abstract|summary)\\b", nearby, re.I))
        body_start = next(
            (
                i
                for i, v in enumerate(tail)
                if len(v["text"].split()) >= 9
                and any((c.islower() for c in v["text"]))
                and (not _INSTITUTION.search(v["text"]))
                and (not _ROLE.search(v["text"]))
            ),
            len(tail),
        )
        further_credit_pair = any(
            (
                abs(v["size"] - first["size"]) <= first["size"] * 0.12
                and _INSTITUTION.search(tail[i + 1]["text"])
                and _names(v["text"])
                for i, v in enumerate(tail[: max(0, body_start - 1)])
            )
        )
        if further_credit_pair:
            continue
        affiliated_body = bool(
            tail
            and _INSTITUTION.search(tail[0]["text"])
            and (tail[0]["box"][1] - band[-1]["box"][3] < first["size"] * 4)
            and any(
                (
                    len(v["text"].split()) >= 9
                    and any((c.islower() for c in v["text"]))
                    and (not _INSTITUTION.search(v["text"]))
                    and (not _ROLE.search(v["text"]))
                    for v in tail[1:18]
                )
            )
        )
        if not (labelled_summary or affiliated_body):
            continue
        numbered_affiliation = bool(
            tail
            and re.match(r"^\d{1,3}\s*[^\W\d_]", tail[0]["text"])
            and labelled_summary
        )
        if not (
            _INSTITUTION.search(nearby)
            or re.match("^\\s*Abstract\\b", nearby, re.I)
            or numbered_affiliation
            or (
                tail
                and _is_date_field(tail[0]["text"])
                and len(tail) > 1
                and re.fullmatch(r"abstract|summary", tail[1]["text"].strip(), re.I)
            )
        ):
            continue
        yield {
            "author": ", ".join(names),
            "source": "text_structured_opening_credit",
            "page": 1,
            "evidence": title + " / " + joined,
            "represented_groups": represented_groups,
            "mechanism": "complete_native_author_band",
            "geometry": {
                "title": [v["box"] for v in cluster],
                "byline": [v["box"] for v in band],
                "marker_exclusions": [
                    m
                    for v in lines
                    if any(
                        v["box"] == b["box"]
                        or (
                            v["baseline"] == b["baseline"]
                            and v["box"][0] >= b["box"][0]
                            and v["box"][2] <= b["box"][2]
                        )
                        for b in band
                    )
                    for m in v.get("marker_exclusions", [])
                ],
            },
        }
    return


def credit_reports(lines, width, height):
    """Retain every interpretation, marking geometric roster suffixes partial."""
    reports = list(_candidate_credit_reports(lines, width, height))
    for report in reports:
        names = [tuple(_words(n)) for n in _credit_roster(report["author"])[0]]
        boxes = report.get("geometry", {}).get("byline", [])
        for other in reports:
            complete = [tuple(_words(n)) for n in _credit_roster(other["author"])[0]]
            other_boxes = other.get("geometry", {}).get("byline", [])
            if (
                _roster_is_partial(names, complete)
                and boxes
                and report.get("geometry", {}).get("title")
                == other.get("geometry", {}).get("title")
                and all(b in other_boxes for b in boxes)
            ):
                report = {
                    **report,
                    "mechanism": "partial_native_author_band",
                    "eligible_for_selection": False,
                    "eligibility_reason": "This is a geometric suffix/subset of the same title-bound complete roster.",
                    "complete_roster": other["author"],
                }
                break
        yield report


def credit(lines, width, height):
    reports = [
        r
        for r in credit_reports(lines, width, height)
        if r.get("eligible_for_selection", True)
    ]
    return reports[0] if reports else not_found()


def alternating_credit_reports(lines, width, height):
    """A title, complete author/affiliation pairs, then an abstract.

    Equal font sizes do not erase these textual role boundaries. A name-like
    subtitle is not a person credit unless it has its own affiliation.
    """
    lines = _column_lines(lines, 0, width)
    stop = next(
        (
            i
            for i, line in enumerate(lines)
            if re.fullmatch("(?i)abstract", line["text"].strip())
        ),
        None,
    )
    labelled_abstract = stop is not None
    if stop is None:
        first_pair = next(
            (
                i
                for i in range(len(lines) - 1)
                if _names(lines[i]["text"])
                and _INSTITUTION.search(lines[i + 1]["text"])
            ),
            None,
        )
        if first_pair is None:
            return
        stop = next(
            (
                i
                for i in range(first_pair + 2, len(lines))
                if len(_words(lines[i]["text"])) >= 9
                and any(c.islower() for c in lines[i]["text"])
                and not _INSTITUTION.search(lines[i]["text"])
                and not _ROLE.search(lines[i]["text"])
            ),
            None,
        )
    if stop is None:
        return
    pairs = []
    for i in range(stop - 1):
        raw = lines[i]["text"]
        names = _names(raw)
        if names and _INSTITUTION.search(lines[i + 1]["text"]):
            pairs.append((i, names))
    if not pairs:
        return
    first = pairs[0][0]
    heading = [
        line
        for line in lines[max(0, first - 4) : first]
        if len(_words(line["text"])) >= 3 and (not _ROLE.search(line["text"]))
    ]
    title = " ".join((line["text"] for line in heading))
    if not 4 <= len(_words(title)) <= 45 or _INSTITUTION.search(title):
        return
    covered = {i for index, _names_at_index in pairs for i in (index, index + 1)}
    if any((i not in covered for i in range(first, stop))):
        return
    names = [name for _, people in pairs for name in people]
    yield {
        "author": ", ".join(names),
        "source": "text_structured_opening_credit",
        "page": 1,
        "evidence": title + " / " + " / ".join((lines[i]["text"] for i, _ in pairs)),
        "mechanism": "title_author_affiliation_pairs_before_abstract"
        if labelled_abstract
        else "title_author_affiliation_pairs_before_body",
        "geometry": {"byline": [lines[i]["box"] for i, _ in pairs]},
    }


def alternating_credit(lines, width, height):
    reports = list(alternating_credit_reports(lines, width, height))
    return reports[0] if reports else not_found()


def _pair_email_columns(lines):
    """Join a name and its same-baseline contact despite a different font size."""
    email = re.compile(r"(?:[^\s<>]+@[^\s<>]+[\s,;]*)+")
    pairs = {}
    used = set()
    for index, line in enumerate(lines):
        if not _names(line["text"]) or _ROLE.search(line["text"]):
            continue
        contacts = [
            (j, v)
            for j, v in enumerate(lines)
            if j != index
            and j not in used
            and email.fullmatch(v["text"])
            and abs(v["baseline"] - line["baseline"]) < 2
            and v["box"][0] >= line["box"][2] - 2
        ]
        if contacts:
            used.update(j for j, _ in contacts)
            boxes = [line["box"], *(v["box"] for _, v in contacts)]
            pairs[index] = {
                **line,
                "text": line["text"] + " " + " ".join(v["text"] for _, v in contacts),
                "box": [
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                ],
                "contact_pair_basis": "same baseline, name to left of email; typography may differ",
            }
    return [pairs.get(i, line) for i, line in enumerate(lines) if i not in used]


def email_roster_reports(lines, width, height):
    """Read every name/email block before the article's body boundary.

    Affiliations may span several lines, and several authors may share one.
    Email is a role cue only in a title-bound opening roster, never a general
    licence to treat contacts or names in prose as authors.
    """
    lines = _column_lines(_pair_email_columns(lines), 0, width)
    stop = next(
        (
            i
            for i, line in enumerate(lines)
            if re.fullmatch(
                r"abstract|summary|(?:1[. ]+)?introduction", line["text"].strip(), re.I
            )
        ),
        None,
    )
    if stop is None:
        return
    opening = lines[:stop]
    email = re.compile(r"[^\s<>]+@[^\s<>]+")
    credits = []
    for index, line in enumerate(opening):
        if not email.search(line["text"]) or _ROLE.search(line["text"]):
            continue
        raw = email.sub("", line["text"]).strip(" ,;:()")
        names = _names(raw)
        if names:
            credits.append((index, line, names))
    if not credits:
        return
    first = credits[0][0]
    before = opening[:first]
    headings = [
        line
        for line in before
        if line["box"][1] < height * 0.55
        and not _ROLE.search(line["text"])
        and not _INSTITUTION.search(line["text"])
        and len(_words(line["text"])) >= 2
    ]
    if not headings:
        return
    size = max(line["size"] for line in headings)
    heading = [line for line in headings if line["size"] >= size * 0.9]
    title = " ".join(line["text"] for line in heading)
    if (
        len(_words(title)) < 3
        or re.search(
            r"\b(?:praise|contents|acknowledg\w*|reviewers?|contact|editorial board)\b",
            title,
            re.I,
        )
        or any(line["size"] >= size for _, line, _ in credits)
    ):
        return
    tail = opening[first:]
    if not any(_INSTITUTION.search(line["text"]) for line in tail):
        return
    # An unpaired byline-size person may be an additional author. Do not
    # declare the email subset complete when such a person is unexplained.
    credit_indices = {index for index, _, _ in credits}
    for index, line in enumerate(opening[first:], first):
        if index in credit_indices or _ROLE.search(line["text"]):
            continue
        if (
            _names(line["text"])
            and any(
                abs(line["size"] - credit["size"]) < 0.5 for _, credit, _ in credits
            )
            and not _INSTITUTION.search(line["text"])
        ):
            return
    names = [name for _, _, people in credits for name in people]
    yield {
        "author": ", ".join(names),
        "source": "text_structured_opening_credit",
        "page": 1,
        "evidence": title + " / " + " / ".join(line["text"] for _, line, _ in credits),
        "mechanism": "complete_title_bound_email_roster",
        "geometry": {"byline": [line["box"] for _, line, _ in credits]},
    }


def deck_credit_reports(lines, width, height):
    """A feature/commentary byline may follow a summary deck, above body prose."""
    if not any(
        (
            re.fullmatch("FEATURE ARTICLE|INSIGHT", line["text"], re.I)
            for line in lines[:8]
        )
    ):
        return
    eligible = [
        line
        for line in lines
        if line["box"][1] < height * 0.45
        and 2 <= len(_words(line["text"])) <= 25
        and (not line["text"].isupper())
        and (not _ROLE.search(line["text"]))
    ]
    if not eligible:
        return
    largest = max((line["size"] for line in eligible))
    heading = [line for line in eligible if line["size"] >= largest * 0.9]
    bottom = max((line["box"][3] for line in heading))
    for index, line in enumerate(lines):
        if line["box"][1] <= bottom or line["box"][1] > height * 0.65:
            continue
        if not line["text"].isupper() or _ROLE.search(line["text"]):
            continue
        deck = " ".join((v["text"] for v in lines[:index] if v["box"][1] >= bottom))
        if len(deck.split()) < 12:
            continue
        band = [line]
        for following in lines[index + 1 :]:
            if (
                not following["text"].isupper()
                or _ROLE.search(following["text"])
                or _INSTITUTION.search(following["text"])
                or abs(following["size"] - line["size"]) > line["size"] * 0.15
                or (following["box"][1] - band[-1]["box"][3] > line["size"] * 1.5)
            ):
                break
            band.append(following)
        joined = " ".join((v["text"] for v in band))
        names = _names(joined)
        tail = " ".join(
            (v["text"] for v in lines[index + len(band) : index + len(band) + 5])
        )
        if (
            not names
            or len(tail.split()) < 12
            or (not any((c.islower() for c in tail)))
        ):
            continue
        yield {
            "author": ", ".join(names),
            "source": "text_structured_opening_credit",
            "page": 1,
            "evidence": " / ".join((v["text"] for v in heading)) + " / " + joined,
            "mechanism": "feature_deck_then_author_byline",
            "geometry": {"byline": [v["box"] for v in band]},
        }
    return


def deck_credit(lines, width, height):
    reports = list(deck_credit_reports(lines, width, height))
    return reports[0] if reports else not_found()


def infer(context):
    if (
        context.path is None
        or context.profile.kind
        not in {"generic", "scholarly_article", "book_chapter", "web_article"}
        or context.profile.work_scope == "whole_issue"
    ):
        return not_found()
    if context.evidence_bundle is not None:
        # Acquisition has already read these pages. Assess each available
        # opening page and specialist even after an earlier proposal succeeds.
        reports = []
        planned = {int(s["page"]) for s in context.planned_opening_pages}
        for page in context.evidence_bundle.get("native", []):
            if (
                (
                    page.get("page", 0) not in planned
                    if planned
                    else page.get("page", 0) > 5
                )
                or "horizontal_lines" not in page
                or "width" not in page
                or "height" not in page
            ):
                continue
            for expert in (credit, alternating_credit, deck_credit):
                report = expert(page["horizontal_lines"], page["width"], page["height"])
                if report.get("author"):
                    reports.append({**report, "page": page["page"]})
        return next(iter(reports), not_found())
    import fitz

    try:
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
        return result
    except OSError, ValueError, RuntimeError:
        return not_found()


def mixed_image_line_views(page, regions):
    """Place small OCR image text beside native text in the same PDF space.

    The native-only and crop-only interpretations remain available. OCR is
    added here only where a small image has no overlapping readable native
    line; it cannot overwrite an already readable native byline.
    """
    native = page.get("horizontal_lines", [])
    relevant = [
        r
        for r in regions
        if r.get("page") == page["page"]
        and r.get("clip")
        and (r["clip"][2] - r["clip"][0]) * (r["clip"][3] - r["clip"][1])
        < page["width"] * page["height"] * 0.5
    ]
    result = []
    for alternate in (False, True):
        if alternate and not any(r.get("alternate") for r in relevant):
            continue
        added = []
        for original in relevant:
            region = original.get("alternate", original) if alternate else original
            factor = region.get("scale", 1)
            x, y = original["clip"][:2]
            for line in region.get("lines", []):
                box = [
                    line["box"][0] / factor + x,
                    line["box"][1] / factor + y,
                    line["box"][2] / factor + x,
                    line["box"][3] / factor + y,
                ]
                area = max(1, (box[2] - box[0]) * (box[3] - box[1]))

                def overlap(b):
                    return max(0, min(box[2], b[2]) - max(box[0], b[0])) * max(
                        0, min(box[3], b[3]) - max(box[1], b[1])
                    )

                if any(overlap(n["box"]) / area > 0.4 for n in native):
                    continue
                text = re.sub(r"(?<=\w)[\d*†‡]+(?=\s|[,;&]|$)", "", line["text"])
                if not text.strip():
                    continue
                added.append(
                    {
                        "text": text,
                        "box": box,
                        "size": max(1, (box[3] - box[1]) * 1.35),
                        "baseline": box[3],
                        "extraction": "ocr_image_in_native_page",
                        "ocr_confidence": line.get("confidence"),
                    }
                )
        if added:
            result.append(
                {
                    "lines": sorted(
                        [*native, *added], key=lambda r: (r["box"][1], r["box"][0])
                    ),
                    "added_boxes": [r["box"] for r in added],
                    "alternate": alternate,
                }
            )
    return result
