"""One document-local text store for private structural manifest records."""

import hashlib
import json
import os
from pathlib import Path

STORE = 'manifest-text.jsonl'
# At 512 bytes, sharing even two copies pays for the store and reference metadata.
# Tiny titles/labels remain inline; page bodies no longer need to exceed 4 KiB.
MIN_SHARED_TEXT_BYTES = 512


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
        return self.records([row])[0]

    def records(self, rows):
        bodies = dict(self.bodies)
        pending = []
        results = []
        for row in rows:
            result = dict(row)
            for key in ('text', 'textContent'):
                text = result.get(key)
                if not isinstance(text, str) or len(text.encode('utf8')) < MIN_SHARED_TEXT_BYTES:
                    continue
                digest = hashlib.sha256(text.encode('utf8')).hexdigest()
                if digest not in bodies:
                    body_id = len(bodies) + 1
                    pending.append(json.dumps({'body_id': body_id, 'sha256': digest, 'text': text},
                                              ensure_ascii=False, separators=(',', ':')) + '\n')
                    bodies[digest] = body_id
                result[key] = {'$manifest_text': 1, 'body_id': bodies[digest], 'sha256': digest}
            results.append(result)
        if pending:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Commit evidence before publishing any manifest that refers to it.
            with self.path.open('ab', buffering=0) as handle:
                boundary = handle.tell()
                try:
                    encoded = ''.join(pending).encode('utf8')
                    if handle.write(encoded) != len(encoded):
                        raise OSError('Incomplete manifest text evidence write')
                    os.fsync(handle.fileno())
                except OSError:
                    # Failed candidate preparation may reuse this catalog. Do
                    # not leave partial lines or reused ordinal IDs behind.
                    handle.truncate(boundary)
                    os.fsync(handle.fileno())
                    raise
            self.bodies = bodies
        return results


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
