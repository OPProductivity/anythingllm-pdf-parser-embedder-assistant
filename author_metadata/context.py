"""Transient evidence supplied to one publication-specific author parser."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .profile import DocumentProfile, classify_document


@dataclass(frozen=True)
class AuthorEvidenceContext:
    samples: tuple[dict, ...]
    title_hint: str
    path: Path | None
    profile: DocumentProfile
    evidence_bundle: dict | None = None
    acquisition_mode: str = "production"
    ocr_workers: int = 0
    lab_page_limit: int = 12
    recognition_effort: str = "extremely_thorough"
    acquisition_checkpoint: Callable | None = field(default=None, repr=False, compare=False)

    def profile_evidence(self):
        pages = []
        for sample in self.samples:
            try:
                page = int(sample.get("page") or 0)
            except TypeError, ValueError, AttributeError:
                continue
            if page > 0 and page not in pages:
                pages.append(page)
        return {
            "kind": self.profile.kind,
            "cues": list(self.profile.cues),
            "classification_pages": list(self.profile.sampled_pages),
            "sampled_pages": pages,
            "scope_complete": self.profile.scope_complete,
            "publication_type": self.profile.publication_type,
            "work_scope": self.profile.work_scope,
        }

    @property
    def opening_pages(self):
        def is_opening(sample):
            try:
                return 1 <= int(sample.get("page") or 0) <= 5
            except TypeError, ValueError:
                return False

        return tuple(sample for sample in self.samples if is_opening(sample))

    @property
    def planned_opening_pages(self):
        """Opening apparatus acquired for this request, excluding closing pages."""
        plan = (self.evidence_bundle or {}).get("trace", {}).get("opening_plan")
        if (
            plan is None
            or (self.evidence_bundle or {}).get("trace", {}).get("mode")
            == "supplied_samples_only"
        ):
            return self.opening_pages
        pages = set(plan)
        return tuple(s for s in self.samples if int(s.get("page") or 0) in pages)

    @classmethod
    def from_samples(
        cls,
        samples,
        *,
        title_hint="",
        path=None,
        profile=None,
        acquisition_mode="production",
        ocr_workers=0,
        lab_page_limit=12,
        recognition_effort="extremely_thorough",
        acquisition_checkpoint=None,
    ):
        from recognition_effort import normalize_effort

        selected = tuple(
            dict(sample) for sample in samples or [] if isinstance(sample, dict)
        )
        return cls(
            samples=selected,
            title_hint=str(title_hint or ""),
            path=Path(path) if path is not None else None,
            profile=profile or classify_document(selected, title_hint=title_hint),
            acquisition_mode=acquisition_mode,
            ocr_workers=ocr_workers,
            lab_page_limit=lab_page_limit,
            recognition_effort=normalize_effort(recognition_effort).value,
            acquisition_checkpoint=acquisition_checkpoint,
        )
