"""Verify effective settings from retained private receipts after the matrix."""

import ast
import json
from pathlib import Path
import sys
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_evidence import read_run_json


def validate():
    target = Path('tmp-output/canonical-settings-matrix-20261003/results.json')
    report = json.loads(target.read_text(encoding='utf8'))
    source = Path('auto_anythingllm_pipeline.py').read_text(encoding='utf8')
    baseline = subprocess.check_output(['git', 'show', 'HEAD:auto_anythingllm_pipeline.py']).decode('utf8')
    def functions(text):
        return {node.name: ast.dump(node, include_attributes=False)
                for node in ast.parse(text).body if isinstance(node, ast.FunctionDef)}
    current_functions, old_functions = functions(source), functions(baseline)
    for name in ('_reocr_confirmed_native_body_region', '_native_body_reocr_decision',
                 'apply_region_aware_native_layout'):
        assert current_functions[name] == old_functions[name], name
    subprocess.run(['git', 'diff', '--exit-code', '--', 'rag_pdf_tools.py'], check=True)
    report['production_ocr_unchanged'] = True
    assert len(report['cases']) == 30
    for case in report['cases']:
        assert not case['error'], case
        private = next(run for run in case['runs'] if run['private'])
        summary_path = Path(private['summary'])
        summary = read_run_json(summary_path)
        profile = read_run_json(summary_path.parent / 'source-profile.json')
        settings = case['settings']
        assert summary['segment_mode'] == settings['segment_mode']
        assert summary['chunk_overlap'] == settings.get('anythingllm_chunk_overlap', 0)
        assert summary['include_back_matter'] == settings['include_back_matter']
        assert summary['native_upload_transport'] == settings['native_upload_transport']
        if settings.get('document_author'):
            assert profile['detected_author'] == settings['document_author']
            assert profile['detected_title'] == settings['document_label']
        expected_size = settings['anythingllm_chunk_size']
        policy = profile['anythingllm_embedder_policy']
        if summary['chunk_size'] != expected_size:
            assert policy['recommended_limit'] == summary['chunk_size'], (expected_size, summary['chunk_size'], policy)
        if settings.get('first_page_override'):
            assert summary['start_page'] == settings['first_page_override']
        private['verified_effective_settings'] = {
            'chunk_size': summary['chunk_size'], 'chunk_overlap': summary['chunk_overlap'],
            'embedder_policy': policy, 'title': profile['detected_title'], 'author': profile['detected_author']}
    report['retained_settings_validation'] = 'PASS'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print('ALL_30_RETAINED_SETTINGS_PASS', flush=True)


if __name__ == '__main__':
    validate()
