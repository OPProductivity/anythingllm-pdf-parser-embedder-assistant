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

    def profile_evidence(self):
        pages = []
        for sample in self.samples:
            try:
                page = int(sample.get("page") or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if page > 0 and page not in pages:
                pages.append(page)
        return {
            "kind": self.profile.kind,
            "cues": list(self.profile.cues),
            "classification_pages": list(self.profile.sampled_pages),
            "sampled_pages": pages,
            "scope_complete": self.profile.scope_complete,
        }

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
