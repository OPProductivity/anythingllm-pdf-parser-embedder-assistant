"""Package an audited wheelhouse or ASAR tool tree into an immutable release.

This does not download or execute dependencies. Resolution/review happens
before packaging; installation verifies the externally supplied bundle hash.
"""
import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import sys
import zipfile


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def build_release(source, destination, kind, python_version=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.is_relative_to(source):
        raise ValueError("Release output must be outside the input tree.")
    files = []
    names = set()
    requirements = []
    if kind == "pdf-assistant-wheels":
        for path in sorted(source.glob("*.whl")):
            if path.is_symlink():
                raise ValueError("Release wheel inputs cannot be links.")
            with zipfile.ZipFile(path) as wheel:
                metadata_paths = [n for n in wheel.namelist() if n.endswith(".dist-info/METADATA")]
                if len(metadata_paths) != 1:
                    raise ValueError(f"Invalid wheel metadata: {path.name}")
                metadata = BytesParser().parsebytes(wheel.read(metadata_paths[0]))
                name = metadata["Name"].lower().replace("_", "-")
                if name in names:
                    raise ValueError(f"Multiple versions of {name} in release wheelhouse.")
                names.add(name)
            sha = digest(path)
            relative = "wheels/" + path.name
            files.append((path, relative, sha))
            requirements.append(f"./{relative} --hash=sha256:{sha}")
        if "anythingllm-pdf-assistant" not in names:
            raise ValueError("The audited assistant wheel must be included.")
        if not python_version:
            raise ValueError("Specify the Python minor version qualified for this wheelhouse.")
        manifest = {"schema_version": 1, "kind": kind, "python_version": python_version,
                    "platform": "win_amd64"}
        lock = ("\n".join(requirements) + "\n").encode()
    else:
        package = source / "node_modules" / "@electron" / "asar" / "package.json"
        metadata = json.loads(package.read_text(encoding="utf-8"))
        if metadata.get("name") != "@electron/asar":
            raise ValueError("Not an Electron ASAR tool tree.")
        bin_path = metadata["bin"]["asar"]
        entrypoint = package.parent / bin_path
        if not entrypoint.resolve().is_relative_to(source) or not entrypoint.is_file():
            raise ValueError("Invalid ASAR entrypoint.")
        if not (source / "package-lock.json").is_file():
            raise ValueError("A complete npm integrity lockfile is required.")
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError("Release tool inputs cannot be links.")
            if path.is_file():
                files.append((path, path.relative_to(source).as_posix(), digest(path)))
        manifest = {"schema_version": 1, "kind": "asar-tool", "asar_version": metadata["version"],
                    "entrypoint": entrypoint.relative_to(source).as_posix(), "node_minimum_version": "22.12.0"}
        lock = None
    manifest["files"] = [{"path": relative, "sha256": sha} for _, relative, sha in files]
    if lock is not None:
        manifest["files"].append({"path": "requirements-release.lock", "sha256": hashlib.sha256(lock).hexdigest()})
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally replacing a published release.
    with destination.open("xb") as output, zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as bundle:
        for path, relative, sha in files:
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha:
                raise ValueError(f"Release input changed during packaging: {relative}")
            bundle.writestr(relative, data)
        if lock is not None:
            bundle.writestr("requirements-release.lock", lock)
        bundle.writestr("release-manifest.json", json.dumps(manifest, separators=(",", ":")))
    return digest(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--kind", choices=["pdf-assistant-wheels", "asar-tool"], required=True)
    parser.add_argument("--python-version", choices=["3.11", "3.12", "3.13", "3.14"])
    args = parser.parse_args()
    print(build_release(args.source, args.output, args.kind, args.python_version))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        print(f"Release packaging failed: {error}", file=sys.stderr)
        raise SystemExit(1)
