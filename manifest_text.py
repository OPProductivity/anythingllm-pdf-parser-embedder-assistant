"""One document-local text store for private structural manifest records."""

import hashlib
import json
import os
from pathlib import Path

STORE = 'manifest-text.jsonl'


class ManifestTextWriter:
    def __init__(self, root):
        self.path = Path(root) / STORE
        if self.path.is_symlink():
            raise ValueError('Manifest text store must not be a symlink')
        self.bodies = {}
        if self.path.exists():
            for line in self.path.read_text(encoding='utf8').splitlines():
                row = json.loads(line)
                if hashlib.sha256(row['text'].encode('utf8')).hexdigest() != row['sha256']:
                    raise ValueError('Manifest text store integrity mismatch')
                self.bodies[row['sha256']] = row['body_id']

    def record(self, row):
        result = dict(row)
        for key in ('text', 'textContent'):
            text = result.get(key)
            if not isinstance(text, str) or len(text.encode('utf8')) < 4096:
                continue
            digest = hashlib.sha256(text.encode('utf8')).hexdigest()
            if digest not in self.bodies:
                body_id = len(self.bodies) + 1
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open('a', encoding='utf8', newline='\n') as handle:
                    handle.write(json.dumps({'body_id': body_id, 'sha256': digest, 'text': text},
                                            ensure_ascii=False, separators=(',', ':')) + '\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                self.bodies[digest] = body_id
            result[key] = {'$manifest_text': 1, 'body_id': self.bodies[digest], 'sha256': digest}
        return result


def read_manifest_rows(path):
    """Resolve private text references or read legacy inline rows losslessly."""
    path = Path(path)
    bodies = None

    def resolve(value):
        nonlocal bodies
        if isinstance(value, dict) and '$manifest_text' in value:
            if set(value) != {'$manifest_text', 'body_id', 'sha256'} or value['$manifest_text'] != 1:
                raise ValueError('Invalid manifest text reference')
            if bodies is None:
                pool = next((parent / STORE for parent in path.resolve().parents
                             if (parent / STORE).exists()), None)
                if pool is None or pool.is_symlink() or not pool.is_file():
                    raise ValueError('Manifest text store missing or unsafe')
                bodies = {}
                for line in pool.read_text(encoding='utf8').splitlines():
                    row = json.loads(line)
                    if (row['body_id'] in bodies or not isinstance(row['text'], str)
                            or hashlib.sha256(row['text'].encode('utf8')).hexdigest() != row['sha256']):
                        raise ValueError('Manifest text store integrity mismatch')
                    bodies[row['body_id']] = row
            body = bodies.get(value['body_id'])
            if not body or body['sha256'] != value['sha256']:
                raise ValueError('Referenced manifest text unavailable or corrupt')
            return body['text']
        if isinstance(value, dict):
            return {key: resolve(child) for key, child in value.items()}
        if isinstance(value, list):
            return [resolve(child) for child in value]
        return value

    return [resolve(json.loads(line)) for line in path.read_text(encoding='utf8').splitlines() if line.strip()]
