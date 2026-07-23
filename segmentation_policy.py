"""Versioned public contracts for local segmentation modes.

These policies describe how the local preparer forms records. They do not
disable AnythingLLM Desktop's own later text splitter. ``none`` is therefore a
valid observation mode that emits one local content file, not a promise of one
stored vector or exact page-level final provenance after upload.
"""

from dataclasses import asdict, dataclass


ALGORITHM_VERSION = "semantic-page-boundary-v1"
UNKNOWN_MODEL_HARD_LIMIT = 4096


@dataclass(frozen=True)
class SegmentationPolicy:
    mode: str
    algorithm_version: str
    page_local: bool
    target_drift_fraction: float
    small_tail_fraction: float
    semantic_priority: float
    size_uniformity_priority: float
    transition_companions: bool = False

    def to_dict(self):
        return asdict(self)


POLICIES = {
    "none": SegmentationPolicy(
        "none", ALGORITHM_VERSION, False, 0.0, 0.0, 0.0, 0.0,
    ),
    "page_limit": SegmentationPolicy(
        "page_limit", ALGORITHM_VERSION, True, 0.30, 0.35, 1.0, 0.55,
    ),
    # ``page_passages`` retains the same page-local provenance contract as
    # ``page_limit``, but treats the requested target as an actual split
    # target.  ``page_limit`` deliberately remains the page-preserving
    # automatic mode: it keeps a page intact until the safety ceiling demands
    # subdivision.
    "page_passages": SegmentationPolicy(
        "page_passages", ALGORITHM_VERSION, True, 0.30, 0.35, 1.0, 0.55,
    ),
    "page": SegmentationPolicy(
        "page", ALGORITHM_VERSION, True, 0.30, 0.35, 1.0, 0.25,
    ),
    "passages": SegmentationPolicy(
        "passages", ALGORITHM_VERSION, True, 0.20, 0.45, 0.75, 1.0,
    ),
}


def policy_for(mode):
    normalized = str(mode or "").strip().casefold()
    if normalized not in POLICIES:
        raise ValueError(f"Unsupported segmentation mode: {mode!r}")
    return POLICIES[normalized]


def source_span_identity(source_sha256, physical_page, normalized_page_hash, start, end, ordinal, algorithm_version=ALGORITHM_VERSION):
    import hashlib
    payload = "|".join(map(str, (
        source_sha256, physical_page, normalized_page_hash, start, end, ordinal, algorithm_version,
    )))
    return "seg-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
