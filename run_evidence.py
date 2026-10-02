"""Lossless, run-local immutable snapshots for private control artifacts.

Only allowlisted diagnostic contracts use references. Native upload payloads,
exports and JSONL transaction journals remain ordinary self-contained records.
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from portable_paths import application_paths, is_private_run_state_path


DIRECTORY = '.run-evidence'
ARTIFACTS = frozenset({
    'run-summary.json', 'run-checkpoint.json', 'run-result.json',
    'source-profile.json', '.automatic-worker-config.json',
    '.automatic-worker-result.json',
})
SNAPSHOT_FIELDS = frozenset({
    'run_control', 'batch_inspection_context', 'anythingllm_resolved_state',
    'compatibility', 'resolved_state', 'global_read_only',
    'resolved_runtime_state', 'legacy_summary', 'evidence',
})
MINIMUM_BYTES = 4096


class RunEvidenceError(RuntimeError):
    """Referenced evidence is unsafe or incomplete; never load defaults."""


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf8')


def _private_root(path):
    path = Path(path).resolve()
    if not is_private_run_state_path(path):
        return None
    relative = path.relative_to(application_paths()['run_state'].resolve())
    # The evidence pool belongs to one run, never the shared application root.
    if len(relative.parts) < 3 or relative.parts[0] not in {'automatic-runs', 'interactive-runs'}:
        return None
    return application_paths()['run_state'].resolve().joinpath(*relative.parts[:2])


def _store(pool, data):
    digest = hashlib.sha256(data).hexdigest()
    pool.mkdir(parents=True, exist_ok=True)
    target = pool / (digest + '.json')
    if target.exists():
        if target.read_bytes() != data:
            raise ValueError('Run evidence snapshot integrity mismatch')
        return digest
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=pool, suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Atomic create-if-absent: concurrent workers cannot overwrite evidence.
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != data:
                raise ValueError('Run evidence snapshot integrity mismatch')
        except OSError as exc:
            if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV} and getattr(exc, 'winerror', None) not in {50, 1314}:
                raise
            # Portable data overrides can use filesystems without hard links.
            # Atomic publication remains safe: racing producers have the same
            # hash and identical bytes, never different snapshot contents.
            if target.exists():
                if target.read_bytes() != data:
                    raise ValueError('Run evidence snapshot integrity mismatch')
            else:
                os.replace(temporary, target)
                temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return digest


def prepare_private_json(path, payload: Any) -> Any:
    """Return a storage representation without mutating the caller's objects."""
    root = _private_root(path)
    if root is None or Path(path).name not in ARTIFACTS:
        return payload
    pool = root / DIRECTORY

    def visit(value, field=''):
        if isinstance(value, dict):
            transformed = {key: visit(child, key) for key, child in value.items()}
        elif isinstance(value, list):
            transformed = [visit(child) for child in value]
        else:
            return value
        if field in SNAPSHOT_FIELDS:
            data = _encoded(transformed)
            if len(data) >= MINIMUM_BYTES:
                return {'$run_evidence': 1, 'sha256': _store(pool, data)}
        return transformed

    return visit(payload)


def read_run_json(path) -> Any:
    """Read legacy JSON or fully resolve a new run's verified snapshots.

    A moved/copied complete run is supported. References have no paths, and
    cannot escape the nearest run-local evidence pool. Missing or corrupt
    evidence fails visibly rather than masquerading as an empty setting.
    """
    path = Path(path)
    pool = None
    cache = {}
    resolving = set()

    def visit(value):
        nonlocal pool
        if isinstance(value, dict):
            if set(value) == {'$run_evidence', 'sha256'}:
                digest = value['sha256']
                if value['$run_evidence'] != 1 or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
                    raise RunEvidenceError('Unsupported or malformed run evidence reference')
                if pool is None:
                    pool = next((parent / DIRECTORY for parent in path.resolve().parents
                                 if (parent / DIRECTORY).is_dir()), None)
                if pool is None:
                    raise RunEvidenceError('Run evidence pool missing')
                if pool.is_symlink():
                    raise RunEvidenceError('Run evidence pool must not be a symlink')
                target = pool / (digest + '.json')
                if target.is_symlink() or not target.resolve().is_relative_to(pool.resolve()):
                    raise RunEvidenceError('Run evidence snapshot escapes its pool')
                if digest in resolving:
                    raise RunEvidenceError('Cyclic run evidence reference')
                if digest not in cache:
                    try:
                        data = target.read_bytes()
                    except OSError as exc:
                        raise RunEvidenceError('Referenced run evidence snapshot unavailable') from exc
                    if hashlib.sha256(data).hexdigest() != digest:
                        raise RunEvidenceError('Run evidence snapshot integrity mismatch')
                    resolving.add(digest)
                    cache[digest] = visit(json.loads(data))
                    resolving.remove(digest)
                # Independent objects preserve the legacy JSON reader contract.
                return json.loads(json.dumps(cache[digest], ensure_ascii=False))
            return {key: visit(child) for key, child in value.items()}
        if isinstance(value, list):
            return [visit(child) for child in value]
        return value

    return visit(json.loads(path.read_text(encoding='utf8')))


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Inspect complete run evidence as ordinary JSON')
    parser.add_argument('path', type=Path)
    arguments = parser.parse_args()
    print(json.dumps(read_run_json(arguments.path), ensure_ascii=False, indent=2))
