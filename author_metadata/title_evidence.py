"""Exact visible-title corroboration, including bounded catalog name labels."""

import re
import unicodedata


def words(value):
    return tuple(re.findall(r"[^\W_]+", unicodedata.normalize("NFC", value).casefold()))


def matches(visible, hint, names, *, allow_long_prefix=False):
    observed = words(visible)
    if len(observed) < 3:
        return False
    if observed == words(hint):
        return True
    parts = re.split(r"\s+(?:--|[-\u2013\u2014])\s+", hint)
    name_key = words(" ".join(names))
    # Only an entire delimiter-separated author label may be removed.
    if len(parts) > 1 and words(parts[0]) == name_key:
        parts = parts[1:]
    if len(parts) > 1 and words(parts[-1]) == name_key:
        parts = parts[:-1]
    expected = words(" ".join(parts))
    return expected == observed or (allow_long_prefix and len(expected) >= 8
                                   and observed[:len(expected)] == expected)


def not_found():
    return {"author": "", "source": "not_found", "page": 0, "evidence": ""}
