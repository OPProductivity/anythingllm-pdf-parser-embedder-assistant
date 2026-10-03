"""Read-only comparison of preserved research rows and isolated fresh evidence."""

import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_evidence import read_run_json

old_root = Path.home() / 'AppData/Local/AnythingLLM PDF Parser Embedder Assistant/run-state/automatic-runs/r-20261003-162544-e28b3f2caa'
old = read_run_json(old_root / 'batch-native-upload-report.json')
config = read_run_json(old_root / '.automatic-batch-upload-config.json')
cases = json.loads(Path('tmp-output/historical-replay-20261003/results.json').read_text())['cases']
storage = Path.home() / 'AppData/Roaming/anythingllm-desktop/storage'
rows = []
with sqlite3.connect((storage / 'anythingllm.db').as_uri() + '?mode=ro', uri=True) as db:
    for source, before in old['document_results'].items():
        locations = before.get('locations') or []
        if not locations:
            continue
        placeholders = ','.join('?' for _ in locations)
        count = db.execute('select count(v.id) from workspace_documents d join document_vectors v on v.docId=d.docId '
                           'where d.workspaceId=(select id from workspaces where slug=?) '
                           f'and d.docpath in ({placeholders})', [config['workspace_slug'], *locations]).fetchone()[0]
        index = next(index for index, case in enumerate(cases, 1) if case['source'] == source)
        after = read_run_json(Path('tmp-output/historical-replay-20261003/isolated-full-30bd88/assistant-home/run-state/automatic-runs')
                              / f'r-live-{index:02d}/batch-native-upload-report.json')
        fresh = sum(batch.get('verification', {}).get('current_upload_document_vector_count') or 0
                    for batch in after['embedding_update']['batches'])
        rows.append({'source': Path(source).name, 'preserved_physical_vectors': count,
                     'fresh_physical_vectors': fresh})
Path('tmp-output/historical-replay-20261003/vector-count-comparison.json').write_text(json.dumps(rows, indent=2), encoding='utf8')
print(json.dumps(rows))
