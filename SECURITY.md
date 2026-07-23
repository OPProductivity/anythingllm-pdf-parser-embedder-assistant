# Security policy

## Reporting a vulnerability

Do **not** open a public issue for a vulnerability, API key, token, private PDF,
AnythingLLM storage archive, or desktop backup. Use GitHub's private security
advisory feature for this repository, or contact the maintainer through the
repository's private contact method when one is configured.

## Scope and operating assumptions

This assistant runs on the user's machine and can prepare local PDF text,
communicate with a local AnythingLLM Desktop instance, and optionally install a
desktop refresh bridge. Review the code and backups before enabling the bridge.
Provider traffic is governed by the embedding provider configured inside
AnythingLLM; users are responsible for their own provider, retention, and data
handling choices.
