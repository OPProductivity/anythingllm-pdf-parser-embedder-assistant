from unittest.mock import patch
import pytest
from PIL import Image, ImageDraw
import auto_anythingllm_pipeline as a
import rag_pdf_tools as t
import rag_pdf_gradio_app as g

pytestmark = pytest.mark.offline_deterministic


def test_underlined_region_changes_layout_not_pixels():
    im = Image.new("L", (850, 950), 255)
    draw = ImageDraw.Draw(im)
    for x in [100, 220, 350]:
        draw.line((x, 450, x + 60, 450), fill=0, width=1)
    assert t.annotated_text_block_psm(im) == 4  # rules alone are not prose
    for y in range(50, 400, 15):
        for x in range(50, 750, 15):
            draw.rectangle((x, y, x + 4, y + 7), fill=0)
    assert t.annotated_text_block_psm(im) == 4  # detached rules still aren't underlines
    for y in (105, 210, 315):
        draw.line((100, y, 300, y), fill=0, width=1)
    before = im.tobytes()
    assert t.annotated_text_block_psm(im) == 6
    assert im.tobytes() == before
    assert t.annotated_text_block_psm(im, 3) == 3
    highlighted = im.convert("RGB")
    ImageDraw.Draw(highlighted).rectangle((100, 500, 700, 530), fill=(255, 255, 0))
    assert t.annotated_text_block_psm(highlighted) == 4
    assert t.annotated_text_block_psm(Image.new("L", (850, 950), 255)) == 4
    draw.rectangle((0, 0, 849, 12), fill=0)
    assert t.annotated_text_block_psm(im) == 4  # scan frame is not annotation


def test_music_staff_keeps_existing_layout():
    im = Image.new("L", (850, 950), 255)
    draw = ImageDraw.Draw(im)
    for y in range(50, 400, 15):
        for x in range(50, 750, 15):
            draw.rectangle((x, y, x + 4, y + 7), fill=0)
    for y in range(600, 630, 6):
        draw.line((100, y, 650, y), fill=0, width=1)
    assert t.annotated_text_block_psm(im) == 4


def test_single_scan_spine_keeps_existing_layout():
    im = Image.new("L", (850, 950), 255)
    draw = ImageDraw.Draw(im)
    for y in range(50, 850, 15):
        for x in range(180, 750, 15):
            draw.rectangle((x, y, x + 4, y + 7), fill=0)
        draw.rectangle((35, y, 39, y + 7), fill=0)
    draw.line((100, 50, 100, 850), fill=0, width=1)
    for x in [250, 400, 550]:
        draw.line((x, 900, x + 60, 900), fill=0, width=1)
    assert t.annotated_text_block_psm(im) == 4


def test_ruled_table_keeps_existing_layout():
    im = Image.new("L", (850, 950), 255)
    draw = ImageDraw.Draw(im)
    for x in [100, 400, 700]:
        draw.line((x, 100, x, 850), fill=0, width=2)
    for y in [100, 400, 700]:
        draw.line((100, y, 700, y), fill=0, width=2)
    assert t.annotated_text_block_psm(im) == 4


def test_optional_model_is_scoped_and_integrity_checked(tmp_path):
    assert not t.annotated_model_arguments(4, 4)
    assert not t.annotated_model_arguments(3, 3)
    bad = tmp_path / "eng.traineddata"
    bad.write_bytes(b"not a trained model")
    assert not t._verified_annotated_model(
        str(bad), bad.stat().st_size, bad.stat().st_mtime_ns
    )
    with patch.object(t, "_verified_annotated_model", return_value=False):
        assert not t.annotated_model_arguments(4, 6)


def test_short_book_title_keeps_both_explicit_editors():
    samples = [
        {
            "page": 1,
            "text": "Sound, Media, Ecology\nEdited by\nMilena Droumeva · Randolph Jordan",
        },
        {
            "page": 4,
            "text": "Milena Droumeva • Randolph Jordan\nEditors\nSound, Media,\nEcology",
        },
    ]
    assert (
        a.infer_author_from_text_samples(samples, "Sound, Media, Ecology")["author"]
        == "Milena Droumeva, Randolph Jordan"
    )
    assert (
        a.infer_author_from_text_samples(
            [
                {
                    "page": 1,
                    "text": "John Smith • Jane Smith\nSeries Editors\nSound, Media, Ecology",
                }
            ],
            "Sound, Media, Ecology",
        ).get("source")
        != "text_edited_by"
    )


def candidate(pages=305):
    return {
        "segments": [{}],
        "start_page": 2,
        "quality": {
            "included_pages": pages,
            "included_words": 100000,
            "empty_pages": 0,
            "scanned_likelihood": "low",
            "text_integrity_status": "not_flagged",
        },
        "native_chunk_eval": {"status": "pass"},
    }


def test_excluded_cover_not_counted_as_missing_selected_text():
    assert a.has_complete_native_text_candidate([candidate()], 306)
    assert not a.has_complete_native_text_candidate([candidate(304)], 306)
    assert not a.has_complete_native_text_candidate(
        [dict(candidate(), start_page=307)], 306
    )
    assert not a.has_complete_native_text_candidate(
        [dict(candidate(), error="failed")], 306
    )
    assert not a.has_complete_native_text_candidate(
        [dict(candidate(), native_chunk_eval={"status": "fail"})], 306
    )


def test_fresh_reprice_does_not_amplify_opening_discount():
    params = dict(
        previous_expected_seconds=1035,
        previous_presentation_expected_seconds=725,
        new_expected_seconds=859,
        is_material_reprice=True,
        elapsed_seconds=442,
    )
    assert g.reprice_presentation_expected_seconds(**params) == 642
    assert (
        g.reprice_presentation_expected_seconds(**params, minimum_remaining_ratio=0.7)
        == 725
    )
    assert (
        g.reprice_presentation_expected_seconds(
            **dict(params, is_material_reprice=False), minimum_remaining_ratio=0.7
        )
        == 725
    )


def test_image_only_review_does_not_claim_missing_body_text():
    r = a.visual_text_coverage_review([], 1, [{"pdf_page": 1, "image_count": 1}])
    assert r["unresolved_page_count"] == 1
    assert "does not distinguish" in r["message"]
    assert "publisher logos" in r["message"]
