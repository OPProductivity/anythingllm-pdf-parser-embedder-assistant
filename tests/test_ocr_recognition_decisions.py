from pathlib import Path
from contextlib import ExitStack
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw, ImageOps

import rag_pdf_tools as tools

pytestmark = pytest.mark.offline_deterministic


def annotated_region():
    image = Image.new("L", (850, 950), 255)
    draw = ImageDraw.Draw(image)
    for y in range(50, 400, 15):
        for x in range(50, 750, 15):
            draw.rectangle((x, y, x + 4, y + 7), fill=0)
    for x in (100, 220, 350):
        draw.line((x, 450, x + 60, 450), fill=0)
    return image


def test_missing_model_restores_the_complete_original_route():
    with patch.object(tools, "annotated_model_arguments", return_value=[]):
        psm, arguments, decision = tools._resolve_ocr_recognition(annotated_region(), 4)
    assert (psm, arguments) == (4, [])
    assert decision["candidate_psm"] == 6
    assert decision["psm"] == 4
    assert decision["model"] == "installed_eng"
    assert decision["route_reason"] == "annotated_model_unavailable_original_route"


def test_invalid_model_restores_original_route():
    with patch.object(tools, "_verified_annotated_model", return_value=False):
        psm, arguments, decision = tools._resolve_ocr_recognition(annotated_region(), 4)
    assert (psm, arguments) == (4, [])
    assert decision["underlined_prose_detected"] is True


@pytest.mark.parametrize("requested", [3, 6, 7, 10, 13])
def test_explicit_layout_is_preserved(requested):
    psm, arguments, decision = tools._resolve_ocr_recognition(
        annotated_region(), requested
    )
    assert (psm, arguments) == (requested, [])
    assert decision["geometry_reason"] == "explicit_layout_preserved"
    assert not decision["underlined_prose_detected"]


def test_probe_failure_is_visible_and_nonblocking():
    class BrokenImage:
        def convert(self, *args, **kwargs):
            raise RuntimeError("simulated image probe failure")

    psm, arguments, decision = tools._resolve_ocr_recognition(BrokenImage(), 4)
    assert (psm, arguments) == (4, [])
    assert decision["geometry_reason"] == "geometry_probe_unavailable"
    assert decision["probe_error_type"] == "RuntimeError"


@pytest.mark.parametrize("with_layout", [False, True])
@pytest.mark.parametrize("model_available", [False, True])
def test_subprocess_and_retained_decision_agree(with_layout, model_available):
    evidence = {"obsolete_previous_attempt": True}
    args = (
        ["--tessdata-dir", "qualified-model", "--oem", "1"] if model_available else []
    )
    captured = []

    def run(command, **kwargs):
        captured.append(command)
        if with_layout:
            Path(command[2]).with_suffix(".txt").write_text(
                "Actual recognized words", encoding="utf8"
            )
            Path(command[2]).with_suffix(".tsv").write_text(
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
                "5\t1\t1\t1\t1\t1\t10\t10\t30\t10\t95\tActual\n",
                encoding="utf8",
            )
        return SimpleNamespace(returncode=0, stdout="Actual recognized words")

    operation = (
        tools._ocr_photographed_crop_with_layout
        if with_layout
        else tools._ocr_photographed_crop
    )
    with (
        patch.object(tools, "annotated_model_arguments", return_value=args),
        patch.object(
            tools, "annotated_text_block_psm", wraps=tools.annotated_text_block_psm
        ) as probe,
        patch.object(tools.subprocess, "run", side_effect=run),
    ):
        result = operation(
            annotated_region(),
            (0, 0, 1, 1),
            "tesseract",
            ImageOps,
            recognition_evidence=evidence,
        )
    assert probe.call_count == 1
    assert len(captured) == 1  # no hidden recovery or extra OCR
    command = captured[0]
    expected = 6 if model_available else 4
    assert command[command.index("--psm") + 1] == str(expected)
    assert ("--tessdata-dir" in command) == model_available
    assert evidence["psm"] == expected
    assert evidence["crop_fraction"] == [0, 0, 1, 1]
    assert "obsolete_previous_attempt" not in evidence
    if with_layout:
        assert result[1][0]["ocr_psm"] == expected
        assert result[1][0]["ocr_model"] == evidence["model"]


def test_valid_bundled_asset_is_usable():
    # Packaging qualification for the data file shipped with this checkout.
    psm, arguments, decision = tools._resolve_ocr_recognition(annotated_region(), 4)
    assert psm == 6 and "--tessdata-dir" in arguments
    assert decision["model"] == "tessdata_best_eng"


@pytest.mark.parametrize(
    "case,selected",
    [
        ("display", "display"),
        ("full_selected", "full"),
        ("full_retained", "base"),
        ("text_fallback", "text_fallback"),
    ],
)
def test_region_reports_selected_call_not_last_attempt(case, selected):
    image = Image.new("RGB", (800, 1100), "white")
    pixmap = SimpleNamespace(width=800, height=1100, samples=image.tobytes())
    page = SimpleNamespace(get_text=lambda *_: "", get_pixmap=lambda **_: pixmap)
    fraction = (0.1, 0.1, 0.9, 0.9)
    body = "Trustworthy ordinary recognized source words. " * 30

    def with_layout(*args, recognition_evidence, **kwargs):
        recognition_evidence.update(psm=4, test_call="base")
        return (
            "" if case == "text_fallback" else "Title" if case == "display" else body
        ), []

    def text_only(image, crop, *args, psm=4, recognition_evidence=None, **kwargs):
        tag = "display" if psm == 3 else "text_fallback" if crop == fraction else "full"
        recognition_evidence.update(psm=psm, test_call=tag, crop_fraction=list(crop))
        return "An authoritative title by an actual author" if psm == 3 else body

    values = {
        "photographed_spread_crop_specs": [],
        "photographed_portrait_neighbour_sliver": None,
        "embedded_scanned_image_fraction": fraction,
        "photographed_three_column_signal": {"detected": False},
        "adaptive_ocr_crop_fraction": (fraction, {}),
        "ocr_crop_boundary_evidence": {},
        "embedded_scan_crop_needs_full_page_retry": case.startswith("full_"),
        "full_page_ocr_retry_materially_better": case == "full_selected",
        "credible_short_ocr_display_text": True,
    }
    with ExitStack() as stack:
        for name, value in values.items():
            stack.enter_context(patch.object(tools, name, return_value=value))
        for name in (
            "recover_geometry_aligned_drop_caps",
            "recover_missing_display_regions",
        ):
            stack.enter_context(
                patch.object(tools, name, side_effect=lambda text, *a, **kw: (text, {}))
            )
        stack.enter_context(
            patch.object(
                tools, "_ocr_photographed_crop_with_layout", side_effect=with_layout
            )
        )
        stack.enter_context(
            patch.object(tools, "_ocr_photographed_crop", side_effect=text_only)
        )
        regions = tools.photographed_page_ocr_regions(
            page, {"tesseract_executable": sys.executable}, page_number=1
        )
    assert len(regions) == 1
    assert regions[0]["recognition_layout"]["test_call"] == selected
    assert regions[0]["recognition_layout"]["psm"] == (3 if case == "display" else 4)
