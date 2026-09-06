"""Keep real runtime imports and distribution contents aligned."""
import ast
from pathlib import Path
import tomllib

import pytest

pytestmark = pytest.mark.offline_deterministic
ROOT = Path(__file__).resolve().parents[1]


def test_production_dependencies_match_runtime_lock():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    lock = {line.strip() for line in (ROOT / 'requirements.lock').read_text().splitlines() if line.strip() and not line.startswith('#')}
    assert set(project['project']['dependencies']) == lock
    assert {'psutil==7.2.2', 'fastapi==0.139.0', 'starlette==1.3.1', 'opencv-python==5.0.0.93'} <= lock


def test_all_statically_imported_local_runtime_modules_are_packaged():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    modules = set(project['tool']['setuptools']['py-modules'])
    local = {path.stem: path for path in ROOT.glob('*.py')}
    missing = set()
    for module in modules:
        tree = ast.parse(local[module].read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name.split('.')[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imported = [node.module.split('.')[0]]
            else:
                imported = []
            missing.update(name for name in imported if name in local and name not in modules)
    assert not missing, f'Runtime modules omitted from wheel: {sorted(missing)}'


def test_release_version_matches_ui_resource():
    assert tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version'] == (ROOT / 'VERSION').read_text().strip()
