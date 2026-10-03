"""Read-only source census and isolated OCR comparisons; never embeds documents."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_anythingllm_pipeline as pipeline
import rag_pdf_tools


ROOT = Path.home() / 'Documents/Documenten 2025 - 2026/studie/_blok 1 & 2 & 3 & 4'
SOURCES = [ROOT / 'Film II/Articles', ROOT / 'MMT - Keywords Resit/sources']


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')), encoding='utf8')
    temporary.replace(path)


def census(output):
    paths = sorted(path for root in SOURCES for path in root.rglob('*') if path.suffix.lower() == '.pdf')
    documents = []
    for index, path in enumerate(paths, 1):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt = output / 'census' / (digest + '.json')
        if receipt.exists() and not json.loads(receipt.read_text(encoding='utf8')).get('error'):
            record = json.loads(receipt.read_text(encoding='utf8'))
        else:
            record = {'sha256': digest, 'pages': [], 'error': ''}
            try:
                with fitz.open(path) as pdf:
                    for number, page in enumerate(pdf, 1):
                        area = max(page.rect.get_area(), 1)
                        images = page.get_images(full=True)
                        ratio = max(((page.get_image_bbox(row) & page.rect).get_area() / area
                                     for row in images), default=0)
                        text = page.get_text()
                        plan = {'applied': False, 'reason': 'no_page_sized_scan_background'}
                        if ratio >= .65:
                            rows = pipeline._layout_line_rows(page)
                            plan = pipeline._layout_marginal_annotation_plan(rows, page.rect.width, page.rect.height)
                        record['pages'].append({
                            'page': number, 'words': len(text.split()), 'raster_ratio': round(ratio, 4),
                            'raster_backed': ratio >= .65, 'image_without_substantial_text': ratio >= .65 and len(text.split()) < 40,
                            'native_body_candidate': bool(plan.get('applied')) and ratio >= .65,
                            'body_bounds': plan.get('body_bounds'), 'width': page.rect.width,
                            'annotation_reason': plan.get('reason'),
                        })
                        if number % 100 == 0:
                            print(f'PAGE_SCAN {path.name}: {number}/{len(pdf)}', flush=True)
            except Exception as exc:
                record['error'] = f'{type(exc).__name__}: {exc}'
            save(receipt, record)
        pages = record['pages']
        scans = sum(row['raster_backed'] for row in pages)
        fraction = scans / max(len(pages), 1)
        documents.append({'path': str(path), 'sha256': digest, 'page_count': len(pages),
                          'raster_pages': scans, 'raster_fraction': round(fraction, 4),
                          'image_only_pages': sum(row['image_without_substantial_text'] for row in pages),
                          'native_body_candidates': sum(row['native_body_candidate'] for row in pages),
                          'cohort': 'majority_scan' if fraction >= .5 else 'substantial_minority_scan' if fraction >= .2 else 'other',
                          'error': record['error']})
        print(f'CENSUS {index}/{len(paths)} {path.name}: pages={len(pages)} scans={scans} body_candidates={documents[-1]["native_body_candidates"]}', flush=True)
        save(output / 'inventory.json', documents)
    with (output / 'inventory.csv').open('w', encoding='utf8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(documents[0]) if documents else [])
        writer.writeheader()
        writer.writerows(documents)
    print('TOTAL', json.dumps({'paths': len(documents), 'unique_pdfs': len({row['sha256'] for row in documents}),
                              'pages_by_path': sum(row['page_count'] for row in documents),
                              'cohorts_by_path': {key: sum(row['cohort'] == key for row in documents) for key in ['majority_scan', 'substantial_minority_scan', 'other']},
                              'candidate_documents_by_path': sum(row['native_body_candidates'] > 0 for row in documents),
                              'candidate_pages_by_path': sum(row['native_body_candidates'] for row in documents),
                              'errors': sum(bool(row['error']) for row in documents)}), flush=True)


def compare(output):
    inventory = json.loads((output / 'inventory.json').read_text(encoding='utf8'))
    seen = set()
    for document in inventory:
        digest = document['sha256']
        if digest in seen:
            continue
        seen.add(digest)
        if document['error'] or not document['native_body_candidates']:
            continue
        path = Path(document['path'])
        census_record = json.loads((output / 'census' / (digest + '.json')).read_text(encoding='utf8'))
        with fitz.open(path) as pdf:
            for row in census_record['pages']:
                if not row['native_body_candidate']:
                    continue
                target = output / 'comparisons' / digest / f'page-{row["page"]}.json'
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    continue
                page = pdf[row['page'] - 1]
                native = page.get_text()
                baseline_psm = (6 if pipeline._layout_embedded_symbol_noise_score(native) == 0
                                and pipeline._layout_text_noise_score(native) > 0 else 4)
                variants = {}
                for psm in [4, 6]:
                    started = time.monotonic()
                    text = pipeline._reocr_confirmed_native_body_region(path, row['page'], row['body_bounds'], row['width'], segmentation_mode=psm)
                    variants[str(psm)] = {'text': text, 'elapsed_seconds': round(time.monotonic() - started, 4),
                                          'gate': pipeline._native_body_reocr_decision(native, text)}
                result = {'pdf': str(path), 'sha256': digest, 'page': row['page'], 'baseline_psm': baseline_psm,
                          'body_bounds': row['body_bounds'], 'width': row['width'], 'native_text': native,
                          'variants': variants}
                save(target, result)
                print(f'COMPARE {path.name} p{row["page"]} baseline={baseline_psm} selected={variants[str(baseline_psm)]["gate"]["selected"]}', flush=True)


def ocr_pair(job):
    """Each process owns its PDF handles and isolated recognition override."""
    path, number, runtime, target = job
    original = rag_pdf_tools._resolve_ocr_recognition
    results = {}
    try:
        for variant in ['production', 'force_resolved_psm4_to_psm6']:
            def resolve(cropped, requested):
                mode, model, evidence = original(cropped, requested)
                if variant != 'production' and mode == 4:
                    evidence = {**evidence, 'psm': 6, 'experimental_original_psm': 4}
                    mode = 6
                return mode, model, evidence
            rag_pdf_tools._resolve_ocr_recognition = resolve
            started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix='rag-corpus-ocr-') as scratch:
                result = rag_pdf_tools._unstructured_one_page(path, number - 1, 'ocr_only', scratch, runtime)
            results[variant] = {'result': result, 'elapsed_seconds': round(time.monotonic() - started, 4)}
        receipt = {'pdf': path, 'page': number, 'variants': results, 'error': ''}
    except Exception as exc:
        receipt = {'pdf': path, 'page': number, 'variants': results, 'error': f'{type(exc).__name__}: {exc}'}
    finally:
        rag_pdf_tools._resolve_ocr_recognition = original
    save(Path(target), receipt)
    return Path(path).name, number, receipt['error']


def qualify(output, force_all=False, articles_first=False):
    inventory = json.loads((output / 'inventory.json').read_text(encoding='utf8'))
    unique = {row['sha256']: row for row in inventory if not row['error']}
    runtime = rag_pdf_tools.unstructured_runtime_status('ocr_only')
    jobs = []
    for digest, row in unique.items():
        if articles_first and row['page_count'] > 100:
            continue
        if not force_all and row['cohort'] == 'other' and not row['native_body_candidates']:
            continue
        census_record = json.loads((output / 'census' / (digest + '.json')).read_text(encoding='utf8'))
        native_target = output / 'native-layout' / (digest + '.json')
        native_target.parent.mkdir(exist_ok=True)
        if not native_target.exists():
            started = time.monotonic()
            pages, count = rag_pdf_tools.get_pages_with_pymupdf(Path(row['path']))
            def progress(number, total, stage):
                if number % 100 == 0:
                    print(f'NATIVE_PROGRESS {Path(row["path"]).name}: {number}/{total} {stage}', flush=True)
            chosen, evidence = pipeline.apply_region_aware_native_layout(Path(row['path']), pages, progress_callback=progress)
            chosen, sanitation = pipeline.prepare_readable_pages(Path(row['path']), chosen)
            save(native_target, {'pdf': row['path'], 'pages': chosen, 'layout_evidence': evidence,
                                 'sanitation': sanitation, 'page_count': count,
                                 'elapsed_seconds': round(time.monotonic() - started, 4)})
            print(f'NATIVE_BASELINE {Path(row["path"]).name}: {count} pages', flush=True)
        for page in census_record['pages']:
            if not (page['raster_backed'] if force_all else page['image_without_substantial_text']):
                continue
            target = output / 'ocr-pairs' / digest / f'page-{page["page"]}.json'
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not json.loads(target.read_text(encoding='utf8')).get('error'):
                continue
            jobs.append((row['path'], page['page'], runtime, str(target)))
    print(f'OCR_PAIR_JOBS {len(jobs)} force_all={force_all}', flush=True)
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ocr_pair, job) for job in jobs]
        for index, future in enumerate(as_completed(futures), 1):
            name, number, error = future.result()
            print(f'OCR_PAIR {index}/{len(jobs)} {name} p{number}: {error or "complete"}', flush=True)


def benchmark(output):
    inventory = json.loads((output / 'inventory.json').read_text(encoding='utf8'))
    runtime = rag_pdf_tools.unstructured_runtime_status('ocr_only')
    examples = [('David Gillespie', 2), ('Roberto Rossellini', 3), ('Handbook of Latinos', 100),
                ('Cultures Of United States', 100), ('Joseph Garncarz', 7), ('Hansen-Early', 10)]
    jobs = []
    for prefix, number in examples:
        row = next(row for row in inventory if Path(row['path']).name.startswith(prefix))
        target = output / 'benchmark' / f'{row["sha256"]}-page-{number}.json'
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            jobs.append((row['path'], number, runtime, str(target)))
    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(ocr_pair, job) for job in jobs]):
            name, number, error = future.result()
            print(f'BENCHMARK {name} p{number}: {error or "complete"}', flush=True)
    print(f'BENCHMARK_WALL_SECONDS {time.monotonic() - started:.3f}', flush=True)


def render_review(output):
    for target in (output / 'comparisons').rglob('page-*.json'):
        row = json.loads(target.read_text(encoding='utf8'))
        with fitz.open(row['pdf']) as pdf:
            pdf[row['page'] - 1].get_pixmap(matrix=fitz.Matrix(1.6, 1.6)).save(target.with_suffix('.png'))


def report(output):
    """Count completed article pairs without equating text changes with accuracy."""
    inventory = json.loads((output / 'inventory.json').read_text(encoding='utf8'))
    unique = {row['sha256']: row for row in inventory if not row['error']}
    rows = []
    missing = []
    for digest, document in unique.items():
        if document['page_count'] > 100:
            continue
        census_record = json.loads((output / 'census' / (digest + '.json')).read_text(encoding='utf8'))
        for page in census_record['pages']:
            if not page['raster_backed']:
                continue
            target = output / 'ocr-pairs' / digest / f'page-{page["page"]}.json'
            if not target.exists():
                missing.append({'pdf': document['path'], 'page': page['page']})
                continue
            pair = json.loads(target.read_text(encoding='utf8'))
            old = pair['variants'].get('production', {})
            new = pair['variants'].get('force_resolved_psm4_to_psm6', {})
            old_page = old.get('result', {}).get('page_row', {})
            new_page = new.get('result', {}).get('page_row', {})
            left, right = old_page.get('text', ''), new_page.get('text', '')
            rows.append({'pdf': document['path'], 'sha256': digest, 'page': page['page'],
                         'old_kind': old_page.get('kind', ''), 'new_kind': new_page.get('kind', ''),
                         'old_words': len(left.split()), 'new_words': len(right.split()),
                         'word_delta': len(right.split()) - len(left.split()), 'text_changed': left != right,
                         'old_seconds': old.get('elapsed_seconds', 0), 'new_seconds': new.get('elapsed_seconds', 0),
                         'error': pair['error'], 'receipt': str(target)})
    with (output / 'article-comparison.csv').open('w', encoding='utf8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
    summary = {'unique_pdfs_in_inventory': len(unique), 'completed_article_pairs': len(rows),
               'article_pdfs': len({row['sha256'] for row in rows}), 'missing': missing,
               'errors': [row for row in rows if row['error']],
               'changed_text_pages': sum(row['text_changed'] for row in rows),
               'old_route_counts': {kind: sum(row['old_kind'] == kind for row in rows)
                                    for kind in sorted({row['old_kind'] for row in rows})},
               'warning': 'Word counts and changed text are not accuracy measures; full books deferred.'}
    save(output / 'article-comparison-summary.json', summary)
    review = output / 'article-visual-review'
    review.mkdir(exist_ok=True)
    for row in sorted((row for row in rows if row['text_changed']),
                      key=lambda row: abs(row['word_delta']), reverse=True)[:6]:
        with fitz.open(row['pdf']) as pdf:
            pdf[row['page'] - 1].get_pixmap(matrix=fitz.Matrix(1.6, 1.6)).save(
                review / f'{row["sha256"]}-page-{row["page"]}.png')
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['census', 'compare', 'qualify', 'benchmark', 'render', 'report'])
    parser.add_argument('--force-all-scan-pages', action='store_true')
    parser.add_argument('--articles-first', action='store_true')
    parser.add_argument('--output', type=Path, default=Path('tmp-output/ocr-corpus-20261003'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'census').mkdir(exist_ok=True)
    if args.stage == 'qualify':
        qualify(args.output, args.force_all_scan_pages, args.articles_first)
    elif args.stage == 'benchmark':
        benchmark(args.output)
    elif args.stage == 'render':
        render_review(args.output)
    elif args.stage == 'report':
        report(args.output)
    else:
        (census if args.stage == 'census' else compare)(args.output)
