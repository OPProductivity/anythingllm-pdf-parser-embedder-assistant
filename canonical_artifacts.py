"""Run-local static artifact aliases, without filesystem links or blob names."""

import hashlib
import json
import os
from pathlib import Path


class CanonicalArtifacts:
    """Keep role paths in an index and store each identical static body once."""

    def __init__(self, root, writer, existing=()):
        self.root = Path(root).resolve()
        self.writer = writer
        self.index = {}
        self.roles = {}
        for path in existing:
            if Path(path).is_file():
                self.register(path)

    def checked(self, path):
        path = Path(path)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError('Canonical artifact must stay within its document run')
        return path.resolve()

    def register(self, path):
        path = self.checked(path)
        content = path.read_bytes()
        key = (path.suffix, len(content), hashlib.sha256(content).digest())
        paths = self.index.setdefault(key, [])
        if path not in paths:
            paths.append(path)
        return path

    def role(self, preferred, actual):
        preferred, actual = self.checked(preferred), self.checked(actual)
        self.roles[str(preferred.relative_to(self.root))] = str(actual.relative_to(self.root))
        return actual

    def copy(self, source, preferred):
        return self.role(preferred, self.register(source))

    def promote(self, source, preferred):
        source, preferred = self.checked(source), self.checked(preferred)
        if source == preferred:
            return self.role(preferred, preferred)
        if preferred.exists():
            if preferred.read_bytes() != source.read_bytes():
                raise ValueError('Refusing to replace a different canonical artifact')
            source.unlink()
        else:
            preferred.parent.mkdir(parents=True, exist_ok=True)
            source.replace(preferred)
        for paths in self.index.values():
            paths[:] = [preferred if path == source else path for path in paths]
        old = str(source.relative_to(self.root))
        new = str(preferred.relative_to(self.root))
        self.roles = {role: new if actual == old else actual for role, actual in self.roles.items()}
        self.role(source, preferred)
        return self.role(preferred, preferred)

    def text(self, preferred, content):
        preferred = self.checked(preferred)
        existing = self.existing_text(content, preferred.suffix)
        if existing is not None:
            return self.role(preferred, existing)
        # Match the established Windows writer's newline translation exactly.
        encoded = content.replace('\n', os.linesep).encode('utf8')
        if preferred.exists() and preferred.read_bytes() != encoded:
            raise ValueError('Refusing to modify an existing static canonical artifact')
        self.writer(preferred, content)
        self.register(preferred)
        return self.role(preferred, preferred)

    def existing_text(self, content, suffix='.txt'):
        """Find reusable text without materializing an optional artifact."""
        encoded = content.replace('\n', os.linesep).encode('utf8')
        key = (suffix, len(encoded), hashlib.sha256(encoded).digest())
        for path in self.index.get(key, []):
            if path.is_file() and path.read_bytes() == encoded:
                return path
        return None

    def jsonl(self, preferred, rows):
        return self.text(preferred, ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))

    def remove_empty_alias_directories(self):
        for role, actual in self.roles.items():
            if role == actual:
                continue
            directory = self.checked(self.root / role).parent
            while directory != self.root:
                try:
                    directory.rmdir()
                except OSError:
                    break
                directory = directory.parent


def selected_artifact_path(selected, directory, name):
    return Path((selected.get('artifact_paths') or {}).get(name) or Path(directory) / name)
