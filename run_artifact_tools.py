"""Explicit, local-only rendering of optional artifacts from retained run evidence."""

import argparse
from pathlib import Path, PureWindowsPath

from canonical_artifacts import CanonicalArtifacts, checked_role_path
from run_evidence import read_run_json


def evidence_path(root, role):
    root = Path(root).resolve(strict=True)
    index_path = root / 'artifact-locations.json'
    index = read_run_json(index_path) if index_path.is_file() else {}
    roles = {PureWindowsPath(key).as_posix(): value for key, value in index.get('roles', {}).items()}
    relative = PureWindowsPath(roles.get(PureWindowsPath(role).as_posix(), role))
    if relative.anchor:
        raise ValueError(f'Unsafe run artifact: {role}')
    path = checked_role_path(root, Path(*relative.parts))
    if not path.is_file():
        raise FileNotFoundError(f'Missing run artifact: {role}')
    return path


def read_rows(path):
    from manifest_text import read_manifest_rows

    return read_manifest_rows(path)


def artifact_catalog(root, writer):
    """Reuse only text that the diagnostic bundle can also transport."""
    index_path = root / 'artifact-locations.json'
    index = read_run_json(index_path) if index_path.is_file() else {}
    existing = [evidence_path(root, role) for role, actual in index.get('roles', {}).items()
                if PureWindowsPath(actual).suffix == '.txt']
    existing.extend((root / 'on-demand').rglob('*.txt'))
    return CanonicalArtifacts(root, writer, dict.fromkeys(existing))


def materialize_optional_artifacts(run_directory, kind='diagnostic-text'):
    """Never uploads, embeds, prunes, or changes the original run summary/plans."""
    import auto_anythingllm_pipeline as pipeline

    root = Path(run_directory).resolve(strict=True)
    output = root / 'on-demand' / kind
    if (output.is_symlink() or output.parent.is_symlink()
            or not output.resolve().is_relative_to(root)):
        raise ValueError('Unsafe on-demand output directory')
    # Validate inputs before writing any output, including relocated bundles.
    if kind == 'diagnostic-text':
        transitions = read_rows(evidence_path(root, 'page-transition-manifest.jsonl'))
        review = read_run_json(evidence_path(root, 'retrieval-lane-review.json'))
        companions = []
        for row in transitions:
            if not row.get('continuation_detected'):
                continue
            name = str(row['boundary_id'])
            if Path(name).name != name or any(char in name for char in '/\\:') or name in ('.', '..'):
                raise ValueError('Unsafe transition identifier')
            path = output / 'page-transition-companions' / (name + '.txt')
            if path.is_symlink() or not path.resolve().is_relative_to(output.resolve()):
                raise ValueError('Unsafe companion output path')
            companions.append((path, row['reconstructed_text']))
        supplementary_path = output / 'supplementary-content-candidates.txt'
        if supplementary_path.is_symlink() or not supplementary_path.resolve().is_relative_to(output.resolve()):
            raise ValueError('Unsafe supplementary output path')
        output.mkdir(parents=True, exist_ok=True)
        paths = []
        for path, text in companions:
            pipeline.atomic_write_text(path, text)
            paths.append(path)
        supplementary = pipeline.write_supplementary_lane_candidate_text(
            supplementary_path, review)
        if supplementary:
            paths.append(supplementary)
        return paths
    manifest = evidence_path(root, 'segment-manifest.jsonl')
    segments = read_rows(manifest)
    if not segments:
        raise ValueError('Empty retained segment manifest')
    if kind == 'manual-kits':
        summary = read_run_json(root / 'run-summary.json')
        catalog = artifact_catalog(root, pipeline.atomic_write_text)
        probe = [segments[0], segments[len(segments) // 2]] if len(segments) > 2 else segments
        paths = []
        for name, rows in (('native-metadata-test-kit', segments), ('native-metadata-compatibility-probe', probe)):
            kit = pipeline.write_native_metadata_test_kit(
                rows, output / name, workspace_slug=summary.get('workspace_slug') or 'test', artifact_catalog=catalog)
            paths.extend([Path(kit['upload_plan']), Path(kit['checklist'])])
            paths.extend(Path(row['text_file']) for row in pipeline.load_upload_plan_rows(kit['upload_plan']))
        return list(dict.fromkeys(paths))
    if kind == 'upload-alternatives':
        # Reuse the original payload generators, including publication metadata.
        parents = pipeline.build_page_parent_rows(segments)
        catalog = artifact_catalog(root, pipeline.atomic_write_text)
        paths = []
        for representation in ('segments', 'page-parents'):
            for mode in ('strict', 'native_header'):
                payloads = (pipeline.generate_api_payloads(segments, mode) if representation == 'segments'
                            else pipeline.generate_page_parent_payloads(parents, mode))
                rows = pipeline.build_file_upload_rows_from_payloads(
                    payloads, output / f'{representation}-{mode}', artifact_catalog=catalog)
                path = output / f'upload-plan-{representation}-{mode}.csv'
                pipeline.write_csv(path, rows)
                paths.append(path)
                paths.extend(Path(row['text_file']) for row in rows)
        return list(dict.fromkeys(paths))
    raise ValueError(f'Unknown optional artifact kind: {kind}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_directory', help='Per-document private directory containing run-summary.json')
    parser.add_argument('--kind', choices=('diagnostic-text', 'manual-kits', 'upload-alternatives'),
                        default='diagnostic-text')
    args = parser.parse_args()
    for path in materialize_optional_artifacts(args.run_directory, args.kind):
        print(path)


if __name__ == '__main__':
    main()
