"""Malformed optional publisher XMP must not discard readable PDF evidence."""
import fitz
import pytest

import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


def test_non_stream_xmp_preserves_standard_metadata_and_native_author_samples(tmp_path):
    path = tmp_path / "broken-xmp.pdf"
    doc = fitz.open()
    doc.set_metadata({"title": "Readable Work", "author": "Celia Rivers"})
    page = doc.new_page()
    page.insert_text((72, 72), "Readable Work\nby Celia Rivers\nReadable native page evidence.")
    xref = doc.get_new_xref()
    doc.update_object(xref, "<< /Type /Metadata /Subtype /XML >>")
    doc.xref_set_key(doc.pdf_catalog(), "Metadata", f"{xref} 0 R")
    doc.save(path)
    doc.close()
    with fitz.open(path) as damaged:
        with pytest.raises(Exception, match="not a stream"):
            damaged.get_xml_metadata()
    result = pipeline.pdf_metadata(path, include_author_samples=True)
    assert result["title"] == "Readable Work"
    assert result["author"] == "Celia Rivers"
    assert result["pdf_page_count"] == 1
    assert result["publisher"] == ""
    assert "not a stream" in result["publisher_error"]
    assert "Celia Rivers" in str(result["_author_text_samples"])
