"""Default test discovery must not read or lock the user's Desktop database."""
from pathlib import Path

import pytest

import anythingllm_compatibility
import auto_anythingllm_pipeline

pytestmark = pytest.mark.offline_deterministic


def test_default_desktop_storage_is_inside_the_temporary_test_profile(pytestconfig):
    isolated = Path(pytestconfig._pdf_test_profile.name).resolve()
    for storage in [auto_anythingllm_pipeline.default_anythingllm_storage_dir(),
                    anythingllm_compatibility.default_storage_dir()]:
        assert storage.resolve().is_relative_to(isolated)
