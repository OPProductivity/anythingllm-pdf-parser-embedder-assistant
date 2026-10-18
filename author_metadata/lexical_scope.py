"""Reuse immutable lexical work only for the duration of one inference."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_SCOPE = ContextVar("publishing_lexical_scope", default=None)


@contextmanager
def lexical_scope():
    entries = {}
    token = _SCOPE.set(entries)
    try:
        yield
    finally:
        entries.clear()
        _SCOPE.reset(token)


def lexical_parse(kind, *, list_result=False):
    def decorate(parse):
        @wraps(parse)
        def scoped(value):
            entries = _SCOPE.get()
            if entries is None or not isinstance(value, str) or len(value) > 8192:
                return parse(value)
            key = (kind, value)
            if key not in entries:
                result = parse(value)
                if len(entries) >= 8192:
                    return result
                entries[key] = tuple(result)
            result = entries[key]
            return list(result) if list_result else result

        return scoped

    return decorate


def immutable_lexical_parse(kind):
    def decorate(parse):
        @wraps(parse)
        def scoped(*args, **kwargs):
            entries = _SCOPE.get()
            values = (*args, *kwargs.values())
            if entries is None or any(
                not isinstance(value, (str, bool))
                or isinstance(value, str)
                and len(value) > 8192
                for value in values
            ):
                return parse(*args, **kwargs)
            key = (kind, args, tuple(sorted(kwargs.items())))
            if key not in entries:
                result = parse(*args, **kwargs)
                if len(entries) >= 8192:
                    return result
                entries[key] = result
            return entries[key]

        return scoped

    return decorate
