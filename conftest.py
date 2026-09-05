"""Keep test imports and their diagnostic logging out of the live app profile."""

import os
import tempfile


_PROFILE_ENV = "ANYTHINGLLM_PDF_ASSISTANT_HOME"


def pytest_configure(config):
    # Set this before collection imports the app and initializes its logger.
    # A fixture is too late; individual modules previously relied on their
    # collection order to establish a test profile.
    config._pdf_previous_profile = os.environ.get(_PROFILE_ENV)
    config._pdf_test_profile = tempfile.TemporaryDirectory(
        prefix="pdf-assistant-pytest-", ignore_cleanup_errors=True
    )
    os.environ[_PROFILE_ENV] = config._pdf_test_profile.name


def pytest_unconfigure(config):
    profile = getattr(config, "_pdf_test_profile", None)
    if profile is None:
        return
    previous = config._pdf_previous_profile
    if previous is None:
        os.environ.pop(_PROFILE_ENV, None)
    else:
        os.environ[_PROFILE_ENV] = previous
    profile.cleanup()
