from pathlib import Path
from unittest.mock import patch
import pytest
import auto_anythingllm_pipeline as a
import rag_pdf_tools as t

pytestmark = pytest.mark.offline_deterministic

def test_translator_removed_even_after_title_block_fallback():
    samples=[{'page':1,'text':'The Ontology of the Photographic Image\nAndré Bazin; Hugh Gray\nFilm Quarterly, Vol. 13, No. 4.'},
             {'page':2,'text':'The Ontology of the Photographic Image\nTRANSLATED BY HUGH GRAY\nOrdinary prose.'}]
    result=a.infer_author_from_samples_or_filename(samples,Path('The_Ontology.pdf'),title_hint='The Ontology of the Photographic Image')
    assert result['author']=='André Bazin'

def test_editor_title_page_outranks_endorsement_affiliations():
    title='European Cinema in the Twenty-First Century'
    samples=[{'page':1,'text':title},
             {'page':2,'text':'“A useful book.”\n—Pat Brereton, Professor, University\n“Good work.”\n—Maria Flood, Lecturer, University'},
             {'page':3,'text':'Ingrid Lewis • Laura Canning\nEditors\n'+title}]
    assert a.infer_author_from_text_samples(samples,title)['author']=='Ingrid Lewis, Laura Canning'

def test_series_editor_not_promoted():
    result=a.infer_author_from_text_samples([{'page':1,'text':'John Smith\nSeries Editors\nA Different Book About Films'}], 'A Different Book About Films')
    assert result.get('source')!='text_edited_by'

@pytest.mark.parametrize('repeat,expected',[(True,'ROGER ODIN'),(False,'')])
def test_selected_ocr_running_head_requires_independent_page(repeat,expected):
    pages=[{'page':1,'text':'A SEMIO-PRAGMATIC APPROACH TO\nTHE DOCUMENTARY FILM\nROGER ODIN\n'+'Ordinary prose. '*50}]
    if repeat:
        pages.append({'page':2,'text':'228 ROGER ODIN\n'+'Body words. '*50})
    assert a.recover_author_from_selected_extraction(pages)['author']==expected

def test_sparse_spread_gutter_requires_both_windows_and_blank_full_gutter():
    from PIL import Image,ImageStat,ImageDraw
    im=Image.new('RGB',(1200,800),'white')
    with patch.object(t,'photographed_fold_gutter_fraction',side_effect=[.48,.48]):
        assert t.photographed_sparse_spread_gutter(im,ImageStat)==.48
    ImageDraw.Draw(im).rectangle((550,430,620,470),fill='black')
    with patch.object(t,'photographed_fold_gutter_fraction',side_effect=[.48,.48]):
        assert t.photographed_sparse_spread_gutter(im,ImageStat) is None
    with patch.object(t,'photographed_fold_gutter_fraction',side_effect=[.48,None]):
        assert t.photographed_sparse_spread_gutter(im,ImageStat) is None

@pytest.mark.parametrize('line_height,expected',[(12,180),(24,144)])
def test_spread_resolution_only_changes_small_type(line_height,expected):
    from PIL import Image,ImageDraw,ImageOps
    im=Image.new('RGB',(1200,800),'white')
    draw=ImageDraw.Draw(im)
    for y in range(170,640,42):
        draw.rectangle((100,y,480,y+line_height-1),fill='black')
        draw.rectangle((700,y,1050,y+line_height-1),fill='black')
    specs=t.photographed_spread_crop_specs(1200,800,.5)
    assert t.photographed_spread_render_dpi(im,specs,ImageOps)==expected
    assert t.photographed_spread_render_dpi(im,[],ImageOps)==144

def _features():
    import rag_pdf_gradio_app as g
    return dict(mode=g.MODE_NATIVE_UPLOAD_LABEL,embedding_submission_strategy='desktop_queue',
                document_count=17,estimated_records=900,actual_records=900,
                estimated_embedding_provider_requests=900,page_count=900,embedding_provider_request_seconds_prior=1.5,
                native_upload_scope=g.NATIVE_UPLOAD_SCOPE_ALL_LABEL if hasattr(g,'NATIVE_UPLOAD_SCOPE_ALL_LABEL') else 'All segments',
                embedding_timing_lane='cloud:openrouter',native_upload_transport='file_upload',
                native_upload_representation='page_parents',segment_mode='Page - preserve automatically')


def test_spread_resolution_preserves_illustrated_pages():
    from PIL import Image,ImageDraw,ImageOps
    im=Image.new('RGB',(1200,800),'white')
    draw=ImageDraw.Draw(im)
    for y in range(170,640,42):
        draw.rectangle((700,y,1050,y+11),fill='black')
    draw.rectangle((100,170,480,450),fill='black')
    assert t.photographed_spread_render_dpi(
        im,t.photographed_spread_crop_specs(1200,800,.5),ImageOps)==144

def test_opening_queue_prior_uses_staging_not_instantaneous_rate():
    import rag_pdf_gradio_app as g
    f=_features()
    rows=[dict(f,run_key=str(i),batch_measurements=[{'records':900,'elapsed_seconds':450,'searchability_proven':True}]) for i in range(2)]
    with patch.object(g,'timing_model_observation_usable',return_value=True),patch.object(g,'timing_model_cached_attachment_reuse_count',return_value=0):
        p=g.completed_queue_opening_prior(f,rows)
        assert p['seconds_per_record']==.625
        assert not g.completed_queue_opening_prior(f,rows[:1])
        assert not g.completed_queue_opening_prior(dict(f,document_count=2),rows)
        bad=[dict(r,batch_measurements=[{'records':899,'elapsed_seconds':450,'searchability_proven':True}]) for r in rows]
        assert not g.completed_queue_opening_prior(f,bad)
    changed=dict(f,opening_complete_queue_prior=p)
    assert g.timing_model_base_seconds(changed)<g.timing_model_base_seconds(f)
    assert changed['embedding_provider_request_seconds_prior']==f['embedding_provider_request_seconds_prior']

def test_cache_and_unproven_queue_cannot_teach_opening_prior():
    import rag_pdf_gradio_app as g
    f=_features()
    rows=[dict(f,run_key=str(i),batch_measurements=[{'records':900,'elapsed_seconds':200,'searchability_proven':False}]) for i in range(2)]
    with patch.object(g,'timing_model_observation_usable',return_value=True):
        assert not g.completed_queue_opening_prior(f,rows)
        with patch.object(g,'timing_model_cached_attachment_reuse_count',return_value=900):
            assert not g.completed_queue_opening_prior(f,rows)

def test_capacity_terminal_uses_checked_values_not_old_aliases(tmp_path):
    import rag_pdf_gradio_app as g
    source=tmp_path/'source.pdf'
    source.write_bytes(b'pdf')
    capacity=g.automatic_batch_output_capacity_preflight([source],tmp_path)
    result=g.terminal_output_capacity_evidence(capacity)
    assert result['projected_bytes']==capacity['projected_artifact_bytes']>0
    assert result['available_bytes']==capacity['available_free_bytes']>0
    assert g.terminal_output_capacity_evidence(None)==dict(status='not_recorded',projected_bytes=None,available_bytes=None)

def test_opening_prior_survives_history_compaction():
    import rag_pdf_gradio_app as g
    p={'seconds_per_record':.35,'matching_runs':2,'fresh_records':1800,
       'basis':'complete_proven_queue_durations_including_staging'}
    f=dict(_features(),opening_complete_queue_prior=p)
    expanded=g.expand_timing_run_history_row(g.compact_timing_run_history_row(
        dict(f,initial_estimate_features=f)))
    assert expanded['opening_complete_queue_prior']==p
    assert expanded['initial_estimate_features']['opening_complete_queue_prior']==p
