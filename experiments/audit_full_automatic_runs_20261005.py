"""Read-only evidence and sampled text audit of retained full automatic PDF runs.

Only --output-prefix JSON/TXT receipts are written. No pipeline, API, database,
embedding, cleanup or OCR operations are invoked.
"""

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import random
import re
import sys
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_evidence import read_run_json
from run_artifact_tools import evidence_path
from manifest_text import read_manifest_rows


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def normalized(text):
    return ' '.join(unicodedata.normalize('NFKC', text).split())


def tokens(text):
    return re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold())


def load(path, errors, required=False):
    if not path.is_file():
        if required:
            errors.append(f'Missing retained evidence: {path}')
        return {}
    try:
        return read_run_json(path)
    except (OSError, ValueError, RuntimeError) as exc:
        errors.append(f'{path}: {type(exc).__name__}: {exc}')
        return {}


def role(root, summary, name, field=None):
    try:
        return evidence_path(root, name)
    except (OSError, ValueError):
        value = summary.get(field or '') or summary.get('artifact_paths', {}).get(name)
        candidate = Path(value) if value else root / name
        return candidate if candidate.is_file() else None


def references(root, documents):
    result = []
    for document, data in documents:
        def visit(value, trail=''):
            if isinstance(value, dict):
                for key, child in value.items():
                    visit(child, f'{trail}.{key}'.strip('.'))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f'{trail}[{index}]')
            elif isinstance(value, str) and value and (Path(value).is_absolute() or re.match(r'^[A-Za-z]:[\\/]', value)):
                path = Path(value)
                result.append({'evidence_file': document, 'field': trail, 'path': value,
                               'exists': path.exists(), 'is_file': path.is_file(),
                               'within_run': path.resolve().is_relative_to(root.resolve())})
        visit(data)
    return result


def sample_pages(source, summary, rows, export, count, seed):
    grouped = defaultdict(list)
    multi_page_rows = []
    for row in rows:
        page = int(row.get('pdf_page') or 0)
        end = int(row.get('pdf_page_end') or page)
        if page and end == page:
            grouped[page].append(str(row.get('text') or row.get('textContent') or ''))
        else:
            multi_page_rows.append(row.get('segment_id'))
    candidates = sorted(grouped)
    stable_seed = f'{seed}:{summary.get("source_sha256", source.name)}'
    selected = sorted(random.Random(stable_seed).sample(candidates, min(count, len(candidates))))
    report = {'seed': stable_seed, 'eligible_pages': len(candidates), 'selected_pages': selected,
              'multi_page_rows_not_compared': multi_page_rows, 'pages': [],
              'method': 'Page-local manifest body located in exported TXT; NFKC token multiset overlap with native PDF text.',
              'limitations': 'Native text is not visual ground truth. Header removal, OCR, columns, hyphenation and cleanup can change overlap. Samples do not establish full-document fidelity.'}
    if not source.is_file():
        report['status'] = 'source_missing'
        return report
    try:
        import fitz
        with fitz.open(source) as pdf:
            report['source_physical_pages'] = len(pdf)
            for page in selected:
                body = '\n'.join(grouped[page])
                record = {'pdf_page': page, 'manifest_chars': len(body),
                          'sample_body_present_in_export': bool(export) and all(normalized(part) in normalized(export) for part in grouped[page] if part.strip())}
                if page < 1 or page > len(pdf):
                    record['status'] = 'page_out_of_bounds'
                else:
                    native = pdf[page - 1].get_text('text')
                    original, prepared = Counter(tokens(native)), Counter(tokens(body))
                    matched = sum((original & prepared).values())
                    record.update(native_chars=len(native), native_tokens=sum(original.values()),
                                  prepared_tokens=sum(prepared.values()),
                                  native_token_recall=round(matched / sum(original.values()), 4) if original else None,
                                  prepared_token_precision=round(matched / sum(prepared.values()), 4) if prepared else None,
                                  native_private_use_chars=sum(unicodedata.category(c) == 'Co' for c in native),
                                  manifest_private_use_chars=sum(unicodedata.category(c) == 'Co' for c in body),
                                  native_replacement_chars=native.count('\ufffd'),
                                  manifest_replacement_chars=body.count('\ufffd'))
                    record['status'] = ('native_text_unavailable_for_comparison' if sum(original.values()) < 20
                                        else 'review_low_overlap' if record['native_token_recall'] < .8 or record['prepared_token_precision'] < .8
                                        else 'overlap_observed')
                report['pages'].append(record)
        report['status'] = 'sampled' if selected else 'no_page_local_manifest_rows'
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        report['status'] = 'comparison_unavailable'
        report['error'] = f'{type(exc).__name__}: {exc}'
    return report


def audit_document(path, samples, seed):
    root, errors = path.parent, []
    summary = load(path, errors, True)
    profile = load(root / 'source-profile.json', errors, True)
    source = Path(profile.get('source_file') or summary.get('source_file') or '__source_unavailable__')
    manifest = role(root, summary, 'segment-manifest.jsonl', 'manifest')
    rows = []
    if manifest:
        try:
            rows = read_manifest_rows(manifest)
        except (OSError, ValueError) as exc:
            errors.append(f'{manifest}: {exc}')
    else:
        errors.append('Retained segment manifest unavailable')
    export_path = role(root, summary, 'anythingllm-upload.txt', 'upload_file')
    export = export_path.read_text(encoding='utf-8-sig') if export_path else ''
    pages = profile.get('page_profile') or []
    covered = set()
    for row in rows:
        start = int(row.get('pdf_page') or 0)
        end = int(row.get('pdf_page_end') or start)
        if 0 < start <= end:
            covered.update(range(start, end + 1))
    total = int(profile.get('pdf_page_count') or summary.get('pdf_page_count') or 0)
    selected_start = int(summary.get('start_page') or 1)
    selected_end = int(summary.get('end_page') or total)
    expected = set(range(selected_start, selected_end + 1))
    inference = profile.get('author_inference') or {}
    warning_fields = ('readiness_status', 'readiness_reasons', 'ocr_content_status', 'ocr_assisted_extraction_used',
                      'automatic_targeted_ocr_pages', 'ocr_page_quality_summary', 'ocr_page_evidence',
                      'visual_text_review', 'text_export_hygiene', 'diagnostic_warning_count', 'diagnostic_error_count')
    checks = references(root, [('run-summary.json', summary), ('source-profile.json', profile)])
    for plan in sorted(root.rglob('*.csv')):
        if 'upload-plan' not in plan.name:
            continue
        with plan.open(encoding='utf-8-sig', newline='') as handle:
            for index, row in enumerate(csv.DictReader(handle), 1):
                if row.get('text_file'):
                    target = Path(row['text_file'])
                    checks.append({'evidence_file': str(plan), 'field': f'row[{index}].text_file',
                                   'path': str(target), 'exists': target.exists(), 'is_file': target.is_file(),
                                   'within_run': target.resolve().is_relative_to(root.resolve())})
    return {'document_root': str(root), 'source': str(source), 'evidence_errors': errors,
            'source_hash_matches': sha256(source) == summary.get('source_sha256') if source.is_file() and summary.get('source_sha256') else None,
            'metadata': {'title': summary.get('detected_title'), 'author': summary.get('detected_author'),
                         'provenance': profile.get('metadata_provenance') or summary.get('metadata_provenance'),
                         'author_inference': inference, 'confidence': inference.get('confidence', 'not_recorded'),
                         'manual_correctness_validation': 'not_performed'},
            'coverage': {'physical_page_count': total, 'selected_start': selected_start, 'selected_end': selected_end,
                         'manifest_covered_pages': sorted(covered), 'missing_selected_pages': sorted(expected - covered),
                         'outside_selected_pages': sorted(covered - expected),
                         'profile_page_count': len(pages), 'pages': pages},
            'warnings': {key: summary.get(key) for key in warning_fields},
            'reference_checks': checks, 'missing_reference_count': sum(not row['exists'] for row in checks),
            'export_path': str(export_path) if export_path else None,
            'sample_comparison': sample_pages(source, summary, rows, export, samples, seed)}


def retained_evidence_inventory(root):
    records = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.suffix not in {'.json', '.jsonl', '.log', '.csv', '.txt'}:
            continue
        record = {'path': str(path.relative_to(root)), 'bytes': path.stat().st_size,
                  'sha256': sha256(path), 'status': 'readable'}
        try:
            if path.suffix == '.json':
                read_run_json(path)
                record['status'] = 'json_and_evidence_references_validated'
            elif path.suffix == '.jsonl':
                record['rows'] = len(read_manifest_rows(path))
                record['status'] = 'jsonl_and_manifest_references_validated'
            elif path.suffix == '.log':
                lines = path.read_text(encoding='utf8', errors='replace').splitlines()
                record['warning_error_line_count'] = sum(bool(re.search(r'\b(?:warning|error|exception|traceback|failed)\b', line, re.I)) for line in lines)
                record['lines'] = len(lines)
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            record['status'] = 'invalid_or_unreadable'
            record['error'] = f'{type(exc).__name__}: {exc}'
        records.append(record)
    return records


def audit_run(root, samples, seed):
    errors = []
    progress = load(root / 'run-progress.json', errors, True)
    batch = load(root / 'batch-native-upload-report.json', errors, True)
    integrity = load(root / 'integrity-audit.json', errors, True)
    ledger = load(root / 'batch-embedding-ledger.json', errors, True)
    selected = batch.get('selected_records')
    confirmed = batch.get('vector_confirmed_records', batch.get('confirmed_vector_records'))
    document_results = batch.get('document_results') or {}
    per_source = []
    batch_proofs = []
    proven_locations = set()
    for row in ledger.get('batches') or []:
        verification = row.get('verification') or {}
        expected_locations = set(row.get('locations') or [])
        observed_locations = set(verification.get('current_upload_locations_with_vectors') or [])
        complete = (bool(expected_locations) and len(expected_locations) == row.get('requested')
                    and expected_locations <= observed_locations and row.get('searchability_proven') is True
                    and verification.get('status') == 'pass'
                    and verification.get('current_upload_vector_evidence_complete') is True
                    and verification.get('identity_set_checked') is True
                    and verification.get('identity_set_complete') is True
                    and not verification.get('missing_chunk_sources'))
        batch_proofs.append({'batch': row.get('batch'), 'requested': row.get('requested'),
                             'locations': sorted(expected_locations), 'vector_locations': sorted(observed_locations),
                             'classification': verification.get('classification'), 'exact_complete': complete})
        if complete:
            proven_locations.update(expected_locations)
    for source, row in document_results.items():
        expected = row.get('selected_records', row.get('records'))
        actual = row.get('vector_confirmed_records', row.get('confirmed_vector_records'))
        per_source.append({'source': source, 'selected_records': expected, 'confirmed_records': actual,
                           'searchability_proven': row.get('searchability_proven'), 'post_status': row.get('post_status'),
                           'complete': isinstance(expected, int) and expected > 0 and actual == expected
                                       and row.get('searchability_proven') is True and row.get('post_status') == 'pass'})
    documents = []
    for path in sorted(root.rglob('run-summary.json')):
        try:
            documents.append(audit_document(path, samples, seed))
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            errors.append(f'Document audit unavailable for {path}: {type(exc).__name__}: {exc}')
    inventory = retained_evidence_inventory(root)
    return {'run_root': str(root), 'terminal_state': progress.get('state', 'missing'),
            'terminal_success': progress.get('state') == 'successful', 'completion_code': progress.get('completion_code'),
            'evidence_errors': errors, 'retained_integrity_audit': integrity,
            'retained_evidence_inventory': inventory,
            'vector_proof': {'selected_records': selected, 'confirmed_records': confirmed,
                             'per_source': per_source, 'batches': batch_proofs,
                             'unique_exact_proven_locations': len(proven_locations),
                             'scope': 'Retained fresh-upload exact location and chunk-source verification; existing-workspace-only proof is not qualified here.',
                             'complete': isinstance(selected, int) and selected > 0 and selected == confirmed
                                         and len(proven_locations) == selected
                                         and bool(per_source) and all(row['complete'] for row in per_source)},
            'documents': documents}


def human_summary(report):
    lines = ['Full automatic-run evidence audit', '', report['scope'], '']
    for run in report['runs']:
        proof = run['vector_proof']
        lines.append(f"{run['run_root']}: terminal={run['terminal_state']}; vectors={proof['confirmed_records']}/{proof['selected_records']} exact_complete={proof['complete']}; audit={run['retained_integrity_audit'].get('audit_status', 'missing')}")
        inventory = run['retained_evidence_inventory']
        lines.append(f"  Retained artifact inventory: {len(inventory)} files; invalid/unreadable={sum(row['status'] == 'invalid_or_unreadable' for row in inventory)}")
        for doc in run['documents']:
            coverage, comparison = doc['coverage'], doc['sample_comparison']
            lines.append(f"  {Path(doc['source']).name}: physical={coverage['physical_page_count']}; selected={coverage['selected_start']}-{coverage['selected_end']}; missing={coverage['missing_selected_pages']}; missing references={doc['missing_reference_count']}; source hash={doc['source_hash_matches']}")
            lines.append(f"    Author={doc['metadata']['author']!r}; confidence={doc['metadata']['confidence']}; samples={comparison['selected_pages']} ({comparison['status']})")
            for sample in comparison['pages']:
                lines.append(f"    Page {sample['pdf_page']}: {sample['status']}; native recall={sample.get('native_token_recall')}; prepared precision={sample.get('prepared_token_precision')}; TXT presence={sample['sample_body_present_in_export']}")
            lines.extend('    ERROR: ' + message for message in doc['evidence_errors'])
        lines.extend('  ERROR: ' + message for message in run['evidence_errors'])
    lines.extend(['', 'Limitations: Native-text samples are heuristic comparisons, not visual validation, metadata correctness review, or full-document equality. Missing references may be optional or deliberately pruned; inspect their recorded fields. No live vector database or API was queried.'])
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='Run root or experiment base containing run-progress.json files')
    parser.add_argument('--output-prefix', type=Path, required=True, help='Receipt prefix outside retained run roots')
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--seed', default='full-automatic-20261005')
    options = parser.parse_args()
    if options.samples < 1:
        parser.error('--samples must be positive')
    roots = ([options.root] if (options.root / 'run-progress.json').exists()
             else sorted({path.parent for path in options.root.rglob('run-progress.json')}))
    if not roots:
        parser.error('No retained run-progress.json found')
    outputs = [options.output_prefix.with_suffix('.json'), options.output_prefix.with_suffix('.txt')]
    if any(output.resolve().is_relative_to(root.resolve()) for output in outputs for root in roots):
        parser.error('Receipts must be outside retained run roots')
    report = {'schema_version': 1, 'scope': 'Read-only retained evidence for every discovered run; all document profiles and deterministic random page-local TXT/native PDF samples. No OCR rerun or embedding mutation.',
              'runs': [audit_run(root, options.samples, options.seed) for root in roots]}
    for output in outputs:
        output.parent.mkdir(parents=True, exist_ok=True)
    outputs[0].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    outputs[1].write_text(human_summary(report), encoding='utf8')
    print(human_summary(report))
    print('Receipts:', *(str(output) for output in outputs))


if __name__ == '__main__':
    main()
