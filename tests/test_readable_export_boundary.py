from pathlib import Path
from unittest.mock import patch
import pytest

import text_export_hygiene as hygiene

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize('source,expected', [
    ('institu\u00ad tions', 'institutions'),
    ('moth\u00ad erhood and roof\u00ad tops', 'motherhood and rooftops'),
    ('Po\u00adliti\u00ad cal', 'Political'),
    ('business-\u00adrelated nation–\u00adstate', 'business-related nation-state'),
    ('Harper\u00ad Collins', 'Harper Collins'),
    ('Undocu\u00ad Understanding', 'Undocu Understanding'),
    ('one\u00ad\nnext', 'one\nnext'),
    ('one\u00ad\n\nnext', 'one\n\nnext'),
    ('one\u00ad\tnext', 'one\tnext'),
    ('one\u00ad  next', 'one  next'),
    ('207–\u00ad8; self-\u00ad interest', '207-8; self- interest'),
    ('\u00adtherefore', 'therefore'),
])
def test_soft_hyphen_continuation(source, expected):
    actual, counts = hygiene.readable_export_text(source)
    assert actual == expected
    assert counts['soft_hyphen_removed'] == source.count('\u00ad')
    assert hygiene.readable_export_text(actual)[0] == actual


def test_soft_hyphen_does_not_join_reading_regions():
    pages = [{'page': 1, 'text': 'institu\u00ad tions', 'reading_regions': [
        {'text': 'institu\u00ad'}, {'text': 'tions'}]}]
    actual, _ = hygiene.prepare_readable_pages('not-opened.pdf', pages)
    assert actual[0]['text'] == 'institu\n\ntions'
    assert pages[0]['reading_regions'][0]['text'].endswith('\u00ad')


def test_soft_hyphen_inside_region_lines_only():
    source = 'institu\u00ad\ntions\n\nmoth\u00ad\n\nerhood'
    assert hygiene.readable_export_text(source)[0] == 'institu\ntions\n\nmoth\n\nerhood'
    assert hygiene.readable_export_text(source, join_region_lines=True)[0] == 'institutions\n\nmoth\n\nerhood'
    pages = [{'page': 1, 'text': source, 'reading_regions': [{'text': source}]}]
    out, evidence = hygiene.prepare_readable_pages('not-opened.pdf', pages)
    assert out[0]['text'] == 'institutions\n\nmoth\n\nerhood'
    assert evidence['counts']['soft_hyphen_region_lines_joined'] == 1


@pytest.mark.parametrize('source,expected', [
    ('con\x7frmation', 'conrmation'),
    ('WHAT\ufeffHILLBILLY', 'WHAT HILLBILLY'),
    ('https://www\u200b.example.org', 'https://www.example.org'),
    ('con\u00adfirmation', 'confirmation'),
    ('well-being\n\nNext paragraph.', 'well-being\n\nNext paragraph.'),
    ('a\u200cb\u202dc\u202c', 'abc'),
    ('a\x0cb', 'a\nb'),
    ('© ½ 10⁻³ café', '(c) 1/2 10^(-3) café'),
    ('a\ud800b', 'ab'),
])
def test_readable_policy(source,expected):
    out, _ = hygiene.readable_export_text(source)
    assert out == expected
    assert hygiene.readable_export_text(out)[0] == out


def test_healthy_pages_never_open_pdf_and_keep_inputs():
    original=[{'page':1,'text':'Ordinary prose.','reading_regions':[{'text':'A paragraph.','source_column_index':2}]}]
    with patch('fitz.open',side_effect=AssertionError('must not open')):
        output, evidence = hygiene.prepare_readable_pages(Path('missing.pdf'),original)
    assert original[0]['text']=='Ordinary prose.'
    assert output[0]['reading_regions'][0]['source_column_index']==2
    assert evidence['source_word_ocr_seconds']==0


def test_missing_source_does_not_block_readable_output():
    output, evidence = hygiene.prepare_readable_pages(Path('missing-source.pdf'),[{'page':1,'text':'con\x7frmation'}])
    assert output[0]['text']=='conrmation'
    assert evidence['counts']['unresolved_character_removed']==1


def test_cancellation_is_not_swallowed():
    def cancel(_):
        raise RuntimeError('cancel requested')
    with pytest.raises(RuntimeError,match='cancel requested'):
        hygiene.prepare_readable_pages(Path('missing.pdf'),[{'page':1,'text':'con\x7frmation'}],progress_callback=cancel)


def test_damaged_text_never_opens_pdf_or_launches_ocr(tmp_path):
    with patch('fitz.open',side_effect=AssertionError('must not open')), patch('subprocess.run',side_effect=AssertionError('must not launch')):
        output,evidence=hygiene.prepare_readable_pages(tmp_path/'absent.pdf',[{'page':1,'text':'con\x7frmation'}])
    assert output[0]['text']=='conrmation'
    assert evidence['source_word_ocr_seconds']==0
    assert evidence['policy']=='text_only_readable_v5'


def test_segment_offsets_use_cleaned_content():
    import auto_anythingllm_pipeline as pipeline
    text=('This is con\u00adfirmation and con\x7frmation in the same useful paragraph. ')*3
    meta={'source_id':'s','source_sha256':'a'*64,'source_title':'Title','source_author':'Author'}
    rows=pipeline.make_segments(Path('synthetic.pdf'),'pymupdf',[{'page':1,'text':text}],1,None,meta,80,segment_mode='page')
    assert rows
    for row in rows:
        assert '\u00ad' not in row['text'] and '\x7f' not in row['text']
        assert row['char_end_page']-row['char_start_page']==len(row['text'])


def test_repeated_damaged_words_do_not_invent_a_global_mapping(tmp_path):
    original=[{'page':1,'text':'con\x7frmation con\x7frmation'}]
    output,evidence=hygiene.prepare_readable_pages(tmp_path/'absent.pdf',original)
    assert output[0]['text']=='conrmation conrmation'
    assert original[0]['text']=='con\x7frmation con\x7frmation'
    assert evidence['counts'].get('recovered_words',0)==0


def test_serialized_segment_text_matches_clean_content():
    import json
    import auto_anythingllm_pipeline as pipeline
    raw=('A useful con\u00adfirmation from the author. ')*4
    meta={'source_id':'s','source_sha256':'b'*64,'source_title':'Title','source_author':'Author'}
    rows=pipeline.make_segments(Path('synthetic.pdf'),'pymupdf',[{'page':1,'text':raw}],1,None,meta,80,segment_mode='none')
    assert len(rows)==1
    decoded=json.loads(json.dumps(rows[0],ensure_ascii=False))
    assert decoded['text']==rows[0]['text']
    assert 'confirmation' in decoded['text']
    assert '\u00ad' not in decoded['text']
