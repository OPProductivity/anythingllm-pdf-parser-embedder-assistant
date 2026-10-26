from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import fitz
import pytest

from author_metadata.native_reads import _copy_native
from author_metadata import field_map, names
import rag_pdf_tools as tools

pytestmark = pytest.mark.offline_deterministic


def test_native_copies_preserve_values_and_separate_all_mutable_containers(tmp_path):
    source = tmp_path / "page.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 60), "Barbara Foley\nHistorical Materialism")
        doc.save(source)
    with fitz.open(source) as doc:
        page = doc[0]
        for value in [page.get_text("words"), page.get_text("dict"), page.get_texttrace(),
                      page.get_image_info(), field_map.native_page_atoms(page, 1)]:
            cloned = _copy_native(value)
            assert cloned == deepcopy(value)

            def separate(left, right):
                if isinstance(left, (dict, list)):
                    assert left is not right
                    children = left.items() if isinstance(left, dict) else enumerate(left)
                    for key, child in children:
                        separate(child, right[key])
                elif isinstance(left, tuple):
                    for child, copy in zip(left, right):
                        separate(child, copy)

            separate(value, cloned)


def test_copy_handles_aliases_cycles_mutable_tuple_children_and_extensions():
    class Extension:
        def __init__(self):
            self.values = [1]

        def __deepcopy__(self, memo):
            result = Extension()
            memo[id(self)] = result
            result.values = deepcopy(self.values, memo)
            return result

    shared = []
    mixed = (shared,)
    shared.append(mixed)
    source = {"a": shared, "b": shared, "extension": Extension(),
              "coordinates": (1, (2.0, 3.0))}
    cloned = _copy_native(source)
    assert cloned is not source and cloned["a"] is cloned["b"]
    assert cloned["a"] is not shared
    assert cloned["a"][0][0] is cloned["a"]
    assert cloned["extension"] is not source["extension"]
    cloned["extension"].values.append(2)
    assert source["extension"].values == [1]
    assert cloned["coordinates"] == source["coordinates"]
    assert _copy_native({1: [2]}) == {1: [2]}


def test_opening_map_reuses_only_lexical_work_and_expires_after_each_call(tmp_path):
    source = tmp_path / "page.pdf"
    with fitz.open() as doc:
        for _ in range(2):
            page = doc.new_page()
            page.insert_text((50, 60), "Historical Materialism\nBy Barbara Foley")
        doc.save(source)
    calls = []
    original = names.normalize_text

    def count(value):
        calls.append(value)
        return original(value)

    with patch.object(names, "normalize_text", count):
        direct = field_map.map_pdf_opening.__wrapped__(Path(source), "Historical Materialism")
        uncached_count = len(calls)
        calls.clear()
        first = field_map.map_pdf_opening(Path(source), "Historical Materialism")
        first_count = len(calls)
        second = field_map.map_pdf_opening(Path(source), "Historical Materialism")
    assert first == second == direct
    assert first_count < uncached_count
    assert len(calls) == first_count * 2
    assert first[0] is not second[0]
    first[0]["groups"].clear()
    assert second[0]["groups"]


@pytest.mark.parametrize("with_layout", [False, True])
def test_ocr_transport_preserves_exact_preprocessed_crop_pixels(with_layout):
    from PIL import Image, ImageDraw, ImageOps
    from types import SimpleNamespace

    image = Image.new("RGB", (200, 160), "yellow")
    ImageDraw.Draw(image).rectangle((45, 40, 115, 90), fill="black")
    fraction = (.1, .2, .8, .9)
    expected = ImageOps.autocontrast(image.crop((20, 32, 160, 144)).convert("L"))
    calls = []

    def recognize(command, evidence):
        with Image.open(command[1]) as received:
            assert received.mode == expected.mode
            assert received.size == expected.size
            assert received.tobytes() == expected.tobytes()
        assert command[command.index("--psm") + 1] == "4"
        assert command[command.index("-l") + 1] == "eng"
        calls.append(command)
        if with_layout:
            Path(command[2]).with_suffix(".txt").write_text("By Barbara Foley", encoding="utf8")
            Path(command[2]).with_suffix(".tsv").write_text("header\n", encoding="utf8")
        return SimpleNamespace(returncode=0, stdout="By Barbara Foley")

    function = tools._ocr_photographed_crop_with_layout if with_layout else tools._ocr_photographed_crop
    with patch.object(tools, "_resolve_ocr_recognition", return_value=(4, [], {})), \
            patch.object(tools, "_run_measured_tesseract", recognize):
        result = function(image, fraction, "tesseract", ImageOps)
    assert len(calls) == 1
    assert (result[0] if with_layout else result) == "By Barbara Foley"
