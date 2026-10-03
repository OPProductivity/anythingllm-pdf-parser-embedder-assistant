"""Audit exact confirmation evidence and isolated cleanup without production writes."""

import json
import argparse
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_evidence import read_run_json


def exact_source_identity(case, outcome):
    expected = case.get('sources') or [case.get('source')]
    actual = outcome.get('document_results')
    return (bool(expected) and all(isinstance(source, str) and source for source in expected)
            and len(expected) == len(set(expected)) and isinstance(actual, dict)
            and set(actual) == set(expected))


def journal_events(path):
    ledger = read_run_json(path)
    journal = path.with_suffix('.events.jsonl')
    rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    metadata = ledger['runtime_event_journal']
    sequence = [row for row in rows if row['sequence'] == metadata['sequence']]
    if (not ledger['runtime_event_history_complete']
            or len(rows) != metadata['retained_events']
            or len(sequence) != ledger['runtime_event_count']
            or [row['position'] for row in sequence] != list(range(len(sequence)))):
        raise ValueError(f'Incomplete runtime event journal: {path}')
    return [row['event'] for row in sequence]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-qualified', help='Fail unless this specific instance passes every check')
    options = parser.parse_args()
    root = Path('tmp-output/historical-replay-20261003')
    rows = []
    for base in sorted(root.glob('isolated-*/')):
        path = base / 'report.json'
        if not path.exists():
            continue
        report = json.loads(path.read_text())
        verified, errors, cache, selected, events, physical = 0, [], 0, 0, 0, 0
        source_checks, history_checks, history_errors = [], [], []
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
            source_checks.append(exact_source_identity(case, outcome))
            selected += outcome['selected_records']
            cache += outcome['prepared_record_cache_reused_count']
            verified += outcome['vector_confirmed_records']
            errors.extend(outcome['errors'])
            for value in outcome['document_results'].values():
                if value.get('status') != 'complete' or value.get('error'):
                    errors.append(value.get('error') or 'Source-local outcome was not complete')
                if (value.get('post_status') != 'pass' or not value.get('searchability_proven')
                        or value.get('records') != value.get('vector_confirmed_records')):
                    errors.append('Source-local vector confirmation was incomplete')
            ledger = read_run_json(artifact.parent / 'batch-embedding-ledger.json')
            physical += sum(batch.get('verification', {}).get('current_upload_document_vector_count') or 0
                            for batch in ledger['batches'])
            try:
                observed = journal_events(artifact.parent / 'batch-embedding-ledger.json')
                events += len(observed)
                if 'embedding_update' in outcome and observed != outcome['embedding_update']['runtime_events']:
                    raise ValueError('Top-level journal does not match the returned observations')
                groups = sorted((artifact.parent / 'queue-groups').glob('*embedding-ledger.json'))
                if groups and [event for group in groups for event in journal_events(group)] != observed:
                    raise ValueError('Queue-group journals do not match the complete aggregate history')
                history_checks.append(True)
            except (OSError, KeyError, ValueError) as exc:
                history_checks.append(False)
                history_errors.append(str(exc))
        row = {'instance': base.name, 'completed_sources': sum(len(case.get('sources') or [case.get('source')])
                                                             for case in report['cases']),
               'selected_records': selected, 'exact_vector_confirmed_records': verified,
               'physical_vectors_in_scoped_verification': physical,
               'cached_records': cache, 'errors': errors, 'retained_queue_control_events': events,
               'cleanup_errors': report.get('cleanup_errors'),
               'test_processes_stopped': report.get('owned_test_processes_stopped', False)}
        row['source_identity_complete'] = bool(source_checks) and all(source_checks)
        row['queue_history_complete'] = bool(history_checks) and all(history_checks)
        row['history_errors'] = history_errors
        if row['test_processes_stopped']:
            storage = Path(report['isolation']['storage']).resolve()
            assert storage.is_relative_to(base.resolve())
            with sqlite3.connect((storage / 'anythingllm.db').as_uri() + '?mode=ro', uri=True) as db:
                row['remaining_rows'] = {table: db.execute('select count(*) from ' + table).fetchone()[0]
                                         for table in ('workspaces', 'workspace_documents', 'document_vectors', 'api_keys')}
            row['remaining_cache_files'] = sum(p.is_file() for p in (storage / 'vector-cache').rglob('*'))
            row['remaining_vector_namespaces'] = [p.name for p in (storage / 'lancedb').glob('*.lance')]
            assert not any(row['remaining_rows'].values())
            assert not row['remaining_cache_files'] and not row['remaining_vector_namespaces']
        row['qualification_passed'] = (row['source_identity_complete'] and row['queue_history_complete']
                                       and selected > 0 and selected == verified and not cache and not errors
                                       and row['test_processes_stopped'] and not row['cleanup_errors'])
        rows.append(row)
    output = root / ('live-audit-' + uuid.uuid4().hex + '.json')
    output.write_text(json.dumps(rows, indent=2), encoding='utf8')
    print(json.dumps(rows))
    print('Audit receipt:', output)
    if options.require_qualified:
        return int(not any(row['instance'] == options.require_qualified and row['qualification_passed']
                           for row in rows))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
