"""Audit exact confirmation evidence and isolated cleanup without production writes."""

import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_evidence import read_run_json


def main():
    root = Path('tmp-output/historical-replay-20261003')
    rows = []
    for base in sorted(root.glob('isolated-*/')):
        path = base / 'report.json'
        if not path.exists():
            continue
        report = json.loads(path.read_text())
        verified, errors, cache, selected, events, physical = 0, [], 0, 0, 0, 0
        for case in report['cases']:
            artifact = Path(case['report'])
            if not artifact.exists():
                ledger = read_run_json(artifact.parent / 'batch-embedding-ledger.json')
                outcome = {'selected_records': ledger['requested'],
                           'prepared_record_cache_reused_count': case['cache_reused'],
                           'vector_confirmed_records': sum(batch['requested'] for batch in ledger['batches']
                                                           if batch.get('searchability_proven')),
                           'errors': case['errors'], 'document_results': {}}
            else:
                outcome = read_run_json(artifact)
            selected += outcome['selected_records']
            cache += outcome['prepared_record_cache_reused_count']
            verified += outcome['vector_confirmed_records']
            errors.extend(outcome['errors'])
            for value in outcome['document_results'].values():
                if value.get('status') != 'complete' or value.get('error'):
                    errors.append(value.get('error') or 'Source-local outcome was not complete')
                assert value['post_status'] == 'pass' and value['searchability_proven']
                assert value['records'] == value['vector_confirmed_records']
            ledger = read_run_json(artifact.parent / 'batch-embedding-ledger.json')
            physical += sum(batch.get('verification', {}).get('current_upload_document_vector_count') or 0
                            for batch in ledger['batches'])
            if ledger['runtime_event_count']:
                journal = [json.loads(line) for line in Path(
                    ledger['runtime_event_journal']['path']).read_text().splitlines()]
                assert ledger['runtime_event_history_complete']
                assert len(journal) == ledger['runtime_event_count']
                events += len(journal)
        row = {'instance': base.name, 'completed_sources': sum(len(case.get('sources') or [case.get('source')])
                                                             for case in report['cases']),
               'selected_records': selected, 'exact_vector_confirmed_records': verified,
               'physical_vectors_in_scoped_verification': physical,
               'cached_records': cache, 'errors': errors, 'retained_queue_control_events': events,
               'cleanup_errors': report.get('cleanup_errors'),
               'test_processes_stopped': report.get('owned_test_processes_stopped', False)}
        row['source_identity_complete'] = all(
            bool(source) for case in report['cases'] for source in (
                read_run_json(Path(case['report']))['document_results']
                if Path(case['report']).exists() else []))
        if row['test_processes_stopped']:
            storage = Path(report['isolation']['storage']).resolve()
            assert storage.is_relative_to(base.resolve())
            with sqlite3.connect(storage / 'anythingllm.db') as db:
                # This disposable capability belonged only to the stopped test
                # instance; never retain it with diagnostic evidence.
                db.execute('delete from api_keys')
                row['remaining_rows'] = {table: db.execute('select count(*) from ' + table).fetchone()[0]
                                         for table in ('workspaces', 'workspace_documents', 'document_vectors', 'api_keys')}
            row['remaining_cache_files'] = sum(p.is_file() for p in (storage / 'vector-cache').rglob('*'))
            row['remaining_vector_namespaces'] = [p.name for p in (storage / 'lancedb').glob('*.lance')]
            assert not any(row['remaining_rows'].values())
            assert not row['remaining_cache_files'] and not row['remaining_vector_namespaces']
        rows.append(row)
    (root / 'live-audit.json').write_text(json.dumps(rows, indent=2), encoding='utf8')
    print(json.dumps(rows))


if __name__ == '__main__':
    main()
