"""Optional local GROBID evidence for single-work scholarly articles.

GROBID's TEI is kept as independent evidence. It never changes the selected
PDF extraction or assigns a creator to another document genre.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
import xml.etree.ElementTree as ET
import os
import time

from rag_pdf_tools import normalize_text

from .names import author_candidate_is_document_role, looks_like_person_name
from .title_evidence import words


_TEI = {"t": "http://www.tei-c.org/ns/1.0"}
_XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
_CACHE: dict[tuple, dict] = {}


class GrobidNoContentError(ValueError):
    """The service completed the request but found no structured content."""


def _consolidation_setting(name, allowed):
    value = os.environ.get(name, "0")
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(sorted(allowed))}")
    return value


def _text(element):
    return normalize_text(" ".join(element.itertext())) if element is not None else ""


def _name(author):
    person = author.find("t:persName", _TEI)
    if person is None:
        return ""
    first = [_text(value) for value in person.findall("t:forename", _TEI)]
    surname = _text(person.find("t:surname", _TEI))
    return normalize_text(" ".join([*first, surname]))


def parse_tei(payload: bytes, *, fulltext=False):
    """Read work-level TEI fields without confusing cited authors with creators."""
    root = ET.fromstring(payload)
    header = root.find("t:teiHeader", _TEI)
    if header is None:
        raise ValueError("GROBID response has no TEI header")
    work = header.find("t:fileDesc/t:sourceDesc/t:biblStruct/t:analytic", _TEI)
    if work is None:
        work = header.find("t:fileDesc/t:titleStmt", _TEI)
    if work is None:
        raise ValueError("GROBID response has no work-level header")
    title = _text(work.find("t:title", _TEI))
    authors = []
    for author in work.findall("t:author", _TEI):
        name = _name(author)
        if name:
            person = author.find("t:persName", _TEI)
            authors.append({
                "name": name,
                "coords": person.get("coords", "") if person is not None else "",
                "affiliations": [_text(value) for value in author.findall("t:affiliation", _TEI)
                                 if _text(value)],
            })
    bibliographic = header.find("t:fileDesc/t:sourceDesc/t:biblStruct", _TEI)
    doi = ""
    publication = ""
    published = ""
    if bibliographic is not None:
        for identifier in bibliographic.findall(".//t:idno", _TEI):
            if identifier.get("type", "").casefold() == "doi":
                doi = _text(identifier)
                break
        publication = _text(bibliographic.find("t:monogr/t:title", _TEI))
        date = bibliographic.find("t:monogr/t:imprint/t:date", _TEI)
        published = date.get("when", "") if date is not None else ""
    result = {
        "title": title,
        "title_coords": work.find("t:title", _TEI).get("coords", "")
        if work.find("t:title", _TEI) is not None else "",
        "authors": authors,
        "doi": doi,
        "publication": publication,
        "published": published,
        "abstract": _text(header.find("t:profileDesc/t:abstract", _TEI)),
        "keywords": [_text(term) for term in header.findall(".//t:keywords/t:term", _TEI)
                     if _text(term)],
        "funders": [_text(item) for item in header.findall(".//t:funder", _TEI)
                    if _text(item)],
        "licenses": [_text(item) for item in header.findall(".//t:licence", _TEI)
                     if _text(item)],
        "copyrights": [_text(item) for item in header.findall(".//t:availability/t:p", _TEI)
                       if _text(item)],
        "references": [],
        "citation_contexts": [],
        "figures": [],
        "tables": [],
        "sections": [],
        "paragraphs": [],
        "sentences": [],
        "footnotes": [],
        "formulas": [],
        "body_word_count": 0,
    }
    if fulltext:
        for item in root.findall(".//t:back//t:listBibl/t:biblStruct", _TEI):
            cited_work = item.find("t:analytic", _TEI)
            if cited_work is None:
                cited_work = item.find("t:monogr", _TEI)
            refs = {
                "id": item.get("{http://www.w3.org/XML/1998/namespace}id", ""),
                "coords": item.get("coords", ""),
                "title": _text(cited_work.find("t:title", _TEI)) if cited_work is not None else "",
                "authors": [_name(a) for a in cited_work.findall("t:author", _TEI) if _name(a)]
                if cited_work is not None else [],
                "publication": _text(item.find("t:monogr/t:title", _TEI)),
                "published": (item.find("t:monogr/t:imprint/t:date", _TEI).get("when", "")
                              if item.find("t:monogr/t:imprint/t:date", _TEI) is not None else ""),
                "doi": next((_text(identifier) for identifier in item.findall(".//t:idno", _TEI)
                             if identifier.get("type", "").casefold() == "doi"), ""),
                "raw": _text(item.find("t:note[@type='raw_reference']", _TEI)),
            }
            result["references"].append(refs)
        parents = {child: parent for parent in root.iter() for child in parent}
        for ref in root.findall(".//t:text/t:body//t:ref[@type='bibr']", _TEI):
            paragraph = parents.get(ref)
            while paragraph is not None and paragraph.tag not in {
                f"{{{_TEI['t']}}}p", f"{{{_TEI['t']}}}head",
            }:
                paragraph = parents.get(paragraph)
            result["citation_contexts"].append({
                "target": ref.get("target", ""), "callout": _text(ref),
                "coords": ref.get("coords", ""),
                "context": _text(paragraph)[:700],
                "context_id": paragraph.get(_XML_ID, "") if paragraph is not None else "",
            })
        reference_by_id = {item["id"]: item for item in result["references"] if item["id"]}
        for mention in result["citation_contexts"]:
            referenced = reference_by_id.get(mention["target"].lstrip("#"))
            if referenced:
                mention["reference_title"] = referenced["title"]
                mention["reference_raw"] = referenced["raw"]
                mention["reference_id"] = referenced["id"]
        for figure in root.findall(".//t:text/t:body//t:figure", _TEI):
            entry = {"coords": figure.get("coords", ""),
                     "caption": _text(figure.find("t:figDesc", _TEI)) or
                     _text(figure.find("t:head", _TEI))}
            result["tables" if figure.get("type") == "table" else "figures"].append(entry)
        for section in root.findall(".//t:text/t:body//t:div", _TEI):
            head = section.find("t:head", _TEI)
            if head is not None and _text(head):
                result["sections"].append({"heading": _text(head),
                                           "coords": head.get("coords", ""),
                                           "id": section.get(_XML_ID, "")})
        for paragraph in root.findall(".//t:text/t:body//t:p", _TEI):
            content = _text(paragraph)
            if content:
                paragraph_id = paragraph.get(_XML_ID, "")
                result["paragraphs"].append({"id": paragraph_id, "text": content,
                                             "coords": paragraph.get("coords", "")})
                result["body_word_count"] += len(content.split())
                for sentence in paragraph.findall("t:s", _TEI):
                    result["sentences"].append({
                        "id": sentence.get(_XML_ID, ""),
                        "paragraph_id": paragraph_id,
                        "text": _text(sentence),
                        "coords": sentence.get("coords", ""),
                    })
        for note in root.findall(".//t:text/t:body//t:note", _TEI):
            if note.get("place") in {"foot", "footnote"}:
                result["footnotes"].append({"text": _text(note),
                                            "coords": note.get("coords", "")})
        for formula in root.findall(".//t:text/t:body//t:formula", _TEI):
            result["formulas"].append({"text": _text(formula),
                                       "coords": formula.get("coords", "")})
    return result


def _service_url():
    configured = os.environ.get("PDF_GROBID_URL", "http://127.0.0.1:8070").rstrip("/")
    parsed = urlparse(configured)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("PDF_GROBID_URL must be an HTTP service URL")
    # The default remains local. A remote endpoint requires an explicit URL.
    if "PDF_GROBID_URL" not in os.environ and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Default GROBID service must be local")
    return configured


def _post_pdf(path: Path, *, fulltext: bool, timeout: float):
    boundary = f"grobid-{uuid4().hex}"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"input\"; filename=\"document.pdf\"\r\n"
        "Content-Type: application/pdf\r\n\r\n".encode("ascii") + path.read_bytes(),
    ]
    fields = [("consolidateHeader", _consolidation_setting(
        "PDF_GROBID_HEADER_CONSOLIDATION", {"0", "1", "2", "3"})),
              ("includeRawAffiliations", "1"),
              ("includeRawCopyrights", "1")]
    if fulltext:
        fields.extend([("consolidateCitations", _consolidation_setting(
            "PDF_GROBID_CITATION_CONSOLIDATION", {"0", "1", "2"})),
                       ("includeRawCitations", "1"),
                       ("segmentSentences", "1"),
                       ("generateIDs", "1")])
        fields.extend(("teiCoordinates", kind) for kind in
                      ("persName", "title", "affiliation", "ref", "biblStruct",
                       "figure", "head", "p", "s", "note", "formula"))
    for name, value in fields:
        parts.append(f"\r\n--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"{name}\"\r\n\r\n{value}".encode("ascii"))
    body = b"".join(parts) + f"\r\n--{boundary}--\r\n".encode()
    endpoint = "processFulltextDocument" if fulltext else "processHeaderDocument"
    request = Request(
        f"{_service_url()}/api/{endpoint}", body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}",
         "Accept": "application/xml"}, method="POST",
    )
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:
                if getattr(response, "status", 200) == 204:
                    raise GrobidNoContentError("GROBID returned HTTP 204")
                return response.read()
        except HTTPError as exc:
            if exc.code != 503 or attempt:
                raise
            # GROBID documents 503 as temporary thread exhaustion. Retry one
            # request after its recommended header/fulltext recovery interval.
            time.sleep(5 if fulltext else 2)


def analyze(path, context, *, fulltext=False):
    """Return diagnostics on failure; never block another genre on GROBID."""
    if context.profile.kind != "scholarly_article" or context.profile.work_scope != "single_work":
        return {"status": "skipped_genre"}
    pdf = Path(path)
    if not pdf.is_file():
        return {"status": "missing_pdf"}
    try:
        stat = pdf.stat()
        key = (str(pdf.resolve()), stat.st_size, stat.st_mtime_ns, fulltext,
               os.environ.get("PDF_GROBID_URL", ""),
               os.environ.get("PDF_GROBID_HEADER_CONSOLIDATION", "0"),
               os.environ.get("PDF_GROBID_CITATION_CONSOLIDATION", "0"))
        if key in _CACHE:
            return deepcopy(_CACHE[key])
        parsed = parse_tei(_post_pdf(pdf, fulltext=fulltext,
                                     timeout=90 if fulltext else 20), fulltext=fulltext)
    except GrobidNoContentError:
        return {"status": "no_content"}
    except (OSError, ValueError, ET.ParseError, HTTPError, URLError, TimeoutError) as exc:
        return {"status": "unavailable", "error": type(exc).__name__}
    result = {"status": "ok", "mode": "fulltext" if fulltext else "header",
              "header_consolidation": key[5],
              "citation_consolidation": key[6] if fulltext else "not_requested",
              **parsed}
    if len(_CACHE) >= 32:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = deepcopy(result)
    return result


def _fold(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in value if not unicodedata.combining(ch)).casefold()


def _contains_name_tokens(text, name):
    haystack = re.findall(r"[^\W\d_]+", _fold(text), re.UNICODE)
    needle = re.findall(r"[^\W\d_]+", _fold(name), re.UNICODE)
    return bool(needle and any(haystack[index:index + len(needle)] == needle
                               for index in range(len(haystack) - len(needle) + 1)))


def _exclude_editor_reviewer_credits(context, names, title_pages):
    """Exclude header names visible only inside explicit editorial role panels.

    The PDF's native reading order may place a sidebar before the article, so
    compare role sections with *all* other text on the same opening pages.
    Keep every name if no candidate is independently visible outside panels.
    """
    role_heading = re.compile(r"(?i)^(?:edited(?:\s+and\s+reviewed)?\s+by|reviewed\s+by)$")
    next_heading = re.compile(r"^\*?[A-Z][A-Z /&-]{2,55}$")
    inside = []
    outside = []
    for sample in context.opening_pages:
        page = int(sample.get("page") or 0)
        if not any(start <= page <= start + 1 for start in title_pages):
            continue
        lines = str(sample.get("text") or "").splitlines()
        excluded_lines = set()
        for index, line in enumerate(lines):
            if not role_heading.fullmatch(line.strip()):
                continue
            for next_index in range(index + 1, min(len(lines), index + 15)):
                if next_heading.fullmatch(lines[next_index].strip()):
                    break
                excluded_lines.add(next_index)
        inside.extend(lines[index] for index in sorted(excluded_lines))
        outside.extend(line for index, line in enumerate(lines) if index not in excluded_lines)
    if not inside:
        return names, []
    outside_text = " ".join(outside)
    inside_text = " ".join(inside)
    if not any(_contains_name_tokens(outside_text, name) for name in names):
        return names, []
    excluded = [name for name in names
                if _contains_name_tokens(inside_text, name)
                and not _contains_name_tokens(outside_text, name)]
    return [name for name in names if name not in excluded], excluded


def author_evidence(context, analysis):
    """Promote a GROBID header only when its title and byline are visible."""
    if analysis.get("status") != "ok":
        return {"author": "", "source": "not_found", "page": 0, "evidence": ""}
    title_tokens = words(_fold(analysis.get("title") or ""))
    opening = [(int(s.get("page") or 0), _fold(s.get("text") or ""))
               for s in context.opening_pages if 1 <= int(s.get("page") or 0) <= 3]
    if len(title_tokens) < 3 or len(title_tokens) > 60:
        return {"author": "", "source": "grobid_rejected_title", "page": 0, "evidence": ""}
    width = min(5, len(title_tokens))
    title_pages = set()
    for page, text in opening:
        visible = words(text)
        if any(visible[pos:pos + width] == title_tokens[start:start + width]
               for start in range(len(title_tokens) - width + 1)
               for pos in range(len(visible) - width + 1)):
            title_pages.add(page)
    if not title_pages:
        return {"author": "", "source": "grobid_rejected_title", "page": 0, "evidence": ""}
    names = [item["name"] for item in analysis.get("authors", [])]
    if not names or len(names) > 30:
        return {"author": "", "source": "grobid_rejected_names", "page": 0, "evidence": ""}
    names, excluded_role_credits = _exclude_editor_reviewer_credits(context, names, title_pages)
    if not names:
        return {"author": "", "source": "grobid_rejected_editor_reviewer_credits",
                "page": 0, "evidence": "", "excluded_role_credits": excluded_role_credits}
    found_pages = []
    for name in names:
        if (re.match(r"^(?:Dr|Mr|Mrs|Ms|Prof|Professor)\.?\s+", name, re.I)
                or author_candidate_is_document_role(name)
                or not looks_like_person_name(name)):
            return {"author": "", "source": "grobid_rejected_names", "page": 0, "evidence": ""}
        pieces = re.findall(r"[^\W\d_]+", _fold(name), flags=re.UNICODE)
        matches = [page for page, text in opening
                   if len(pieces) >= 2 and pieces[0] in text and pieces[-1] in text
                   and any(title_page <= page <= title_page + 1
                           for title_page in title_pages)]
        if not matches:
            return {"author": "", "source": "grobid_rejected_invisible_byline",
                    "page": 0, "evidence": ""}
        found_pages.append(matches[0])
    return {"author": ", ".join(names), "source": "grobid_header_author",
            "page": min(found_pages), "evidence": f"GROBID header: {analysis['title'][:160]}",
            "excluded_role_credits": excluded_role_credits}


def reconcile(context, existing, analysis):
    """Keep trusted local credits; use corroborated GROBID to fill weak ones."""
    candidate = author_evidence(context, analysis)
    current = dict(existing or {})
    if not candidate.get("author"):
        return current, candidate.get("source", "not_found")
    if current.get("author"):
        current_names = _fold(current["author"])
        new_names = _fold(candidate["author"])
        def identity_key(value):
            return re.sub(r"[^\w]", "", _fold(value), flags=re.UNICODE)
        if identity_key(current_names) == identity_key(new_names):
            return current, "agrees"
        if str(current.get("source") or "").startswith("filename_"):
            surname = re.findall(r"[^\W\d_]+", current_names, re.UNICODE)
            if len(surname) == 1 and surname[0] in re.findall(r"[^\W\d_]+", new_names, re.UNICODE):
                return candidate, "expanded_filename_surname"
        return current, "conflict_kept_existing"
    return candidate, "filled_unknown"
