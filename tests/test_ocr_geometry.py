"""Image scale may change pixels, not the physical layout seen by experts."""

from copy import deepcopy

import pytest

from author_metadata.ocr_geometry import region_in_pdf_points

pytestmark = pytest.mark.offline_deterministic


def reading(scale):
    return {
        "page": 1, "scale": scale, "width": 400 * scale, "height": 600 * scale,
        "clip": [50, 20, 450, 620], "coordinate_space": "crop_pixels",
        "text": "CHAPTER 8\nAMELIA HART\nA LOCAL CONTRIBUTION",
        "lines": [
            {"text": "CHAPTER 8", "box": [20 * scale, 20 * scale, 100 * scale, 30 * scale], "size": 10 * scale, "confidence": 90},
            {"text": "AMELIA HART", "box": [20 * scale, 50 * scale, 160 * scale, 65 * scale], "size": 15 * scale, "baseline": 65 * scale, "confidence": 95},
        ],
    }


@pytest.mark.parametrize("scale", [1, 1.25, 2, 2.5, 4])
def test_points_are_invariant_across_recorded_raster_scales(scale):
    source = reading(scale)
    before = deepcopy(source)
    points = region_in_pdf_points(source)
    assert points["width"] == 400
    assert points["height"] == 600
    assert points["lines"][1]["box"] == [20, 50, 160, 65]
    assert points["lines"][1]["baseline"] == 65
    assert points["lines"][1]["size"] == 15
    assert points["lines"][1]["confidence"] == 95
    assert points["text"] == source["text"]
    assert points["clip"] == source["clip"]
    assert source == before
    points["lines"][0]["text"] = "Changed only in interpretation"
    assert source == before


def test_alternate_reading_keeps_its_own_scale_and_is_not_mixed_into_view():
    source = reading(2.5)
    source["alternate"] = reading(2)
    points = region_in_pdf_points(source)
    alternate = region_in_pdf_points(source["alternate"])
    assert "alternate" not in points
    assert points["lines"] == alternate["lines"]
    assert source["alternate"]["scale"] == 2


@pytest.mark.parametrize("scale", [None, True, 0, -1, float("nan"), float("inf"), "2"])
def test_scale_is_never_silently_guessed(scale):
    source = reading(2)
    source["scale"] = scale
    with pytest.raises(ValueError, match="raster scale"):
        region_in_pdf_points(source)


def test_a_point_view_cannot_be_converted_twice():
    with pytest.raises(ValueError, match="raw crop-pixel"):
        region_in_pdf_points(region_in_pdf_points(reading(2)))
