import asyncio
import json
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize('selected,preserve,expected',[
    ('C:/chosen',False,'C:/chosen'),('',False,None),('',True,'C:/previous')])
def test_picker_timings_measure_boundaries_without_logging_before_dialog(selected,preserve,expected):
    clock=[0.0]
    logger=Mock()
    root=Mock()
    def create():
        logger.info.assert_not_called()
        clock[0]+=3
        return root
    def choose(**kwargs):
        logger.info.assert_not_called()
        assert kwargs['initialdir']=='C:/previous'
        assert kwargs['parent'] is root
        clock[0]+=10  # Includes the person's interaction, not only opening.
        return selected
    tk=SimpleNamespace(Tk=create,filedialog=SimpleNamespace(askdirectory=choose))
    with (patch.dict(sys.modules,{'tkinter':tk}),patch.object(app,'APP_LOGGER',logger),
          patch.object(app.time,'perf_counter',side_effect=lambda:clock[0]),
          patch.object(app.time,'time',side_effect=lambda:1000+clock[0])):
        trace=app._pdf_picker_timing_start({'click_epoch_ms':999000,'dispatch_epoch_ms':999100})
        clock[0]=5
        result=app.choose_pdf_input_directory('C:/previous',preserve_on_cancel=preserve,picker_timing=trace)
    assert result==expected
    root.destroy.assert_called_once()
    logger.info.assert_called_once()
    data=json.loads(logger.info.call_args.args[1])
    assert data['chain_to_picker_ms']==5000
    assert data['tk_initialize_ms']==3000
    assert data['dialog_call_ms']==10000
    assert data['click_to_dispatch_ms']==100
    assert data['browser_to_callback_ms']==900
    assert data['outcome']==('selected' if selected else 'cancelled')
    assert 'C:/previous' not in str(data) and 'C:/chosen' not in str(data)


def test_logging_failure_never_changes_selected_folder():
    tk=SimpleNamespace(Tk=Mock(return_value=Mock()),filedialog=SimpleNamespace(askdirectory=lambda **_: 'chosen'))
    with patch.dict(sys.modules,{'tkinter':tk}),patch.object(app.APP_LOGGER,'info',side_effect=OSError('disk unavailable')):
        assert app.choose_pdf_input_directory()=='chosen'


def test_tk_failure_retains_existing_cancel_contract_and_logs_stage():
    tk=SimpleNamespace(Tk=Mock(side_effect=RuntimeError('no display')),filedialog=SimpleNamespace())
    with patch.dict(sys.modules,{'tkinter':tk}),patch.object(app,'APP_LOGGER') as logger:
        assert app.choose_pdf_input_directory('previous',preserve_on_cancel=False) is None
    data=json.loads(logger.info.call_args.args[1])
    assert data['outcome']=='exception' and data['exception_type']=='RuntimeError'
    assert 'dialog_invoked_epoch_ms' not in data


@pytest.mark.parametrize('stage', ['withdraw', 'attributes', 'dialog'])
def test_picker_cleans_up_root_on_every_post_creation_failure(stage):
    root = Mock()
    choose = Mock(return_value='chosen')
    (choose if stage == 'dialog' else getattr(root, stage)).side_effect = RuntimeError(stage)
    tk = SimpleNamespace(Tk=Mock(return_value=root), filedialog=SimpleNamespace(askdirectory=choose))
    with patch.dict(sys.modules, {'tkinter': tk}):
        assert app.choose_pdf_input_directory('previous', preserve_on_cancel=False) is None
    root.destroy.assert_called_once()


def test_cleanup_failure_does_not_discard_valid_selection():
    root = Mock()
    root.destroy.side_effect = RuntimeError('cleanup')
    tk = SimpleNamespace(Tk=lambda: root, filedialog=SimpleNamespace(askdirectory=lambda **_: 'chosen'))
    with patch.dict(sys.modules, {'tkinter': tk}):
        assert app.choose_pdf_input_directory() == 'chosen'


@pytest.mark.parametrize('count', [0, 40, 376, 1000])
def test_opening_folder_does_not_resolve_previous_selection(count):
    paths = [f'C:/previous/{i}.pdf' for i in range(count)]
    with (patch.object(app, 'LIVE_AUTOMATIC_RUN_STATUS', {}),
          patch.object(app, 'local_path_identity_key', side_effect=AssertionError('unneeded filesystem access'))):
        result = app.automatic_folder_selection_begin_state({}, '', [], paths, {'pdf_candidates': paths})
    assert result[0]['state'] == 'pending'
    assert result[0]['selection_signature'] == ''
    assert result[0]['accept_next_signature'] is True


def test_actual_selection_still_computes_identity():
    with (patch.object(app, 'LIVE_AUTOMATIC_RUN_STATUS', {}),
          patch.object(app, 'automatic_selection_signature', return_value='verified') as signature):
        result = app.automatic_selection_begin_state({}, '', ['C:/a.pdf'])
    signature.assert_called_once()
    assert result[0]['selection_signature'] == 'verified'


def test_initial_settings_snapshot_does_not_become_runtime_cache():
    assert not hasattr(app, '_INITIAL_UI_SETTINGS')
    snapshot = {'chunking': {'chunk_size': 777, 'chunk_overlap': 33},
                'embedder': {'max_chunk_length': 888, 'policy': {'recommended_limit': 999}}}
    with patch.object(app, 'anythingllm_resolved_state', return_value=snapshot) as resolve:
        assert app.current_anythingllm_chunk_size_value(snapshot) == '777'
        assert app.current_anythingllm_chunk_overlap_value(snapshot) == '33'
        assert app.current_anythingllm_embedder_max_chunk_value(snapshot) == 888
        assert app.current_anythingllm_recommended_embedder_limit_value(snapshot) == 999
        resolve.assert_not_called()
        assert app.current_anythingllm_chunk_size_value() == '777'
        assert app.current_anythingllm_embedder_max_chunk_value() == 888
        assert resolve.call_count == 2


def test_browser_data_is_diagnostic_only_and_bounded():
    trace=app._pdf_picker_timing_start({'click_epoch_ms':10**999,'dispatch_epoch_ms':float('nan'),
                                      'document_age_ms':'not a number','path':'secret'})
    assert not {'click_epoch_ms','dispatch_epoch_ms','document_age_ms','path'} & trace.keys()


def test_existing_jsonl_formatter_actually_retains_timing_details(tmp_path):
    from structured_logging import configure_structured_logger
    path=tmp_path/'app.jsonl'
    logger=configure_structured_logger('picker_timing_test_'+tmp_path.name,path)
    try:
        with patch.object(app,'APP_LOGGER',logger):
            app._pdf_picker_timing_log({'id':'example','callback_epoch_ms':2000,
                                      'dispatch_epoch_ms':1500,'tk_initialize_ms':3000,
                                      'callback_monotonic':1,'outcome':'cancelled'})
        row=json.loads(path.read_text(encoding='utf8'))
        assert row['event']=='pdf_folder_picker_timing'
        data=json.loads(row['message'].removeprefix('PDF picker timing '))
        assert data['tk_initialize_ms']==3000 and data['browser_to_callback_ms']==500
        assert 'callback_monotonic' not in data
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def test_javascript_preserves_all_existing_inputs_and_does_not_wait():
    node=shutil.which('node')
    if not node:
        pytest.skip('Node is required for JavaScript behavior test')
    code='const window={ragPdfFolderClick:{epoch:Date.now()-50}};const fn='+app.PDF_FOLDER_PICKER_TIMING_JS+';'
    code+='const input=[{state:"ready"},"run",["a.pdf"],["b.pdf"],{x:1},null];const out=fn(...input);'
    code+='if(out.slice(0,5).some((v,i)=>v!==input[i]))throw Error("inputs changed");'
    code+='if(!out[5].captured_click||out[5].dispatch_epoch_ms<out[5].click_epoch_ms)throw Error("timing missing");'
    code+='if(window.ragPdfFolderClick!==null)throw Error("stale click retained"); console.log("ok");'
    result=subprocess.run([node,'-e',code],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='ok'
    assert 'await ' not in app.PDF_FOLDER_PICKER_TIMING_JS
    assert 'fetch(' not in app.PDF_FOLDER_PICKER_TIMING_JS


def test_real_gradio_chain_transports_timing_without_changing_picker_outputs():
    from gradio.state_holder import SessionState
    state=SessionState(app.demo)
    first=next(f for f in app.demo.fns.values() if f.fn is app.automatic_folder_selection_begin_state)
    second=next(f for f in app.demo.fns.values() if f.fn is app.choose_pdf_input_directory_for_scan)
    assert len(first.inputs)==6 and len(first.outputs)==7
    assert first.inputs[-1].get_block_name()=='json'
    assert first.inputs[-1].visible is False
    assert len(second.inputs)==2 and len(second.outputs)==9
    async def run():
        with patch.object(app,'LIVE_AUTOMATIC_RUN_STATUS',{}):
            await app.demo.process_api(first,inputs=[None,None,[],None,None,{'click_epoch_ms':1000,'dispatch_epoch_ms':1001}],state=state)
            with patch.object(app,'choose_pdf_input_directory',return_value=None) as choose:
                result=await app.demo.process_api(second,inputs=[None,None],state=state)
            assert choose.call_args.kwargs['picker_timing']['click_epoch_ms']==1000
            assert len(result['data'])==9
    asyncio.run(run())
