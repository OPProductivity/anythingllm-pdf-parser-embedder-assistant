"""Transient evidence supplied to one publication-specific author parser."""

from dataclasses import dataclass
from pathlib import Path

from .profile import DocumentProfile, classify_document


@dataclass(frozen=True)
class AuthorEvidenceContext:
    samples: tuple[dict, ...]
    title_hint: str
    path: Path | None
    profile: DocumentProfile

    @property
    def opening_pages(self):
        def is_opening(sample):
            try:
                return 1 <= int(sample.get("page") or 0) <= 4
            except (TypeError, ValueError):
                return False
        return tuple(sample for sample in self.samples if is_opening(sample))

    @classmethod
    def from_samples(cls, samples, *, title_hint="", path=None):
        selected = tuple(dict(sample) for sample in samples or [] if isinstance(sample, dict))
        return cls(
            samples=selected,
            title_hint=str(title_hint or ""),
            path=Path(path) if path is not None else None,
            profile=classify_document(selected, title_hint=title_hint),
        )
