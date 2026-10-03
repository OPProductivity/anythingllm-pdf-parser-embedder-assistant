# Verified offline releases

The Windows installers no longer install from a mutable source URL or resolve
dependencies at installation time. Supply a release ZIP and its SHA-256 from an
independently trusted publisher. A hash supplied by the same untrusted source
does not establish publisher authenticity.

## Assistant release

1. Build the reviewed assistant wheel using a trusted build environment.
2. Resolve the full pinned dependency graph for the intended Windows x64 Python
   minor version into an audited wheelhouse. Include the assistant wheel, every
   transitive dependency and required extras. Review licenses and vulnerability
   results. Package resolution is a publisher operation, not an installer step.
3. Package the wheelhouse with the repository tool:

```powershell
python scripts/build_verified_release.py --source C:\release\wheelhouse --output C:\release\assistant.zip --kind pdf-assistant-wheels --python-version 3.14
```

The tool prints the archive SHA-256. It adds per-member hashes and a local-wheel
requirements lock. It refuses to overwrite an existing bundle. It does not
download or execute dependencies and does not certify dependency completeness.

4. First verify without executing release code:

```powershell
.\Install-AnythingLLMPdfAssistant.ps1 -BundlePath C:\release\assistant.zip -BundleSha256 <trusted-sha256> -VerifyOnly
```

5. Qualify installation in a disposable Windows environment using the same
   command without `-VerifyOnly`. Installation uses offline hash-required pip
   and checks the installed dependency graph before creating shortcuts. The
   existing research installation must not be used as a release test target.
6. Publish only that qualified immutable bundle, together with its independently
   verifiable hash. An HTTPS `-BundleUrl` is supported instead of `-BundlePath`.

Existing release runtimes are not overwritten. Failed installs may leave an
incomplete versioned runtime for inspection; inspect it before explicit removal.
The old pipx/source installation instructions are not a substitute for this
verified release path. Development from a trusted checkout remains separate.

## Optional Desktop bridge tool

Prepare and review a pinned npm tree including `package-lock.json` and all
`@electron/asar` dependencies. Package it using `--kind asar-tool`, then pass
`-AsarToolBundle` and `-AsarToolSha256` to the Desktop bridge installer (or the
corresponding CLI options). Node must satisfy the bundle's minimum version.
No network package execution occurs during bridge installation. Do not test
bridge modification against a Desktop instance doing important research work.

Release ZIPs are publisher artifacts, not ordinary run logs or output exports.
