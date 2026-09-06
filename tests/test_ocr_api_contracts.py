"""Check narrow dependency-call corrections without changing OCR selection."""
from types import SimpleNamespace

import cv2
import fitz
import numpy as np
import pytest

import rag_pdf_tools as tools

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize('seed', range(20))
def test_explicit_connectivity_preserves_previous_component_results(seed):
    rng = np.random.default_rng(seed)
    mask = (rng.random((120, 160)) < .25).astype('uint8')
    previous = cv2.connectedComponentsWithStats(mask, 8)
    current = cv2.connectedComponentsWithStats(mask, connectivity=8)
    assert previous[0] == current[0]
    for before, after in zip(previous[1:], current[1:]):
        np.testing.assert_array_equal(before, after)
    # Explicit NumPy indexing must not allocate another page-sized label map.
    assert np.asarray(current[1], dtype=np.int32) is current[1]


def test_eight_way_connectivity_preserves_diagonal_ink():
    mask = np.eye(12, dtype='uint8')
    assert cv2.connectedComponentsWithStats(mask, connectivity=8)[0] == 2
    assert cv2.connectedComponentsWithStats(mask, connectivity=4)[0] == 13


def test_annotation_recovery_visits_all_pages_without_modifying_original(tmp_path):
    source_path = tmp_path / 'source.pdf'
    with fitz.open() as source:
        for index in range(3):
            page = source.new_page()
            page.insert_text((72, 72), f'Physical page {index + 1}')
            page.add_rect_annot(fitz.Rect(30, 30, 40, 40))
            invalid = page.add_rect_annot(fitz.Rect(50, 50, 60, 60))
            source.xref_set_key(invalid.xref, 'Rect', '[0 0 0 0]')
        source.save(source_path)
    original = source_path.read_bytes()
    calls = []

    def extract(path, **options):
        calls.append(options)
        with fitz.open(path) as source:
            assert source.page_count == 3
            for index in range(3):
                page = source[index]
                assert f'Physical page {index + 1}' in page.get_text()
                annotations = list(page.annots())
                assert len(annotations) == 1
                assert not annotations[0].rect.is_empty
        return [{'text': 'Selected page text'}]

    chunks, evidence = tools._pymupdf4llm_retry_without_invalid_page_annotations(
        SimpleNamespace(to_markdown=extract), str(source_path), 1, 200)
    assert chunks == [{'text': 'Selected page text'}]
    assert calls == [{'pages': [1], 'page_chunks': True, 'ocr_dpi': 200}]
    assert [row['source_page'] for row in evidence['removed_invalid_annotations']] == [1, 2, 3]
    assert source_path.read_bytes() == original
