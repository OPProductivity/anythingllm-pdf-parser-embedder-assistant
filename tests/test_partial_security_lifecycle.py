from unittest.mock import Mock

import pytest

import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


def test_ui_simulation_preview_never_resolves_credentials(monkeypatch):
    resolve = Mock(return_value={'status': 'ready', 'adapter': None})
    monkeypatch.setattr(app, 'resolve_default_simulation_adapter', resolve)
    assert app.default_simulation_resolution()['status'] == 'ready'
    resolve.assert_called_once_with(resolve_credentials=False)


def test_failed_key_cleanup_retains_only_safe_obligation(tmp_path, monkeypatch):
    delete = Mock(return_value={'status': 'delete_failed', 'error': 'sensitive peer body'})
    monkeypatch.setattr(pipeline, 'delete_temporary_desktop_api_key', delete)
    monkeypatch.setattr(pipeline, 'application_paths', lambda: {'config': tmp_path})
    monkeypatch.setattr(pipeline.time, 'sleep', lambda _: None)
    result = pipeline.cleanup_temporary_desktop_api_key('http://127.0.0.1:3001', 'owned-key-id')
    assert result['status'] == 'delete_failed'
    assert delete.call_count == 2
    evidence = (tmp_path / 'temporary-key-cleanup-obligations.jsonl').read_text()
    assert 'owned-key-id' in evidence and 'needs_review' in evidence
    assert 'sensitive peer body' not in evidence


def test_successful_key_cleanup_creates_no_diagnostic_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'delete_temporary_desktop_api_key',
                        lambda *args, **kwargs: {'status': 'deleted'})
    monkeypatch.setattr(pipeline, 'application_paths', lambda: {'config': tmp_path})
    assert pipeline.cleanup_temporary_desktop_api_key('http://127.0.0.1:3001', 'owned-key-id')['status'] == 'deleted'
    assert not list(tmp_path.iterdir())
