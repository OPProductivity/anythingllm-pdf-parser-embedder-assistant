"""Legacy import facade for shared helpers; no worker patching or installation.

Production adapters import the neutral common module directly. Retain this
module name for helper-only callers without retaining obsolete Desktop writes.
"""
from anythingllm_source_atomic_common import (
    SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE as SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE,
    SOURCE_ATOMIC_MAX_PROVIDER_BATCH_SIZE as SOURCE_ATOMIC_MAX_PROVIDER_BATCH_SIZE,
    _activation_state_for_installed_worker as _activation_state_for_installed_worker,
    _atomic_write as _atomic_write,
    _desktop_root_started_after as _desktop_root_started_after,
    _sha256_bytes as _sha256_bytes,
)
