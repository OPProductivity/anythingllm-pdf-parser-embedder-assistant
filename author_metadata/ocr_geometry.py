"""Explicit geometry views of an already acquired OCR reading.

Native PDF layouts use points while Tesseract returns pixels. Fixed-distance
rules cannot compare those units directly. This view changes coordinates,
not image pixels, OCR text, confidence, relative geometry or contributor roles.
Raw acquired evidence must remain available separately.
"""

from copy import deepcopy
import math


def region_in_pdf_points(region):
    """Convert one reading to crop-relative PDF points without modifying it.

    ``scale`` is the actual raster matrix factor recorded by collection; it
    is not a guessed DPI from a filename or image metadata. Alternate OCR
    readings can use another scale and must be converted independently.
    The returned view is for layout experts, not another collector input.
    """
    scale = region.get("scale")
    if type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0:
        raise ValueError("OCR geometry requires its recorded positive raster scale.")
    if region.get("coordinate_space") not in (None, "crop_pixels"):
        raise ValueError("Only raw crop-pixel geometry can be converted.")
    view = deepcopy({key: value for key, value in region.items() if key != "alternate"})
    for key in ("width", "height"):
        value = region.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"OCR geometry requires a positive {key}.")
        view[key] = value / scale
    for line in view.get("lines", []):
        box = line.get("box")
        if (not isinstance(box, (list, tuple)) or len(box) != 4
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in box)):
            raise ValueError("OCR line has invalid crop coordinates.")
        line["box"] = [value / scale for value in box]
        for key in ("size", "baseline"):
            if key in line:
                line[key] /= scale
    view.update(
        scale=1.0,
        coordinate_space="crop_points",
        geometry_normalization={
            "source_scale": scale,
            "source_units": "crop_pixels",
            "units": "PDF points relative to the original crop",
            "raw_reading_preserved": True,
            "font_size_caution": "OCR size is a median glyph-box height, not a recovered PDF font size",
        },
    )
    return view


def layout_trial_reports(region):
    """Compare both unit interpretations without selecting an author verdict.

    This is a laboratory diagnostic. The application has not adopted its new
    proposals or changed existing priority values. No extraction is performed.
    """
    from .native_article import credit_reports, alternating_credit_reports
    from .portable_layout import layout_credit_reports, publisher_credit_reports

    points = region_in_pdf_points(region)
    result = {}
    for units, view in (("legacy_crop_pixels", region), ("crop_points", points)):
        lines = [{**line, "baseline": line.get("baseline", line["box"][3])} for line in view["lines"]]
        reports = [
            *publisher_credit_reports(lines, page=view["page"]),
            *layout_credit_reports(lines, view["width"], view["height"], page=view["page"]),
            *credit_reports(lines, view["width"], view["height"]),
            *alternating_credit_reports(lines, view["width"], view["height"]),
        ]
        result[units] = [{**report, "geometry_coordinate_space": units} for report in reports]
    return result
