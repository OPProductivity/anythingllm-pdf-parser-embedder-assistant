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
