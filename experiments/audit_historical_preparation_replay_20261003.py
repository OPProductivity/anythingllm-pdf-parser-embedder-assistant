"""Compare retained historical and fresh preparation evidence without API calls."""

import difflib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from canonical_artifacts import checked_role_path
from manifest_text import read_manifest_rows
from run_evidence import read_run_json


def main():
    receipt = Path('tmp-output/historical-replay-20261003')
    replay = json.loads((receipt / 'results.json').read_text())
    comparisons = []
    for case in replay['cases']:
        if case['status'] != 'complete':
            comparisons.append({'source': case['source'], 'status': case['status'], 'error': case.get('error')})
            continue
        config = read_run_json(case['historical_config'])
        old = read_run_json(Path(case['historical_config']).parent / 'run-summary.json')
        root = Path(case['run_root']) / 'document'
        new = read_run_json(root / 'run-summary.json')
        before, after = read_manifest_rows(old['manifest']), read_manifest_rows(new['manifest'])
        old_text, new_text = '\n'.join(row['text'] for row in before), '\n'.join(row['text'] for row in after)
        index = read_run_json(root / 'artifact-locations.json')
        for role in index['roles'].values():
            assert checked_role_path(root, role).is_file(), role
        settings = {}
        for key in ('segment_mode', 'include_front_matter', 'include_back_matter',
                    'native_upload_representation', 'native_upload_transport', 'disable_inline_markers'):
            settings[key] = {'requested': config['args'].get(key), 'before': old.get(key), 'after': new.get(key)}
        changes = {key: {'before': old.get(key), 'after': new.get(key)}
                   for key in ('detected_title', 'detected_author', 'selected_backend', 'start_page',
                               'end_page', 'readiness_status', 'segments', 'page_parents')
                   if old.get(key) != new.get(key)}
        differences = []
        if old_text != new_text:
            matcher = difflib.SequenceMatcher(None, old_text.splitlines(), new_text.splitlines(), autojunk=True)
            for operation, a, b, c, d in matcher.get_opcodes():
                if operation != 'equal':
                    differences.append({'operation': operation, 'old_lines': [a, b], 'new_lines': [c, d],
                                        'old_sample': '\n'.join(old_text.splitlines()[a:b])[:300],
                                        'new_sample': '\n'.join(new_text.splitlines()[c:d])[:300]})
        comparisons.append({'source': case['source'], 'status': 'complete',
                            'original_run': Path(case['historical_config']).parents[1].name,
                            'pages': case['pages'], 'body_identical': old_text == new_text,
                            'old_body_contained': old_text in new_text, 'settings': settings,
                            'metadata_and_scope_changes': changes, 'text_differences': differences,
                            'ocr_cache_reused_pages': new.get('ocr_cache_reused_pages'),
                            'canonical_roles_verified': len(index['roles']),
                            'fresh_result': case['run_root']})
    output = receipt / 'comparison.json'
    output.write_text(json.dumps(comparisons, ensure_ascii=True, indent=2), encoding='utf8')
    print(json.dumps({'complete': sum(row['status'] == 'complete' for row in comparisons),
                      'not_complete': sum(row['status'] != 'complete' for row in comparisons),
                      'identical': sum(row.get('body_identical', False) for row in comparisons),
                      'changed': [Path(row['source']).name for row in comparisons
                                  if row['status'] == 'complete' and not row['body_identical']],
                      'ocr_cache_reuse': sum(row.get('ocr_cache_reused_pages') or 0 for row in comparisons)}))


if __name__ == '__main__':
    main()
