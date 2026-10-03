"""Audit fresh preparation against retained evidence without any API calls."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_anythingllm_pipeline as pipeline
from canonical_artifacts import checked_role_path
from manifest_text import read_manifest_rows
from portable_paths import DATA_DIRECTORY_ENVIRONMENT_VARIABLE
from run_evidence import read_run_json


def audit(results, replacement_results=None, baseline_results=None):
    report = json.loads(results.read_text(encoding='utf8'))
    if replacement_results:
        by_hash = {}
        for path in replacement_results:
            replacements = json.loads(path.read_text(encoding='utf8'))
            by_hash.update({case['sha256']: case for case in replacements['cases']})
        report['cases'] = [by_hash.get(case['sha256'], case) for case in report['cases']]
    baseline = json.loads(Path('tmp-output/historical-replay-20261003/results.json').read_text(encoding='utf8'))
    prior = {row['sha256']: row for row in baseline['cases']}
    if baseline_results:
        for path in baseline_results:
            preserved = json.loads(path.read_text(encoding='utf8'))
            prior.update({case['sha256']: case for case in preserved['cases']})
    output = {'cases': [], 'baseline': baseline['commit'], 'preparation_results': str(results)}
    if replacement_results:
        output['replacement_preparation_results'] = [str(path) for path in replacement_results]
    if baseline_results:
        output['additional_baseline_results'] = [str(path) for path in baseline_results]
    # Replay receipt roots are deliberately verbose. Retention also exercises
    # the Windows filename budget, so use short, isolated production-like homes.
    home = Path(tempfile.mkdtemp(prefix='pca-')) / 'home'
    os.environ[DATA_DIRECTORY_ENVIRONMENT_VARIABLE] = str(home)
    output['retention_test_home'] = str(home)
    for index, case in enumerate(report['cases'], 1):
        assert case['status'] == 'complete', case
        root = Path(case['run_root']) / 'document'
        summary = read_run_json(root / 'run-summary.json')
        old_root = (Path(prior[case['sha256']]['run_root']) / 'document' if case['sha256'] in prior
                    else Path(case['historical_config']).parent)
        old_summary = read_run_json(old_root / 'run-summary.json')
        before = read_manifest_rows(old_summary['manifest'])
        after = read_manifest_rows(summary['manifest'])
        assert [row['text'] for row in before] == [row['text'] for row in after], case['source']
        settings = ('start_page', 'end_page', 'selected_backend', 'segments', 'segment_mode',
                    'include_back_matter', 'native_upload_representation', 'native_upload_transport')
        assert {key: summary.get(key) for key in settings} == {key: old_summary.get(key) for key in settings}
        assert hashlib.sha256(Path(case['source']).read_bytes()).hexdigest() == case['sha256']
        deltas = {key: [old_summary.get(key), summary.get(key)] for key in
                  ('detected_title', 'detected_author', 'source_short_label')
                  if old_summary.get(key) != summary.get(key)}
        assert not deltas or Path(case['source']).name.upper() == 'PRECAR~1.PDF', deltas
        # Compare the same structural files including the body store, rather
        # than claiming unrelated timing/queue evidence has disappeared.
        def structural_bytes(directory):
            return sum(path.stat().st_size for path in directory.rglob('*.jsonl')
                       if path.name in {'segment-manifest.jsonl', 'page-parent-manifest.jsonl', 'manifest-text.jsonl'}
                       or path.name.startswith('raw-text-payloads-'))
        old_bytes = structural_bytes(old_root)
        new_bytes = structural_bytes(root)
        roles = read_run_json(root / 'artifact-locations.json')['roles']
        assert all(checked_role_path(root, actual).is_file() for actual in roles.values())
        for path in root.rglob('*.jsonl'):
            read_manifest_rows(path)
        record = {'source': case['source'], 'pages': case['pages'], 'body_identical': True,
                  'settings_identical': True, 'metadata_deltas': deltas,
                  'old_structural_bytes': old_bytes, 'new_structural_bytes': new_bytes}
        copied_run = home / 'run-state/automatic-runs' / f'r-{index:02d}'
        shutil.copytree(root.parent, copied_run)
        copy = copied_run / 'document'
        def relocate(value):
            if isinstance(value, dict):
                return {key: relocate(item) for key, item in value.items()}
            if isinstance(value, list):
                return [relocate(item) for item in value]
            return value.replace(str(root.parent), str(copied_run)) if isinstance(value, str) else value
        copied_summary = relocate(summary)
        retained = pipeline.retain_successful_run_leanly(
            copy, copied_summary, {}, Path(copied_summary['upload_file']), segments=after,
            preserve_preexisting_children=False)
        record['retention_applied'] = retained['applied']
        if retained['applied']:
            exports = pipeline.local_segment_export_sources(Path(copied_summary['upload_file']), retained)
            expected = [] if summary['segment_mode'] == 'none' else after
            assert len(exports) == len(expected)
            page_counts = Counter()
            expected_by_suffix = {}
            for row in expected:
                page = max(1, int(row.get('pdf_page') or 0))
                page_counts[page] += 1
                suffix = f'-p{page:03d}-s{page_counts[page]:02d}.txt'
                expected_by_suffix[suffix] = row['text'].replace('\r\n', '\n')
            assert {suffix: path.read_text(encoding='utf8') for path, suffix in exports} == expected_by_suffix
            record['segment_exports'] = len(exports)
            record['physical_segment_files'] = len({path for path, _ in exports})
            record['canonical_export_reuses'] = sum(path.parent != copy for path, _ in exports)
        output['cases'].append(record)
    target = results.parent / 'page-credit-audit.json'
    target.write_text(json.dumps(output, ensure_ascii=True, indent=2), encoding='utf8')
    print(json.dumps({'cases': len(output['cases']), 'pages': sum(r['pages'] for r in output['cases']),
                      'metadata_changes': sum(bool(r['metadata_deltas']) for r in output['cases']),
                      'old_structural_bytes': sum(r['old_structural_bytes'] for r in output['cases']),
                      'new_structural_bytes': sum(r['new_structural_bytes'] for r in output['cases']),
                      'audit': str(target)}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    parser.add_argument('--replacement-results', type=Path, action='append', help='Explicit fresh requalification receipt')
    parser.add_argument('--baseline-results', type=Path, action='append', help='Additional preserved baseline-worker receipt')
    options = parser.parse_args()
    audit(options.results, options.replacement_results, options.baseline_results)
