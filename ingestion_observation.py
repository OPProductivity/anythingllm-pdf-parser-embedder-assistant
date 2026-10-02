"""Cheap commit hints; never substitutes for exact physical vector proof."""

from pathlib import Path
import sqlite3
from contextlib import closing


class SubmissionCommitSignal:
    """Wake exact verification when mappings commit despite missing SSE events.

    The fingerprint only avoids repeating the same SQLite query; it is not a
    completion verdict or liveness clock. No LanceDB writer is opened here.
    """

    def __init__(self, storage_dir, workspace_slug):
        self.database = Path(storage_dir) / 'anythingllm.db'
        self.workspace_slug = workspace_slug
        self.last_key = None
        self.complete = False

    def observe(self, locations):
        expected = tuple(sorted(set(str(path) for path in locations if str(path))))
        if not expected:
            return False
        try:
            fingerprint = []
            for path in (self.database, Path(str(self.database) + '-wal')):
                try:
                    info = path.stat()
                    fingerprint.append((info.st_size, info.st_mtime_ns))
                except FileNotFoundError:
                    fingerprint.append(None)
            key = (expected, tuple(fingerprint))
            if key == self.last_key:
                return self.complete
            present = set()
            # A tiny, strictly read-only, bounded query. Busy/unavailable
            # storage is uncertainty, not an upload failure or retry license.
            with closing(sqlite3.connect(self.database.resolve().as_uri() + '?mode=ro',
                                         uri=True, timeout=0.025)) as connection:
                for start in range(0, len(expected), 256):
                    portion = expected[start:start + 256]
                    placeholders = ','.join('?' for _ in portion)
                    rows = connection.execute(
                        'SELECT wd.docpath FROM workspace_documents wd '
                        'JOIN workspaces w ON w.id=wd.workspaceId '
                        f'WHERE w.slug=? AND wd.docpath IN ({placeholders}) '
                        'AND EXISTS (SELECT 1 FROM document_vectors dv WHERE dv.docId=wd.docId)',
                        (self.workspace_slug, *portion),
                    )
                    present.update(row[0] for row in rows)
            self.last_key = key
            self.complete = present == set(expected)
            return self.complete
        except (OSError, sqlite3.Error):
            # Do not memoize a failed observation: it can resolve without
            # changing the database fingerprint (e.g. a short sharing lock).
            return False
