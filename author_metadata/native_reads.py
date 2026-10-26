"""Exact native measurements shared only inside one field-review invocation.

No OCR, acquisition plan, evidence bundle, genre, role or verdict is stored.
Every consumer receives a fresh value and still executes its own analysis.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import hashlib
from pathlib import Path

_ACTIVE = ContextVar('author_field_native_reads', default=None)


def _stamp(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _freeze(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return type(value).__name__, value
    if isinstance(value, (tuple, list)):
        return type(value).__name__, tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        return 'dict', tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    # In particular, a TextPage bound to a different Document is never reused.
    raise TypeError('Native measurement option has unsupported identity')


class _ReviewReads:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.stamp = _stamp(self.path)
        with self.path.open('rb') as stream:
            self.digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        self.values = {}
        self.documents = {}
        self.operations = {}
        self.invalidated = False

    def key(self, page, operation, options):
        if self.invalidated:
            return None
        try:
            if _stamp(self.path) != self.stamp:
                self.invalidated = True
                self.values.clear()
                return None
            doc = page.parent
            if not doc or doc.is_dirty or doc.is_encrypted or doc.needs_pass:
                return None
            if not doc.name or Path(doc.name).resolve() != self.path:
                return None
            identity = id(doc)
            prior = self.documents.get(identity)
            if prior is not doc:
                with self.path.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != self.digest:
                        self.invalidated = True
                        self.values.clear()
                        return None
                # PyMuPDF Document has no weak-reference support. Retain only
                # its Python identity until scope exit; callers still close
                # every native handle in their existing with-blocks.
                self.documents[identity] = doc
            import fitz
            geometry = (page.number, page.xref, page.rotation,
                        tuple(page.mediabox), tuple(page.cropbox),
                        fitz.TOOLS.set_small_glyph_heights())
            return self.digest, geometry, operation, _freeze(options)
        except (OSError, RuntimeError, ValueError, AttributeError, TypeError):
            return None

    def read(self, page, operation, options, acquire):
        key = self.key(page, operation, options)
        counts = self.operations.setdefault(operation, {'requests': 0, 'reads': 0, 'reused': 0})
        counts['requests'] += 1
        if key is not None and key in self.values:
            counts['reused'] += 1
            return deepcopy(self.values[key])
        counts['reads'] += 1
        value = acquire()
        if key is not None:
            self.values[key] = deepcopy(value)
        return value


@contextmanager
def native_review_scope(path):
    """Fresh request-local scope; missing/unverifiable sources use direct reads."""
    try:
        scope = _ReviewReads(path)
    except (OSError, ValueError):
        scope = None
    token = _ACTIVE.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE.reset(token)
        if scope is not None:
            scope.values.clear()
            scope.documents.clear()


def native_measurement(page, operation, options, acquire):
    scope = _ACTIVE.get()
    return acquire() if scope is None else scope.read(page, operation, options, acquire)


def native_read(page, method, *args, **kwargs):
    return native_measurement(page, method, (args, kwargs),
                              lambda: getattr(page, method)(*args, **kwargs))


def review_scope(function):
    @wraps(function)
    def wrapped(pdf_path, *args, **kwargs):
        with native_review_scope(pdf_path) as scope:
            result = function(pdf_path, *args, **kwargs)
            result['native_read_reuse'] = {
                'scope': 'one_field_review', 'ocr_reused': False,
                'expert_results_reused': False, 'evidence_bundles_reused': False,
                'operations': deepcopy(scope.operations) if scope else {},
                'source_invalidated': bool(scope and scope.invalidated),
            }
            return result
    return wrapped
