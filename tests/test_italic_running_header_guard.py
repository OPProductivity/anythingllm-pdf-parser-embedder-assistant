from pathlib import Path

import fitz
import pytest

import auto_anythingllm_pipeline as pipeline


pytestmark = pytest.mark.offline_deterministic


def row(text, font, *, y0=20, y1=30, x0=40, x1=200):
    return {"text": text, "normalized": text, "fonts": [font],
            "x0": x0, "x1": x1, "y0": y0, "y1": y1}


@pytest.mark.parametrize("text,font,expected", [
    ("Robert Fanuzzi", "ITCStoneSerif-Medium", False),
    ("Robert Fanuzzi", "ITCStoneSerif-MediumItalic", True),
    ("Equiano [1785] 2003; Wheatley [1773] 1999).", "Times-Italic", False),
    ("Americana, panetnicidade, Hispano /Latino,", "ArnoPro-Italic", False),
    ("Zabala Ortiz", "GillSansMTPro-MediumItal", True),
    ("T.A. Seltzer-Rogers", "CharisSIL-Italic", True),
])
def test_running_name_requires_name_shaped_text_and_explicit_italic_font(text, font, expected):
    assert pipeline._layout_is_running_name(row(text, font)) is expected


def test_running_name_abstains_below_overlapping_page_content():
    caption = row("Participant Demographics", "Times-Italic", y0=82, y1=92)
    label = row("TABLE 1", "Times-Roman", y0=67, y1=77)
    assert pipeline._layout_is_running_name(caption)
    assert not pipeline._layout_is_running_name(caption, [label, caption])


def test_native_layout_keeps_citation_and_caption_but_removes_true_head(tmp_path: Path):
    pdf_path = tmp_path / "native-layout.pdf"
    document = fitz.open()
    for number in range(1, 4):
        page = document.new_page(width=612, height=792)
        if number == 1:
            page.insert_text((320, 95), "Equiano [1785] 2003; Wheatley [1773] 1999).",
                             fontsize=10, fontname="heit")
        elif number == 2:
            page.insert_text((40, 72), "TABLE 1", fontsize=10)
            page.insert_text((40, 92), "Participant Demographics", fontsize=10,
                             fontname="heit")
        else:
            page.insert_text((40, 32), "Josephine A. Ruggiero", fontsize=10,
                             fontname="heit")
        for line in range(12):
            page.insert_text((40, 140 + line * 22),
                             f"page {number} ordinary body prose line {line} remains readable.",
                             fontsize=10)
    document.save(pdf_path)
    document.close()
    with fitz.open(pdf_path) as source:
        pages = [{"page": index + 1, "text": page.get_text("text")}
                 for index, page in enumerate(source)]

    transformed, evidence = pipeline.apply_region_aware_native_layout(pdf_path, pages)

    assert "Equiano [1785] 2003; Wheatley [1773] 1999)." in transformed[0]["text"]
    assert "Participant Demographics" in transformed[1]["text"]
    assert "Josephine A. Ruggiero" not in transformed[2]["text"]
    assert any(entry["reason"] == "italic_running_author"
               for entry in evidence["pages"][2]["removed_marginalia"])
