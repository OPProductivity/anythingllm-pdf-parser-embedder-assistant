"""Fail-closed safety boundary inherited by isolated qualification workers."""
import os
import sys

try:
    from isolated_full_pdf_guard_runtime import install
    install()
except BaseException:
    # Python normally ignores sitecustomize failures. This harness must not.
    sys.stderr.write('Isolated full-PDF guard failed to initialize.\n')
    os._exit(92)
