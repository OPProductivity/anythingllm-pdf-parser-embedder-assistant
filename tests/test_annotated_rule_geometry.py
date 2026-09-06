"""Annotation opt-in must be backed by text, not merely decorative rules."""
from PIL import Image, ImageDraw
import pytest
import rag_pdf_tools as t

pytestmark = pytest.mark.offline_deterministic


def prose():
    image = Image.new('L', (850, 950), 255)
    draw = ImageDraw.Draw(image)
    for y in range(50, 400, 15):
        for x in range(50, 750, 15):
            draw.rectangle((x, y, x + 4, y + 7), fill=0)
    return image, draw


def test_repeated_author_rules_use_installed_model_not_annotation_model():
    image, draw = prose()
    for y in (450, 550, 650):
        draw.line((100, y, 170, y), fill=0)
    psm, args, evidence = t._resolve_ocr_recognition(image, 4)
    assert (psm, args) == (6, [])
    assert evidence['geometry_reason'] == 'aligned_short_printed_rules'
    assert evidence['model'] == 'installed_eng'
    assert evidence['underlined_prose_detected'] is False
    assert evidence['text_supported_rule_count'] == 0


def test_unaligned_detached_rules_preserve_original_layout():
    image, draw = prose()
    for x, y in ((100, 450), (300, 550), (500, 650)):
        draw.line((x, y, x + 70, y), fill=0)
    evidence = {}
    assert t.annotated_text_block_psm(image, decision=evidence) == 4
    assert evidence['geometry_reason'] == 'insufficient_text_supported_underlines'


def test_paired_heading_rules_are_not_called_handwritten_annotations():
    from unittest.mock import patch
    image, draw = prose()
    for top, bottom in ((450, 485), (600, 635)):
        draw.line((200, top, 500, top), fill=0)
        draw.line((200, bottom, 500, bottom), fill=0)
        for x in range(210, 480, 15):
            draw.rectangle((x, bottom - 12, x + 4, bottom - 5), fill=0)
    with patch.object(t, 'annotated_model_arguments', return_value=['--oem', '1']):
        psm, args, evidence = t._resolve_ocr_recognition(image, 4)
    assert psm == 6 and args
    assert evidence['geometry_reason'] == 'paired_printed_heading_rules'
    assert evidence['underlined_prose_detected'] is False


def test_heading_route_does_not_enlarge_image():
    from unittest.mock import patch
    from types import SimpleNamespace
    from PIL import ImageOps
    image, _ = prose()
    evidence = {}
    with patch.object(t, '_resolve_ocr_recognition', return_value=(6, ['--oem','1'], {
        'requested_psm':4, 'geometry_reason':'paired_printed_heading_rules'})), \
        patch.object(Image.Image, 'resize', side_effect=AssertionError('must not enlarge')), \
        patch.object(t.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='Text')) as call:
        assert t._ocr_photographed_crop(image, (0,0,1,1), 'tesseract', ImageOps,
            enhance_annotated_prose=True, recognition_evidence=evidence) == 'Text'
    assert call.call_count == 1
    assert evidence['recognition_raster_scale'] == 1


def test_supported_short_annotation_keeps_existing_recognition():
    image, draw = prose()
    for x in (100, 250, 400):
        draw.line((x, 105, x + 70, 105), fill=0)
    evidence = {}
    assert t.annotated_text_block_psm(image, decision=evidence) == 6
    assert evidence['text_supported_rule_count'] == 3
    assert evidence['underlined_baseline_count'] == 1


def test_staff_with_short_interleaved_fragments_does_not_opt_in():
    image, draw = prose()
    for y in (600, 606, 612, 618, 624):
        draw.line((100, y, 650, y), fill=0)
    for x, y in ((350,603), (100,609), (300,615), (500,621)):
        draw.line((x, y, x + 55, y), fill=0)
    evidence = {}
    assert t.annotated_text_block_psm(image, decision=evidence) == 4
    assert evidence['geometry_reason'] == 'staff_like_parallel_rules'


def test_staff_line_interrupted_by_notes_keeps_layout():
    from unittest.mock import patch
    image, draw = prose()
    for y, x in ((600,100),(606,100),(612,100),(618,180),(624,100)):
        draw.line((x, y, 650, y), fill=0)
    for x, y in ((350,603), (100,609), (300,615), (500,621)):
        draw.line((x, y, x + 55, y), fill=0)
    evidence = {}
    assert t.annotated_text_block_psm(image, decision=evidence) == 4
    assert evidence['geometry_reason'] == 'staff_like_parallel_rules'
    assert evidence['staff_fragment_recovery'] is True
    with patch.object(t, 'annotated_model_arguments', return_value=['--oem', '1']):
        psm, args, decision = t._resolve_ocr_recognition(image, 4)
    assert psm == 4 and args
    assert decision['underlined_prose_detected'] is False
    with patch.object(t, 'annotated_model_arguments', return_value=[]):
        assert t._resolve_ocr_recognition(image, 4)[:2] == (4, [])


def test_real_multiline_underlines_still_opt_in_without_mutation():
    image, draw = prose()
    for y in (105, 210, 315):
        draw.line((100, y, 300, y), fill=0)
    original = image.tobytes()
    evidence = {}
    assert t.annotated_text_block_psm(image, decision=evidence) == 6
    assert evidence['underlined_baseline_count'] == 3
    assert image.tobytes() == original


def test_new_geometry_evidence_survives_page_ledger():
    import auto_anythingllm_pipeline as pipeline
    profile = {'psm': 4, 'underlined_baseline_count': 1,
               'route_reason': 'insufficient_text_supported_underlines'}
    ledger = pipeline.ocr_page_evidence_ledger(
        [{'page': 1, 'text': 'Source text', 'reading_regions': [{'recognition_layout': profile}]}],
        {'status': 'not_available'}, [1])
    assert ledger['pages'][0]['recognition_layout'] == [profile]


def test_old_recognition_checkpoint_is_not_reused_after_upgrade(tmp_path):
    from unittest.mock import patch
    import fitz
    pdf = tmp_path / 'source.pdf'
    with fitz.open() as document:
        document.new_page()
        document.save(pdf)
    runtime = {'backend_module_origin':'fixture', 'tesseract_executable':''}
    checkpoint = tmp_path / 'run' / '.ocr-page-checkpoints'
    with patch.object(t, 'UNSTRUCTURED_OCR_CHECKPOINT_SCHEMA_VERSION', 1):
        t.save_unstructured_ocr_checkpoint(pdf, 'hi_res', runtime, checkpoint,
            [{'page':1, 'text':'Old recognition'}], 1, [])
        assert t.load_unstructured_ocr_checkpoint(pdf, 'hi_res', runtime, checkpoint) is not None
    assert t.load_unstructured_ocr_checkpoint(pdf, 'hi_res', runtime, checkpoint) is None
    t.save_unstructured_ocr_checkpoint(pdf, 'hi_res', runtime, checkpoint,
        [{'page':1, 'text':'Current recognition'}], 1, [])
    current = t.load_unstructured_ocr_checkpoint(pdf, 'hi_res', runtime, checkpoint)
    assert current[0][0]['text'] == 'Current recognition'
