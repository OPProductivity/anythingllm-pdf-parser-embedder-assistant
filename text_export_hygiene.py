"""Readable export boundary for the assistant's production pipeline.

Clean before building segment offsets and upload evidence. Never rewrite an
already-accounted payload at its final write boundary.
"""

from collections import Counter
from functools import lru_cache
import re
import unicodedata


# Explicit readable equivalents supplement Unicode compatibility decomposition.
# Unknown characters are removed by category/fallback, never copied through.
_ASCII_EQUIVALENTS = {
    "ß": "ss", "ẞ": "SS", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D",
    "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th", "ı": "i", "ŋ": "n", "Ŋ": "N",
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"', "«": '"', "»": '"',
    "‹": "'", "›": "'", "…": "...", "•": "*", "·": "*",
    "−": "-", "⁄": "/", "∕": "/", "×": "x", "÷": "/",
    "≤": "<=", "≥": ">=", "≠": "!=", "±": "+/-", "∞": "infinity",
    "→": "->", "←": "<-", "↔": "<->", "⇒": "=>", "⇐": "<=", "⇔": "<=>",
    "©": "(c)", "®": "(R)", "™": "(TM)", "§": "section ", "¶": "paragraph ",
    "€": "EUR ", "£": "GBP ", "¥": "JPY ", "¢": " cents", "°": " degrees",
}


@lru_cache(maxsize=2048)
def _ascii_equivalent(char):
    """Total mapping, including unseen Unicode values and surrogate code units.

    Private-use/unassigned values deliberately have no invented interpretation.
    This is the lossy fallback; normal Latin accents bypass it in the caller.
    """
    if ord(char) < 128:
        return char if char in "\n\t" or 32 <= ord(char) < 127 else ""
    category = unicodedata.category(char)
    if category[0] in "CM":
        return ""
    if char in _ASCII_EQUIVALENTS:
        return _ASCII_EQUIVALENTS[char]
    if category == "Pd":
        return "-"
    if category in ("Zl", "Zp"):
        return "\n"
    if category == "Zs":
        return " "
    decomposed = unicodedata.normalize("NFKD", char)
    if decomposed != char:
        return "".join(_ascii_equivalent(c) for c in decomposed)
    if category == "Nd":
        return str(unicodedata.decimal(char))
    return ""


@lru_cache(maxsize=2048)
def _latin_letter(char):
    canonical = unicodedata.normalize("NFC", char)
    return (len(canonical) == 1 and unicodedata.category(canonical).startswith("L")
            and "LATIN" in unicodedata.name(canonical, ""))


def _simplify_private_use_accent_clusters(text):
    """Simplify only an accented cluster interrupted inside a Latin word.

    Do not flatten the whole name or a normal accent beside a detached icon.
    This is a text-only fallback, not identification of a PUA glyph's meaning.
    """
    if not any(unicodedata.category(c) == "Co" for c in text):
        return text, 0
    output = []
    changed = 0
    cluster_start = None
    for i, char in enumerate(text):
        category = unicodedata.category(char)
        if category == "Co" and cluster_start is not None:
            # Consecutive PUA marks are handled when the final mark is reached.
            if i + 1 < len(text) and _latin_letter(text[i + 1]):
                cluster = "".join(output[cluster_start:])
                if any(unicodedata.category(c).startswith("M") for c in unicodedata.normalize("NFD", cluster)):
                    replacement = "".join(_ascii_equivalent(c) for c in cluster)
                    output[cluster_start:] = [replacement]
                    changed += 1
            output.append(char)
            continue
        if _latin_letter(char):
            cluster_start = len(output)
        elif not category.startswith("M"):
            cluster_start = None
        output.append(char)
    return "".join(output), changed


def readable_export_text(text: str, *, join_region_lines: bool = False) -> tuple[str, dict]:
    """No diagnostic placeholders; preserve paragraphs and ordinary hyphens.

    Preserve normal Latin letters/accents; remove unsupported character values.
    This is NOT a claim of recovered missing letters or corrected OCR spelling.
    """
    counts = Counter()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.isascii() and not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        return text, {}
    text, n = _simplify_private_use_accent_clusters(text)
    if n:
        counts["damaged_accent_clusters_simplified"] += n
    # Compatibility decomposition alone would make 10 superscript -3 become
    # 10-3, or 1 followed by a half become 11/2. Retain readable ASCII notation.
    for pattern, prefix, counter in (
        (r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾]+", "^", "superscript_runs_represented"),
        (r"[₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎]+", "_", "subscript_runs_represented"),
    ):
        def script_notation(match):
            value = "".join(_ascii_equivalent(c) for c in match[0])
            return prefix + (value if len(value) == 1 else f"({value})")
        text, n = re.subn(pattern, script_notation, text)
        if n:
            counts[counter] += n
    text, n = re.subn(r"(?<=[0-9])(?=[¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])", " ", text)
    if n:
        counts["mixed_fraction_spaces_inserted"] += n
    if join_region_lines:
        # A caller-supplied reading region is the boundary, not the whole page.
        # Preserve blank lines and indentation (possible tables or paragraphs).
        text, n = re.subn(r"(?<=[^\W\d_])\u00ad\n(?=[a-zà-öø-ÿ])", "", text)
        counts["soft_hyphen_region_lines_joined"] += n
        counts["soft_hyphen_removed"] += n
    urls = list(re.finditer(r"(?:https?://|www\.)[^\s]+", text, re.I))
    index = 0
    joined_space = -1
    parts = []
    latin_cluster = False
    for i, char in enumerate(text):
        if i == joined_space:
            continue
        category = unicodedata.category(char)
        latin_mark = (
            latin_cluster and category.startswith("M")
            and unicodedata.combining(char) > 0
            and unicodedata.name(char, "").startswith("COMBINING")
        )
        # Removing an invisible discretionary/direction mark must not destroy
        # a valid decomposed accent on the surviving Latin letter. Word-space
        # replacements and unknown glyphs still end the cluster.
        if char in ("\u00ad", "\u034f") or (category == "Cf" and char not in ("\ufeff", "\u200b")):
            pass
        else:
            latin_cluster = _latin_letter(char) or latin_mark
        while index < len(urls) and urls[index].end() <= i:
            index += 1
        in_url = index < len(urls) and urls[index].start() <= i < urls[index].end()
        if char == "\u00ad":
            counts["soft_hyphen_removed"] += 1
            # Only the explicit discretionary-break marker licenses joining.
            # Never span lines, paragraphs, tabs, or multiple-space boundaries;
            # callers clean separate reading regions independently.
            if (i > 0 and text[i-1].isalpha() and i + 2 < len(text)
                    and text[i+1] == " " and text[i+2].islower()):
                joined_space = i + 1
                counts["soft_hyphen_continuations_joined"] += 1
        elif char in "\v\f":
            parts.append("\n")
            counts["page_or_line_separator_to_newline"] += 1
        elif char in ("\ufeff", "\u200b"):
            if not in_url and i and i + 1 < len(text) and not text[i-1].isspace() and not text[i+1].isspace():
                parts.append(" ")
                counts["format_boundary_to_space"] += 1
            else:
                counts["format_mark_removed"] += 1
        elif category == "Cf":
            counts["format_mark_removed"] += 1
        elif (category == "Cc" and char not in "\n\t") or category == "Cs" or char == "\ufffd" or (ord(char) & 0xFFFF) in (0xFFFE, 0xFFFF) or 0xFDD0 <= ord(char) <= 0xFDEF:
            counts["unresolved_character_removed"] += 1
        elif ord(char) > 127:
            # Normal accents are not damage. Preserve both precomposed Latin
            # letters and genuine combining accents. Presentation ligatures
            # still expand to their readable letters.
            if (_latin_letter(char) and not 0xFB00 <= ord(char) <= 0xFB06) or latin_mark:
                # Canonical Latin aliases (e.g. ANGSTROM SIGN -> Å) must not
                # lose their accent through the lossy compatibility fallback.
                # Do not normalize whole strings or alter decomposed accents.
                canonical = unicodedata.normalize("NFC", char)
                parts.append(canonical)
                if canonical != char:
                    counts["canonical_latin_aliases_normalized"] += 1
                continue
            replacement = _ascii_equivalent(char)
            parts.append(replacement)
            counts["nonascii_replaced" if replacement else "nonascii_removed"] += 1
            if not replacement:
                counts[f"removed_category_{category}"] += 1
        else:
            parts.append(char)
    result = "".join(parts)
    result.encode("utf-8", errors="strict")
    return result, dict(counts)


# Standard Macintosh post-table glyph-name order (258 entries), converted to
# Unicode using the Adobe Glyph List. Data table, not a source-PDF fingerprint.
# https://developer.apple.com/fonts/TrueType-Reference-Manual/RM06/Chap6post.html
_STANDARD_GLYPH_UNICODE = ["","",""," ","!","\"","#","$","%","&","'","(",")","*","+",",","-",".","/","0","1","2","3","4","5","6","7","8","9",":",";","<","=",">","?","@","A","B","C","D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z","[","\\","]","^","_","`","a","b","c","d","e","f","g","h","i","j","k","l","m","n","o","p","q","r","s","t","u","v","w","x","y","z","{","|","}","~","Ä","Å","Ç","É","Ñ","Ö","Ü","á","à","â","ä","ã","å","ç","é","è","ê","ë","í","ì","î","ï","ñ","ó","ò","ô","ö","õ","ú","ù","û","ü","†","°","¢","£","§","•","¶","ß","®","©","™","´","¨","≠","Æ","Ø","∞","±","≤","≥","¥","µ","∂","∑","∏","π","∫","ª","º","Ω","æ","ø","¿","¡","¬","√","ƒ","≈","∆","«","»","…"," ","À","Ã","Õ","Œ","œ","–","—","“","”","‘","’","÷","◊","ÿ","Ÿ","⁄","¤","‹","›","ﬁ","ﬂ","‡","·","‚","„","‰","Â","Ê","Á","Ë","È","Í","Î","Ï","Ì","Ó","Ô","","Ò","Ú","Û","Ù","ı","ˆ","˜","¯","˘","˙","˚","¸","˝","˛","ˇ","Ł","ł","Š","š","Ž","ž","¦","Ð","ð","Ý","ý","Þ","þ","−","×","¹","²","³","½","¼","¾","₣","Ğ","ğ","İ","Ş","ş","Ć","ć","Č","č","đ"]
_COMMON_WORDS = frozenset(
    "the of and to in is that it for as with was on be by this are not from or "
    "an have which at but its they their has can these we more than one all "
    "also there into about such when what how would will been were them other "
    "some his her our who through between does only most if then those had".split()
)


def _language_evidence(text):
    words = re.findall(r"[a-z]+", text.lower())
    matches = [word for word in words if word in _COMMON_WORDS]
    return len(matches), len(set(matches)), len(matches) / max(1, len(words))


def _font_like_text(text):
    if len(text) < 90 or _language_evidence(text)[2] > .08:
        return False
    letters = [c for c in text if c.isascii() and c.isalpha()]
    upper = sum(c.isupper() for c in letters) / max(1, len(letters))
    broken = len(re.findall(r"[A-Za-z][_\[\]\\^][A-Za-z]|[_\[\]\\^][a-z]", text))
    return upper > .65 or broken / max(1, len(text.split())) > .08


_READABLE_IDENTIFIERS = re.compile(
    r"((?:https?://|www\.)[^\s]+"
    r"|\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b"
    r"|\b(?:doi:[ \t]*)?10\.[0-9]{4,9}/[^\s]+)",
    re.I,
)
_READABLE_URLS = re.compile(r"((?:https?://|www\.)[^\s]+)", re.I)


def _decode_font_text(text, method):
    # A normal URL is positive evidence of already-decoded text. A surrounding
    # damaged paragraph must not apply its unrelated font map to that token.
    # Email addresses and DOI identifiers carry similarly strong syntax.
    # Preserve them without exempting the surrounding mixed-font prose.
    # Most decoding hypotheses have neither an email nor a DOI. Avoid the
    # broader pattern then; these are necessary characters, not heuristics.
    pattern = _READABLE_IDENTIFIERS if "@" in text or "10." in text else _READABLE_URLS
    pieces = pattern.split(text)
    if len(pieces) > 1:
        return "".join(piece if i % 2 else _decode_font_text(piece, method)
                       for i, piece in enumerate(pieces))
    if method == "standard_glyph_order":
        return "".join(
            _STANDARD_GLYPH_UNICODE[ord(c)]
            if c not in " \t\r\n" and ord(c) < len(_STANDARD_GLYPH_UNICODE) else c
            for c in text
        )
    delta = int(method)
    return "".join(
        chr(ord(c) + delta)
        if c not in " \t\r\n" and ord(c) < 127 and 32 <= ord(c) + delta <= 126 else c
        for c in text
    )


def repair_font_encoded_text(text):
    """Bounded, text-only decoding hypothesis; never spellcheck ordinary prose.

    Each paragraph/window must independently demonstrate corruption and a
    substantial gain in several common words. Never propagate a font decision
    from one page, heading or region to another. Unknown font encodings remain
    imperfect; the export policy still removes their unsupported characters.
    """
    counts = Counter()
    output = []
    for block in re.split(r"(\r?\n[ \t]*\r?\n|\r[ \t]*\r)", text):
        # Bound work and prevent one convincing opening paragraph from
        # licensing the remainder of a mixed-encoding document.
        windows = re.findall(r".{1,2500}(?:\s+|$)|.{1,2500}", block, flags=re.S)
        for window in windows:
            if not _font_like_text(window):
                output.append(window)
                continue
            base = _language_evidence(window)
            choices = []
            methods = ["standard_glyph_order"] + [i for i in range(-40, 41) if i]
            for method in methods:
                candidate = _decode_font_text(window, method)
                evidence = _language_evidence(candidate)
                if (evidence[0] >= 8 and evidence[1] >= 5 and evidence[2] >= .22
                        and evidence[0] >= base[0] + 7 and evidence[2] >= base[2] + .15):
                    # Prefer the documented complete table to its incomplete
                    # ASCII-shift equivalent when lexical evidence is equal.
                    choices.append((evidence[0], method == "standard_glyph_order",
                                    evidence[2], method, candidate))
            if choices:
                best = max(choices, key=lambda row: row[:3])
                # str.splitlines would treat glyph-valued C0 controls as
                # line boundaries. Only physical LF/CR are layout here.
                physical_lines = re.split(r"(\r\n|\n|\r)", window)
                repaired = []
                for line in physical_lines:
                    decoded = _decode_font_text(line, best[3])
                    before, after = _language_evidence(line), _language_evidence(decoded)
                    if ((before[0] >= 1 and after[0] < before[0])
                            or (line.strip() and after[0] == 0)
                            or (len(line) >= 90 and _font_like_text(line) and after[2] < .10)):
                        repaired.append(line)
                        counts["mixed_font_lines_left_unchanged"] += 1
                        continue
                    if best[3] != "standard_glyph_order":
                        # These remaining values belong to an unidentified
                        # font encoding, not trustworthy Unicode accents.
                        counts["unmapped_font_glyphs_removed"] += sum(ord(c) > 127 for c in decoded)
                        decoded = "".join(c for c in decoded if ord(c) < 128)
                    repaired.append(decoded)
                decoded = "".join(repaired)
                # Some encodings retain a frequent control-valued word gap.
                # Require multiple distinct ordinary word boundaries, not
                # merely frequent damage inside words (e.g. a lost fi glyph).
                controls = Counter(c for c in decoded if ord(c) < 32 and c not in "\r\n\t")
                for gap, number in controls.items():
                    if number < 8 or number < sum(controls.values()) * .60:
                        continue
                    adjacent = re.findall(r"([A-Za-z]+)" + re.escape(gap) + r"([A-Za-z]+)", decoded)
                    matches = [w.lower() for pair in adjacent for w in pair if w.lower() in _COMMON_WORDS]
                    if len(matches) >= len(adjacent) * .40 and len(set(matches)) >= 5:
                        decoded = decoded.replace(gap, " ")
                        counts["encoded_word_separators_restored"] += number
                output.append(decoded)
                counts["font_decoded_windows"] += 1
                counts["font_decoded_input_characters"] += len(window)
                counts["standard_glyph_windows" if best[3] == "standard_glyph_order" else "uniform_shift_windows"] += 1
            else:
                output.append(window)
                counts["font_like_unresolved_windows"] += 1
    return "".join(output), dict(counts)


def prepare_readable_pages(pdf_path, pages, *, progress_callback=None):
    """Text-only export preparation; never reopen a PDF or invoke OCR.

    Retain the caller signature, raw quality-gate inputs, reading-region
    boundaries and compact timing schema. The original OCR/extraction pipeline
    is unchanged. A missing source file cannot affect this sanitation step.
    """
    import time

    started = time.monotonic()
    counts = Counter()
    output = []
    last_report = started - 1.0
    for page_info in pages:
        if callable(progress_callback) and time.monotonic() - last_report >= 1.0:
            progress_callback(int(page_info["page"]))
            last_report = time.monotonic()
        page = dict(page_info)
        regions = [dict(r) for r in page_info.get("reading_regions") or []]
        for target in regions if regions else [page]:
            decoded, repair_counts = repair_font_encoded_text(str(target.get("text") or ""))
            counts.update(repair_counts)
            text, changes = readable_export_text(
                decoded, join_region_lines=bool(regions),
            )
            target["text"] = text
            counts.update(changes)
        if regions:
            page["reading_regions"] = regions
            page["text"] = "\n\n".join(str(r.get("text") or "") for r in regions)
        output.append(page)
    return output, {
        "schema_version": 2, "policy": "text_only_readable_v4",
        "counts": dict(counts), "seconds": round(time.monotonic() - started, 3),
        "source_word_ocr_seconds": 0.0,
    }
