from concurrent.futures import ThreadPoolExecutor
import copy
import json
import shutil
import errno

import pytest

import run_control
import run_evidence as evidence


pytestmark = pytest.mark.offline_deterministic


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv('ANYTHINGLLM_PDF_ASSISTANT_HOME', str(tmp_path))
    return tmp_path / 'run-state' / 'automatic-runs' / 'r-test'


def snapshot():
    return {'runtime': 'unique evidence ' * 1000, 'version': 17}


def test_snapshot_written_once_across_workers_and_checkpoint_roles(root):
    value = snapshot()
    payload = {'status': 'ready', 'compatibility': value, 'resolved_state': value}
    before = copy.deepcopy(payload)
    paths = [root / str(index) / name for index in range(5)
             for name in ['run-summary.json', 'run-checkpoint.json', 'run-result.json']]
    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(lambda path: run_control.atomic_write_json(path, payload), paths))
    assert payload == before
    assert len(list((root / evidence.DIRECTORY).glob('*.json'))) == 1
    assert not list(root.rglob('*.tmp'))
    for path in paths:
        stored = json.loads(path.read_text(encoding='utf8'))
        assert stored['status'] == 'ready'
        assert stored['compatibility'] == stored['resolved_state']
        assert evidence.read_run_json(path) == payload
    restored = evidence.read_run_json(paths[0])
    restored['compatibility']['runtime'] = 'changed'
    assert restored['resolved_state'] == value


def test_changed_snapshot_gets_new_identity_and_old_one_survives(root):
    path = root / 'paper' / 'run-summary.json'
    run_control.atomic_write_json(path, {'compatibility': snapshot()})
    old_path = next((root / evidence.DIRECTORY).glob('*.json'))
    old = old_path.read_bytes()
    updated = {**snapshot(), 'version': 18}
    run_control.atomic_write_json(path, {'compatibility': updated})
    assert len(list((root / evidence.DIRECTORY).glob('*.json'))) == 2
    assert old_path.read_bytes() == old
    assert evidence.read_run_json(path)['compatibility'] == updated


def test_complete_run_can_be_copied_and_legacy_json_still_reads(root, tmp_path):
    path = root / 'paper' / 'run-summary.json'
    payload = {'resolved_state': snapshot()}
    run_control.atomic_write_json(path, payload)
    moved = tmp_path / 'copied-run'
    shutil.copytree(root, moved)
    assert evidence.read_run_json(moved / 'paper' / path.name) == payload
    legacy = tmp_path / 'legacy.json'
    run_control.atomic_write_json(legacy, payload)
    assert json.loads(legacy.read_text(encoding='utf8')) == payload
    assert evidence.read_run_json(legacy) == payload


@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'unsupported', 'escape'])
def test_incomplete_evidence_never_becomes_default_settings(root, failure):
    path = root / 'paper' / 'run-summary.json'
    run_control.atomic_write_json(path, {'compatibility': snapshot()})
    stored = json.loads(path.read_text(encoding='utf8'))
    target = next((root / evidence.DIRECTORY).glob('*.json'))
    if failure == 'missing':
        target.unlink()
    elif failure == 'corrupt':
        target.write_text('{}', encoding='utf8')
    elif failure == 'unsupported':
        stored['compatibility']['$run_evidence'] = 2
        path.write_text(json.dumps(stored), encoding='utf8')
    else:
        stored['compatibility']['sha256'] = '../escape'
        path.write_text(json.dumps(stored), encoding='utf8')
    with pytest.raises(evidence.RunEvidenceError):
        evidence.read_run_json(path)


def test_native_payloads_and_small_records_stay_self_contained(root):
    value = {'resolved_state': snapshot()}
    path = root / 'paper' / 'upload-payload.json'
    assert evidence.prepare_private_json(path, value) is value
    small = {'compatibility': {'status': 'ready'}}
    assert evidence.prepare_private_json(root / 'run-summary.json', small) == small
    assert not (root / evidence.DIRECTORY).exists()


def test_portable_filesystem_without_hard_links_preserves_evidence(root, monkeypatch):
    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, 'hard links unavailable')

    monkeypatch.setattr(evidence.os, 'link', unsupported)
    payload = {'compatibility': snapshot()}
    path = root / 'paper' / 'run-summary.json'
    run_control.atomic_write_json(path, payload)
    assert evidence.read_run_json(path) == payload
