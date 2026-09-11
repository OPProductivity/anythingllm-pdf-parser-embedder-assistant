from copy import deepcopy

import pytest
import auto_anythingllm_pipeline as p

pytestmark = pytest.mark.offline_deterministic


def test_whole_file_compacts_blank_lines_and_preserves_spans():
    rows = [
        {"pdf_page": 2, "logical_page": "12", "text": "First\n\nSecond\n \t\nThird",
         "document_region": "body"},
        {"pdf_page": 3, "logical_page": "13", "text": "Fourth\nFifth",
         "document_region": "body"},
    ]
    original = deepcopy(rows)
    result = p.collapse_unsegmented_document_segments(rows, "abc")[0]
    assert result["text"] == "First\nSecond\nThird Fourth\nFifth"
    assert rows == original
    assert [result["text"][s["text_char_start"]:s["text_char_end"]]
            for s in result["page_spans"]] == ["First\nSecond\nThird", "Fourth\nFifth"]
    assert p.generate_upload_text([result], include_markers=False) == result["text"]


def test_whole_file_preserves_same_page_regions_but_not_page_breaks():
    rows = [dict(pdf_page=2, text="First region."),
            dict(pdf_page=2, text="Second region."),
            dict(pdf_page=3, text="  "),
            dict(pdf_page=4, text="Next page.")]
    result = p.collapse_unsegmented_document_segments(rows, "abc")[0]
    assert result["text"] == "First region.\nSecond region. Next page."
    assert [result["text"][s["text_char_start"]:s["text_char_end"]]
            for s in result["page_spans"]] == ["First region.", "Second region.", "Next page."]


def test_ordinary_export_does_not_compact_blank_lines():
    assert p.generate_upload_text([{"text": "First\n\nSecond"}, {"text": "Third"}],
                                  include_markers=False) == "First\n\nSecond\n\nThird"
