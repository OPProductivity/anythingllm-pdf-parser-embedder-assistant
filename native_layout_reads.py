"""One extraction's native line measurements; no OCR or expert results."""

import hashlib
from pathlib import Path

import fitz


def stamp(path):
    value = path.stat()
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def geometry(page):
    return (
        page.number,
        page.xref,
        page.rotation,
        tuple(page.mediabox),
        tuple(page.cropbox),
        fitz.TOOLS.set_small_glyph_heights(),
    )


class NativeLayoutReads:
    """Transfer measured values between two views of the same full-page parse.

    A TextPage is used only while its original Page/Document is open. Only
    Python line measurements cross into the later layout phase; they are
    consumed once, guarded by content, source identity and page geometry.
    """

    def __init__(self, path, build):
        self.path = Path(path).resolve()
        self.build = build
        self.values = {}
        self.documents = {}
        self.valid = True
        try:
            self.version = stamp(self.path)
            self.digest = self._digest()
        except OSError:
            self.valid = False

    def _digest(self):
        with self.path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    def accepts(self, page):
        if not self.valid:
            return False
        try:
            if stamp(self.path) != self.version:
                self.valid = False
                self.values.clear()
                return False
            doc = page.parent
            if (
                not doc
                or doc.is_dirty
                or doc.is_encrypted
                or doc.needs_pass
                or not doc.name
                or Path(doc.name).resolve() != self.path
            ):
                return False
            if self.documents.get(id(doc)) is not doc:
                if self._digest() != self.digest:
                    self.valid = False
                    self.values.clear()
                    return False
                self.documents[id(doc)] = doc
            return True
        except OSError, RuntimeError, ValueError, AttributeError:
            return False

    def text(self, page):
        flags = fitz.TEXTFLAGS_TEXT
        if flags != (
            fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
        ) or not self.accepts(page):
            return page.get_text("text")
        # Both original reads use this exact full-page, image-free TextPage.
        # Extraction format still controls the independently returned view.
        textpage = page.get_textpage(flags=flags)
        text = page.get_text("text", textpage=textpage)
        self.values[page.number] = (geometry(page), self.build(page, textpage))
        return text

    def consume(self, page):
        if not self.accepts(page):
            return None
        stored = self.values.pop(page.number, None)
        if stored is None or stored[0] != geometry(page):
            return None
        return stored[1]
