"""Work/role evidence and scale invariance for an author above a chapter title."""
from copy import deepcopy

import pytest

from author_metadata.chapter_geometry import claims

pytestmark = pytest.mark.offline_deterministic


def line(text, x, y, width=160, height=12):
    return {"text": text, "box": [x, y, x + width, y + height], "size": height, "confidence": 95}


def source(scale=2.5):
    pages = [
        [line("CHAPTER 12", 150, 30), line("AMELIA HART", 155, 70),
         line("A CHANGING LANDSCAPE", 155, 105), line("Communities and shared responsibility", 155, 130)],
        [line("201 AMELIA HART", 30, 20), line("A CHANGING LANDSCAPE 202", 320, 20),
         line("Ordinary chapter prose with no other personal credit.", 30, 150)],
        [line("203 AMELIA HART", 30, 20), line("Ordinary chapter prose continues.", 30, 150)],
    ]
    regions = []
    for page, lines in enumerate(pages, 1):
        scaled = deepcopy(lines)
        for item in scaled:
            item["box"] = [v * scale for v in item["box"]]
            item["size"] *= scale
        regions.append({"page": page, "width": 600 * scale, "height": 800 * scale,
            "clip": [0, 0, 600, 800], "scale": scale, "lines": scaled,
            "text": "\n".join(item["text"] for item in lines)})
    native = [{"page": page, "width": 600, "height": 800, "text": "", "horizontal_lines": []}
              for page in (1, 2, 3)]
    return {"ocr": regions, "native": native, "trace": {"document_page_count": 3}}


@pytest.mark.parametrize("scale", [1, 1.25, 2.5, 4])
def test_same_physical_opening_and_heads_give_the_same_claim_at_each_scale(scale):
    bundle = source(scale)
    before = deepcopy(bundle)
    found = claims(bundle, kind="generic", work_scope="single_work")
    assert [c["author"] for c in found] == ["AMELIA HART"]
    assert found[0]["corroboration_pages"] == [2, 3]
    assert found[0]["geometry"]["byline"] == [155, 70, 315, 82]
    assert found[0]["title_corroboration"] == ["A CHANGING LANDSCAPE"]
    assert bundle == before


def test_name_in_adjacent_previous_leaf_notes_does_not_belong_to_the_marker_column():
    bundle = source(1)
    bundle["ocr"][0]["lines"][1]["box"] = [5, 70, 100, 82]
    assert claims(bundle, kind="generic", work_scope="single_work") == []


def test_interior_crop_top_does_not_supply_a_false_running_head():
    bundle = source(1)
    for page in bundle["native"]:
        page["height"] = 1200
    for region in bundle["ocr"][1:]:
        region["clip"] = [0, 300, 600, 1100]
    assert claims(bundle, kind="generic", work_scope="single_work") == []


def test_unknown_page_or_crop_geometry_cannot_justify_a_new_header_claim():
    bundle = source(1)
    bundle["native"] = []
    assert claims(bundle, kind="generic", work_scope="single_work") == []
    bundle = source(1)
    for region in bundle["ocr"]:
        region.pop("clip")
    assert claims(bundle, kind="generic", work_scope="single_work") == []


def test_two_repeated_header_readings_on_one_page_are_not_two_independent_pages():
    bundle = source(1)
    bundle["ocr"][2]["page"] = 2
    assert claims(bundle, kind="generic", work_scope="single_work") == []


def test_title_header_must_agree_with_the_following_local_title():
    bundle = source(1)
    bundle["ocr"][1]["lines"][1]["text"] = "AN UNRELATED VOLUME 202"
    assert claims(bundle, kind="generic", work_scope="single_work") == []


def test_an_alternate_ocr_reading_can_supply_the_missing_chapter_opening():
    bundle = source(1)
    opening = bundle["ocr"][0]
    bundle["ocr"][0] = {**opening, "text": "Unreadable first segmentation", "lines": [], "alternate": opening}
    assert [c["author"] for c in claims(bundle, kind="generic", work_scope="single_work")] == ["AMELIA HART"]


def test_repeated_topic_words_in_prose_do_not_become_a_personal_credit():
    bundle = source(1)
    for region in bundle["ocr"]:
        for item in region["lines"]:
            item["text"] = item["text"].replace("AMELIA HART", "CULTURAL MEMORY")
    bundle["ocr"][1]["lines"].extend([
        line("Cultural memory can be studied in its social context.", 30, 210),
        line("Another account of cultural memory concerns transmission.", 30, 240)])
    bundle["ocr"][2]["lines"].append(line("The topic of cultural memory returns here.", 30, 210))
    assert claims(bundle, kind="generic", work_scope="single_work") == []


def test_a_writer_can_cite_their_own_work_without_losing_local_authorship():
    bundle = source(1)
    bundle["ocr"][1]["lines"].extend([
        line("See Amelia Hart, an earlier contribution, for further details.", 30, 210),
        line("Amelia Hart, The Earlier Study, chapter 2.", 30, 240)])
    bundle["ocr"][2]["lines"].append(line("Amelia Hart, Another Contribution, pages 12-14.", 30, 210))
    assert [c["author"] for c in claims(bundle, kind="generic", work_scope="single_work")] == ["AMELIA HART"]


@pytest.mark.parametrize("kind,scope", [("generic", "whole_issue"), ("book", "whole_book"), ("report", "single_work")])
def test_chapter_relation_does_not_assign_a_whole_issue_or_other_selected_work(kind, scope):
    assert claims(source(), kind=kind, work_scope=scope) == []
