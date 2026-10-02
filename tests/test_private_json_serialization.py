import json

import pytest

import auto_anythingllm_pipeline as pipeline
import portable_paths
import rag_pdf_gradio_app as app
import run_control
import cancellable_preparation_worker as worker


pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize('writer', [pipeline.write_json, app._write_automatic_run_json,
                                  run_control.atomic_write_json, worker._write_json])
def test_only_private_json_is_compact(tmp_path, monkeypatch, writer):
    monkeypatch.setenv(portable_paths.DATA_DIRECTORY_ENVIRONMENT_VARIABLE, str(tmp_path))
    payload = {'settings': {'text': 'Unicode: \u00e9', 'pages': [1, 2]}, 'empty': {}}
    for directory, compact in [('run-state', True), ('outputs', False), ('run-state-other', False)]:
        path = tmp_path / directory / 'r-test' / 'record.json'
        writer(path, payload)
        content = path.read_text(encoding='utf8')
        assert json.loads(content) == payload
        assert ('\n' not in content) == compact
        assert not list(path.parent.glob('*.tmp'))
