"""A bounded title hypothesis from an opening's actual credit structure."""

import re
from .structured_credit import _names
from .title_evidence import words


def opening_title(lines):
    """Only an opening catalog record or title/name/introduction sequence.

    No title dictionary, expected labels, filename or PDF Author is used.
    """
    lines = [line.strip() for line in lines if line.strip()]
    if len(lines) < 4:
        return ""
    for index in range(min(3, len(lines) - 2)):
        title, credit = lines[index : index + 2]
        if not 3 <= len(words(title)) <= 24 or not _names(credit):
            continue
        if re.search(
            r"(?i)\b(?:edited|translated|foreword|review|contents)\b",
            title + " " + credit,
        ):
            continue
        tail = "\n".join(lines[index + 2 : index + 10])
        catalog = bool(
            re.search(r"(?i)\b(?:vol\.|source:)\s*\d*", tail)
            and re.search(r"(?i)stable url|jstor\.org", tail)
        )
        introduction = bool(
            re.match(
                r"(?i)^(?:[IVX]+\s*)?(?:introduction|abstract)\b",
                " ".join(lines[index + 2 : index + 4]),
            )
        )
        if catalog or introduction:
            return title
    return ""
