"""Source selection must not silently turn local preparation into an upload."""
import pytest
import rag_pdf_gradio_app as app

pytestmark=pytest.mark.offline_deterministic

@pytest.mark.parametrize('mode',[app.MODE_LOCAL_ONLY_LABEL,app.MODE_LOCAL_WITH_LOGS_LABEL,app.MODE_NATIVE_UPLOAD_LABEL])
def test_new_selection_keeps_output_mode_but_clears_shared_document_metadata(monkeypatch,mode):
    monkeypatch.setattr(app,'LIVE_AUTOMATIC_RUN_STATUS',{})
    monkeypatch.setattr(app,'local_workspace_choices',lambda: ([],''))
    updates=app.reset_automatic_run_selection_settings('', ['first.pdf','second.pdf'],[],{'state':'pending'},mode)
    assert len(updates)==50
    assert updates[5]['value']==mode
    assert updates[0]['value']==updates[1]['value']==''

def test_explicit_reset_still_uses_saved_output_mode(monkeypatch):
    monkeypatch.setattr(app,'LIVE_AUTOMATIC_RUN_STATUS',{})
    monkeypatch.setattr(app,'local_workspace_choices',lambda: ([],''))
    expected=app.fresh_automatic_run_setting_values()['mode']
    updates=app.reset_automatic_run_selection_settings('', ['first.pdf'],[],{'state':'pending','reset_per_run_defaults':True},app.MODE_LOCAL_ONLY_LABEL)
    assert updates[5]['value']==expected

def test_active_job_is_not_changed_by_late_selection_mode(monkeypatch):
    monkeypatch.setattr(app,'LIVE_AUTOMATIC_RUN_STATUS',{'state':'running','run_root':'owned'})
    updates=app.reset_automatic_run_selection_settings('', ['next.pdf'],[],{'state':'pending'},app.MODE_LOCAL_ONLY_LABEL)
    assert all('value' not in item for item in updates)

def test_ordinary_and_folder_selection_callbacks_receive_the_current_output_mode():
    config=app.demo.config
    mode=next(c['id'] for c in config['components'] if c.get('props',{}).get('elem_id')=='output-mode-radio')
    callbacks=[d for d in config['dependencies'] if 'reset_automatic_run_selection_settings' in str(d.get('api_name'))]
    assert len(callbacks)==2
    assert all(len(d['inputs'])==5 and d['inputs'][-1]==mode for d in callbacks)
