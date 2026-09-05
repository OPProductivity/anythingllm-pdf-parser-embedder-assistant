from pathlib import Path
from unittest.mock import patch
import pytest
import auto_anythingllm_pipeline as a

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize('name,text,later,expected', [
    ('Jameson-Postmodernism.pdf', 'Fredric Jameson\nPostmodernism\nOrdinary body discussion continues.', 'A cited volume\nedited by Hal Foster,', 'Fredric Jameson'),
    ('West - Pluto.pdf', 'NAME OF WORKER\nDorothy West\nReported by Dorothy West (Staff Writer)', 'immortalized\nby Mr. Walt Disney.', 'Dorothy West'),
    ('Marx_German_Ideology.pdf', 'The German Ideology\nKarl Marx\nOrdinary body text continues.', 'second edition, trans. Robert C. Tucker, pp. 110–164. W.W. Norton, 1978.', 'Karl Marx'),
    ('Lowe - The Woman in the Window.pdf', '\u202dRamona Lowe, “The Woman in the Window,” in Writing Red:\u202c\nWomen Writers, 1930-1940, edited by Charlotte Nekola and Paula Rabinowitz', '', 'Ramona Lowe'),
])
def test_opening_credit_outranks_citation(name,text,later,expected):
    p=Path(name)
    r=a.infer_author_from_samples_or_filename([{'page':1,'text':text},{'page':2,'text':later}],p,p.stem)
    assert r['author']==expected


def test_opening_corroboration_does_not_choose_between_two_people():
    assert not a.opening_filename_corroborated_credit(
        [{'page':1,'text':'John Smith\nJane Smith'}],Path('Smith - Article.pdf'))
    assert not a.opening_filename_corroborated_credit(
        [{'page':1,'text':'Some text mentions John Smith in passing.'}],Path('Smith - Article.pdf'))


def test_existing_explicit_multiple_author_credit_kept():
    r=a.infer_author_from_samples_or_filename(
        [{'page':1,'text':'By John Smith and Jane Doe\nArticle title\nAbstract\nA discussion.'}],Path('Smith - Article.pdf'))
    assert 'Smith' in r['author'] and 'Doe' in r['author']


@pytest.mark.parametrize('value',['trans. Robert C. Tucker','pp. W.W. Norton','Women Writers, 1930-1940, edited by Charlotte Nekola'])
def test_citation_furniture_rejected_before_splitting(value):
    assert not a.split_author_line_candidates(value)


def test_scan_noise_includes_isolated_debris_not_printed_abbreviations():
    assert a._layout_text_noise_score('U.S.\nU.K.\nN.Y.\nOrdinary printed text.')==0
    assert a._layout_text_noise_score('S=\ne\'\n#\nOrdinary printed text.')>=3
    assert a._layout_embedded_symbol_noise_score('S=\ne\'\n#\nOrdinary printed text.')==0
    assert a._layout_embedded_symbol_noise_score('word#!fragment')>0


def test_rejected_and_accepted_scan_ocr_retain_same_decision_dimensions():
    prose='The U.S. government supported an economic recovery. '*30
    for raw,candidate,reason in [(prose,prose,'no_native_noise_evidence'),
                                 ('S=\n#\n'+prose,prose,'scan_body_noise_reduced_with_content_coverage'),
                                 ('S=\n'+prose,'short','candidate_too_short_or_unavailable')]:
        d=a._native_body_reocr_decision(raw,candidate)
        assert d['reason']==reason
        assert {'raw_noise_score','reocr_noise_score','alpha_coverage','selected','attempted'} <= d.keys()


@pytest.mark.parametrize('noise,mode',[('S=\n#\n',6),('word#!fragment\n',4)])
def test_single_body_mode_does_not_replace_existing_mixed_layout_mode(tmp_path,noise,mode):
    pdf=tmp_path/'scan.pdf'
    with a.fitz.open() as document:
        document.new_page().insert_text((70,80),'A document body.')
        document.save(pdf)
    prose='The U.S. government supported an economic recovery. '*30
    with (patch.object(a,'_layout_has_scan_background',return_value=True),
          patch.object(a,'_layout_marginal_annotation_plan',return_value={
              'applied':True,'body_bounds':[60,500]}),
          patch.object(a,'_reocr_confirmed_native_body_region',return_value=prose) as call):
        _,evidence=a.apply_region_aware_native_layout(pdf,[{'page':1,'text':noise+prose}])
    assert call.call_args.kwargs['segmentation_mode']==mode
    decision=evidence['pages'][0]['outer_margin_annotation']['body_reocr']
    assert decision['selected']
    assert decision['segmentation'].endswith(f'psm{mode}')


def test_sparse_illustrated_opening_not_claimed_fully_recovered():
    native=[{'page':i,'text':''} for i in range(1,5)]
    ocr=[{'page':1,'text':'PART TWO'},*({'page':i,'text':'ordinary scholarly discussion '*200} for i in [2,3,4])]
    pages,report=a.reconcile_native_ocr_pages(native,ocr)
    assert pages[0]['text']=='PART TWO'
    assert pages[0]['ocr_page_quality']['state']=='front_matter_sparse_text'
    assert pages[0]['ocr_page_quality']['diagnostic_only']


def test_progress_remap_preserves_short_runs_and_final_proof():
    import rag_pdf_gradio_app as app
    end=app.AUTOMATIC_UPLOAD_PHASE_RANGES['identity_set'][1]
    for value in [.16,.25,.5,.8,end]:
        assert app.ingestion_progress_after_preparation(value,.1)==value
    assert app.ingestion_progress_after_preparation(end,.49)==end
    values=[app.ingestion_progress_after_preparation(x/100,.49) for x in range(15,95)]
    assert values==sorted(values)
    assert values[0]>=.49


def test_display_lift_never_changes_eta_inputs_and_does_not_tick_when_idle():
    import rag_pdf_gradio_app as app
    with patch.object(app,'LIVE_AUTOMATIC_RUN_STATUS',{}):
        record=app.update_live_automatic_run_status(state='running',phase='Preparing',
            confirmed_fraction=.13,presentation_progress_fraction=.45,expected_seconds=1047,
            progress_phase='worker_lifecycle')
        assert record['confirmed_fraction']==.13
        assert record['expected_seconds']==1047
        assert app.raw_paced_progress_fraction(record,now=record['updated_epoch'])>=.45
        # The new field is a fixed observed checkpoint, not elapsed-time fill.
        record['phase_allowance']=0
        assert app.raw_paced_progress_fraction(record,now=record['updated_epoch']+1000)==.45
        restarted=app.update_live_automatic_run_status(state='running',phase='Restart',
            confirmed_fraction=0,reset_progress=True)
        assert restarted['presentation_progress_fraction']==0


def test_worker_lifecycle_is_actually_wired_to_the_display_only_path():
    import inspect
    import rag_pdf_gradio_app as app
    text=inspect.getsource(app)
    assert 'phase_name == "worker_lifecycle" and not grouped_upload_progress_active' in text
    assert 'presentation_progress_fraction = prepared_display' in text
    assert 'presentation_progress_fraction=grouped_display_fraction' in text


@pytest.mark.parametrize('sources',[1,4,36,376,1000])
def test_preparation_then_ingestion_keeps_reporting_reservation(sources):
    import rag_pdf_gradio_app as app
    origin=app.AUTOMATIC_UPLOAD_PHASE_RANGES['payloads'][1]
    values=[]
    for i in range(101):
        fraction=i/100
        values.append(app.concurrent_preparation_progress_fraction(
            origin*fraction,600*fraction,1500,fraction,
            presentation_expected_seconds=1000))
    assert values==sorted(values)
    assert values[-1]==.5
    frozen=values[-1]
    for i in range(101):
        canonical=origin+(.94-origin)*i/100
        shown=app.ingestion_progress_after_preparation(canonical,frozen)
        assert frozen<=shown<=.95
    # No first-source exception can spend a large batch's whole local lane.
    first=app.concurrent_preparation_progress_fraction(
        origin/sources,600,1500,1/sources,presentation_expected_seconds=1000)
    assert first<=.5/sources


def test_display_lift_respects_cancellation_and_terminal_evidence():
    import rag_pdf_gradio_app as app
    r={'state':'running','confirmed_fraction':.13,
       'presentation_progress_fraction':.5,'display_anchor_fraction':.42,
       'cancel_requested':True}
    assert app.paced_progress_fraction(r,now=10000)==.42
    assert app.paced_progress_fraction(dict(r,state='warning'),now=10000)==.13
    assert app.paced_progress_fraction(dict(r,state='successful'),now=10000)==1


def test_last_run_worker_checkpoints_no_longer_bypass_local_progress():
    import rag_pdf_gradio_app as app
    # Retained 36-PDF run checkpoints; conservative completed-source share,
    # ignoring any progress inside the currently active PDF.
    for elapsed,canonical,done,lower in [(116.286,.03722,9,.11),
                                        (251.161,.08747,23,.23),
                                        (401.061,.11153,29,.38),
                                        (490.739,.12249,32,.44)]:
        display=app.concurrent_preparation_progress_fraction(
            canonical,elapsed,1495,done/36,presentation_expected_seconds=1047)
        assert display>=lower
        assert display<=.5
    assert .53<app.ingestion_progress_after_preparation(.24036,.49)<.57
