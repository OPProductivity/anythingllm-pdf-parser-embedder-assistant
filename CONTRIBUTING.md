# Contributing

Thanks for considering an improvement.

## Before opening an issue

- Reproduce the behavior with a small, non-sensitive PDF when possible.
- Do not attach PDFs that contain private, copyrighted, or confidential text.
- Include the app version, AnythingLLM Desktop version, operating system,
  segmentation mode, output mode, and the concise error code or run summary.
- Remove API keys, tokens, local paths, workspace names, and personal details
  from screenshots and logs.

## Development workflow

1. Create a branch from `main`.
2. Keep changes scoped and add/adjust tests for changed behavior.
3. Run `py -m pytest -q` and `py -m pre_commit run --all-files`.
4. Explain user-visible behavior, limitations, and validation evidence in the
   pull request.

## Principles

- Preserve clear source-page provenance when a mode promises it.
- Do not claim upload, vector storage, or retrieval success without evidence.
- Keep provider credentials outside the repository and outside issue reports.
- Prefer a safe, explicit warning over silent fallback behavior.
