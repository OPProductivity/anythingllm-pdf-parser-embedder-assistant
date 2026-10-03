"""Check every regenerated payload against retained real-document evidence."""

import json
from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_anythingllm_pipeline as pipeline
from run_artifact_tools import evidence_path, materialize_optional_artifacts, read_rows


def run():
    receipts = Path('tmp-output/' + ('optional-parent-followup-20261003' if '--parent-followup' in sys.argv else
                                   'optional-artifact-followup-20261003'))
    report = json.loads((receipts / 'results.json').read_text(encoding='utf8'))
    old = json.loads(Path('tmp-output/canonical-kit-followup-20261003/results.json').read_text(encoding='utf8'))
    validated = []
    for case in report['cases']:
        assert not case['error'], case['error']
        private = next(row for row in case['runs'] if row['private'])
        root = Path(private['summary']).parent
        identity = root.parent.name
        rows_checked = 0
        with tempfile.TemporaryDirectory(prefix='oa-zip-') as scratch:
            with zipfile.ZipFile(Path(report['test_root']) / (identity + '.zip')) as archive:
                archive.extractall(scratch)
            moved = next(Path(scratch).rglob('artifact-locations.json')).parent
            original = (moved / 'run-summary.json').read_bytes()
            for kind in ('diagnostic-text', 'manual-kits', 'upload-alternatives'):
                assert all(path.is_file() for path in materialize_optional_artifacts(moved, kind))
            for representation in ('segments', 'page-parents'):
                for mode in ('strict', 'native_header'):
                    name = ('raw-text-payloads-' + ('page-parents-' if representation == 'page-parents' else '')
                            + mode.replace('_', '-') + '.jsonl')
                    expected = read_rows(evidence_path(moved, 'metadata-api/' + name))
                    actual = pipeline.load_upload_plan_rows(
                        moved / 'on-demand/upload-alternatives' / f'upload-plan-{representation}-{mode}.csv')
                    assert len(actual) == len(expected)
                    for row, payload in zip(actual, expected):
                        assert row['filename'] == payload['filename']
                        assert Path(row['text_file']).read_text(encoding='utf8') == payload['textContent']
                        assert all(row[key] == str(payload['metadata'].get(key) or '')
                                   for key in ('title', 'docAuthor', 'description', 'docSource', 'chunkSource'))
                        rows_checked += 1
            assert (moved / 'run-summary.json').read_bytes() == original
        previous = next((item for item in old['cases'] if item['source'] == case['source']
                         and item['settings'] == case['settings']), None)
        previous_root = Path(next(item for item in previous['runs'] if item['private'])['summary']).parent if previous else None
        before = list((previous_root / 'metadata-api').rglob('*.txt')) if previous_root else []
        after = list((root / 'metadata-api').rglob('*.txt'))
        validated.append(dict(source=case['source'], payload_rows_checked=rows_checked,
                              comparable_before=bool(previous), metadata_txt_before=len(before), metadata_txt_after=len(after),
                              metadata_txt_bytes_before=sum(path.stat().st_size for path in before),
                              metadata_txt_bytes_after=sum(path.stat().st_size for path in after)))
    report['optional_artifact_validation'] = dict(status='pass', cases=validated)
    (receipts / 'results.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf8')
    print(json.dumps(validated, indent=2))
    print('ALL_REGENERATED_PAYLOADS_PASS', sum(item['payload_rows_checked'] for item in validated))


if __name__ == '__main__':
    run()
