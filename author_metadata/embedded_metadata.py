"""Acquire untrusted embedded bibliographic proposals, never author verdicts."""

import re
from xml.etree import ElementTree


def read(document):
    """Read Info and bounded, entity-free XMP during this PDF's acquisition."""
    info = dict(getattr(document, "metadata", None) or {})
    result = {"info": {k: str(info.get(k) or "") for k in (
        "author", "title", "subject", "keywords")}, "xmp": {}, "diagnostics": []}
    getter = getattr(document, "get_xml_metadata", None)
    if getter is None:
        return result
    try:
        xml = getter() or ""
    except Exception as error:
        # PyMuPDF's FzErrorFormat is not a RuntimeError. A malformed optional
        # XMP object must not abort native/image acquisition for the document.
        result["diagnostics"].append({"stage": "xmp_read", "error": type(error).__name__})
        return result
    if not xml.strip():
        return result
    if len(xml) > 2_000_000 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.I):
        result["diagnostics"].append({"stage": "xmp_parse", "error": "UnsupportedXML"})
        return result
    try:
        tree = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        result["diagnostics"].append({"stage": "xmp_parse", "error": "ParseError"})
        return result
    namespaces = {"dc": "http://purl.org/dc/elements/1.1/",
                  "pdf": "http://ns.adobe.com/pdf/1.3/"}
    for prefix, local in (("dc", "creator"), ("pdf", "Author"), ("dc", "title"),
                          ("dc", "description"), ("dc", "rights")):
        tag = "{" + namespaces[prefix] + "}" + local
        values = []
        for node in tree.iter():
            if node.tag == tag:
                items = node.findall(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}li")
                values.extend(" ".join(item.itertext()).strip() for item in items or [node])
            if tag in node.attrib:
                values.append(node.attrib[tag].strip())
        result["xmp"][prefix + ":" + local] = list(dict.fromkeys(v for v in values if v))
    return result
