import re
from collections import Counter

import pytest

from rag_pdf_tools import relocate_bottom_caption_from_narrow_scan_prose


pytestmark = pytest.mark.offline_deterministic


def word(text, left, top, line, number):
    return {
        "text": text, "left": left, "top": top, "width": 20, "height": 10,
        "block": 1, "paragraph": 1, "line": line, "word": number,
        "crop_width": 1000, "crop_height": 1000,
    }


def narrow_story_with_side_caption():
    rows = [word("body", 20, 50 + index * 4, index, 1) for index in range(160)]
    for line, top, words in [
        (200, 850, ["left", "story", "one", "caption", "first"]),
        (201, 870, ["left", "story", "two", "caption", "second"]),
    ]:
        positions = [20, 60, 100, 550, 600]
        rows.extend(word(text, left, top, line, number)
                    for number, (text, left) in enumerate(zip(words, positions), 1))
    text = "\n".join([
        "Earlier story body.",
        "left story one caption first",
        "left story two caption second",
    ])
    return text, rows


def tokens(text):
    return Counter(re.findall(r"\w+|[^\w\s]", text))


def test_bottom_side_caption_moves_after_story_without_changing_tokens():
    original, rows = narrow_story_with_side_caption()
    result, evidence = relocate_bottom_caption_from_narrow_scan_prose(original, rows)
    assert evidence["applied"]
    assert result.startswith("Earlier story body.\nleft story one\nleft story two")
    assert result.rstrip().endswith("caption first\ncaption second")
    assert tokens(result) == tokens(original)


def test_side_caption_rule_abstains_without_two_lines_or_exact_ocr_text():
    original, rows = narrow_story_with_side_caption()
    single_line = [row for row in rows if row["line"] != 201]
    unchanged, evidence = relocate_bottom_caption_from_narrow_scan_prose(original, single_line)
    assert unchanged == original
    assert not evidence["applied"]

    mismatched = original.replace("caption first", "different caption")
    unchanged, evidence = relocate_bottom_caption_from_narrow_scan_prose(mismatched, rows)
    assert unchanged == mismatched
    assert evidence["reason"] == "unique_ocr_line_absent"


def test_side_caption_rule_abstains_without_geometry_or_sustained_narrow_prose():
    original, rows = narrow_story_with_side_caption()
    assert relocate_bottom_caption_from_narrow_scan_prose(original, [])[0] == original
    unchanged, evidence = relocate_bottom_caption_from_narrow_scan_prose(original, rows[100:])
    assert unchanged == original
    assert evidence["reason"] == "sustained_narrow_prose_not_confirmed"
