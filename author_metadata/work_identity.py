"""Distinguish the sampled work from a PDF's publication container."""

from dataclasses import dataclass
from pathlib import Path

from .identity import resolve_title_from_metadata_or_filename
from .profile import DocumentProfile, classify_document, has_browser_print_footer
from .title_evidence import words


@dataclass(frozen=True)
class WorkIdentity:
    title: str
    title_source: str
    container_title: str
    profile: DocumentProfile
    evidence: tuple[str, ...]
    visible_title_verified: bool = False

    def as_evidence(self):
        return {
            "work_title": self.title if self.visible_title_verified else "",
            "display_title": self.title,
            "work_title_source": self.title_source,
            "visible_title_verified": self.visible_title_verified,
            "container_title": self.container_title,
            "publication_type": self.profile.publication_type,
            "work_scope": self.profile.work_scope,
            "category": self.profile.kind,
            "cues": list(self.evidence + self.profile.cues),
            "classification_pages": list(self.profile.sampled_pages),
        }


def _visible_title_match(title, sample_text):
    candidate = words(title)
    opening = words(sample_text[:2500])
    if len(candidate) < 2 or len(candidate) > len(opening):
        return False
    if len(candidate) < 5:
        return any(words(line) == candidate for line in sample_text.splitlines()[:28])
    return any(opening[index:index + len(candidate)] == candidate
               for index in range(min(100, len(opening) - len(candidate) + 1)))


def resolve_work_identity(
    metadata_title, path: Path, samples, *, title_override="", use_file_title_fallback=True,
):
    """Use only a corroborated visible title to strip browser page furniture."""
    resolved = resolve_title_from_metadata_or_filename(
        metadata_title, path, title_override=title_override,
        use_file_title_fallback=use_file_title_fallback,
    )
    title, source = resolved["title"], resolved["source"]
    container = ""
    evidence = ()
    first_page = next((str(sample.get("text") or "") for sample in samples or []
                       if int(sample.get("page") or 0) == 1), "")
    browser_print = has_browser_print_footer(first_page)
    verified = bool(first_page and _visible_title_match(title, first_page))
    if browser_print and source == "pdf_metadata" and " | " in title:
        parts = [part.strip() for part in title.split(" | ") if part.strip()]
        for end in range(1, len(parts)):
            candidate = " | ".join(parts[:end])
            if _visible_title_match(candidate, first_page):
                title, container = candidate, parts[-1]
                source = "pdf_metadata_visible_work_title"
                evidence = ("visible_title_before_container",)
                verified = True
                break
    profile = classify_document(samples, title_hint=title)
    return WorkIdentity(title, source, container, profile, evidence, verified)
