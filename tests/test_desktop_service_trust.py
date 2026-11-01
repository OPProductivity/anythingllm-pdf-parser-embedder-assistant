"""Socket-owner evidence for the local AnythingLLM Desktop API."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import desktop_service_trust as trust


pytestmark = pytest.mark.offline_deterministic


class ProbeSocket:
    def getsockname(self):
        return ("127.0.0.1", 51001)

    def getpeername(self):
        return ("127.0.0.1", 3001)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def connection(status, pid, local_port, remote_port=None):
    return SimpleNamespace(
        status=status,
        pid=pid,
        laddr=SimpleNamespace(ip="127.0.0.1", port=local_port),
        raddr=(SimpleNamespace(ip="127.0.0.1", port=remote_port)
               if remote_port is not None else ()),
    )


def desktop_process_fixture(monkeypatch, tmp_path, *, storage_override=None):
    executable = tmp_path / "Programs" / "AnythingLLM" / "AnythingLLM.exe"
    storage = tmp_path / "desktop-storage"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    class Process:
        def __init__(self, pid=None):
            self.pid = pid or 999

        def create_time(self):
            return 123.0

        def exe(self):
            return str(executable)

        def username(self):
            return "same-user"

        def environ(self):
            return {"SERVER_PORT": "3001", "STORAGE_DIR": str(storage_override or storage)}

    monkeypatch.setattr(trust.psutil, "Process", Process)
    return storage


def test_pid_zero_listener_uses_exact_owned_probe_connection(monkeypatch, tmp_path):
    storage = desktop_process_fixture(monkeypatch, tmp_path)
    observed = Mock(side_effect=[
        [connection(trust.psutil.CONN_LISTEN, 0, 3001)],
        [connection(trust.psutil.CONN_ESTABLISHED, 16304, 3001, 51001)],
    ])
    connect = Mock(return_value=ProbeSocket())
    monkeypatch.setattr(trust.psutil, "net_connections", observed)
    monkeypatch.setattr(trust.socket, "create_connection", connect)

    result = trust.desktop_listener("http://127.0.0.1:3001", storage)

    assert result == {"pid": 16304, "created": 123.0, "port": 3001, "storage": str(storage)}
    connect.assert_called_once_with(("127.0.0.1", 3001), timeout=0.5)


def test_listener_table_error_falls_back_to_exact_owned_probe(monkeypatch, tmp_path):
    storage = desktop_process_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(trust.socket, "create_connection", Mock(return_value=ProbeSocket()))
    monkeypatch.setattr(trust.psutil, "net_connections", Mock(side_effect=[
        RuntimeError("GetExtendedTcpTable failed"),
        [connection(trust.psutil.CONN_ESTABLISHED, 16304, 3001, 51001)],
    ]))

    assert trust.desktop_listener("http://127.0.0.1:3001", storage)["pid"] == 16304


def test_pid_zero_fallback_rejects_wrong_peer_or_storage(monkeypatch, tmp_path):
    storage = desktop_process_fixture(monkeypatch, tmp_path, storage_override=tmp_path / "other-storage")
    monkeypatch.setattr(trust.socket, "create_connection", Mock(return_value=ProbeSocket()))
    monkeypatch.setattr(trust.psutil, "net_connections", Mock(side_effect=[
        [connection(trust.psutil.CONN_LISTEN, 0, 3001)],
        *[[connection(trust.psutil.CONN_ESTABLISHED, 16304, 3001, 51001)] for _ in range(8)],
    ]))
    assert trust.desktop_listener("http://127.0.0.1:3001", storage) is None

    desktop_process_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(trust.psutil, "net_connections", Mock(side_effect=[
        [connection(trust.psutil.CONN_LISTEN, 0, 3001)],
        *[[connection(trust.psutil.CONN_ESTABLISHED, 16304, 3001, 51002)] for _ in range(8)],
    ]))
    assert trust.desktop_listener("http://127.0.0.1:3001", storage) is None


def test_owned_listener_keeps_the_direct_path(monkeypatch, tmp_path):
    storage = desktop_process_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(trust.psutil, "net_connections", lambda **_kw: [
        connection(trust.psutil.CONN_LISTEN, 16304, 3001),
    ])
    monkeypatch.setattr(trust.socket, "create_connection", Mock(side_effect=AssertionError("no probe needed")))

    assert trust.desktop_listener("http://127.0.0.1:3001", storage)["pid"] == 16304


def test_credential_socket_uses_its_own_owned_connection(monkeypatch, tmp_path):
    storage = desktop_process_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(trust.psutil, "net_connections", lambda **_kw: [
        connection(trust.psutil.CONN_ESTABLISHED, 16304, 3001, 51001),
    ])
    monkeypatch.setattr(trust.socket, "create_connection", Mock(side_effect=AssertionError("no extra probe")))
    trust.verify_connected_desktop_socket(ProbeSocket(), "http://127.0.0.1:3001", storage)

    monkeypatch.setattr(trust.psutil, "net_connections", lambda **_kw: [
        connection(trust.psutil.CONN_ESTABLISHED, 16304, 3001, 51002),
    ])
    with pytest.raises(ValueError, match="no credential was sent"):
        trust.verify_connected_desktop_socket(ProbeSocket(), "http://127.0.0.1:3001", storage)


def test_connected_socket_recovers_from_one_windows_table_failure(monkeypatch, tmp_path):
    storage = desktop_process_fixture(monkeypatch, tmp_path)
    observed = Mock(side_effect=[
        RuntimeError("GetExtendedTcpTable failed"),
        [connection(trust.psutil.CONN_ESTABLISHED, 16304, 3001, 51001)],
    ])
    monkeypatch.setattr(trust.psutil, "net_connections", observed)
    monkeypatch.setattr(trust.time, "sleep", lambda _seconds: None)

    trust.verify_connected_desktop_socket(ProbeSocket(), "http://127.0.0.1:3001", storage)

    assert observed.call_count == 2


def test_connected_socket_never_accepts_an_unowned_peer(monkeypatch, tmp_path):
    storage = desktop_process_fixture(monkeypatch, tmp_path, storage_override=tmp_path / "other-storage")
    observed = Mock(return_value=[connection(trust.psutil.CONN_ESTABLISHED, 16304, 3001, 51001)])
    monkeypatch.setattr(trust.psutil, "net_connections", observed)
    monkeypatch.setattr(trust.time, "sleep", lambda _seconds: None)

    with pytest.raises(trust.DesktopSocketIdentityError, match="no credential was sent"):
        trust.verify_connected_desktop_socket(ProbeSocket(), "http://127.0.0.1:3001", storage)

    assert observed.call_count == 3


def test_pre_send_reconnect_uses_a_new_verified_socket(monkeypatch, tmp_path):
    first, second = Mock(), Mock()
    connect = Mock(side_effect=[first, second])
    verify = Mock(side_effect=[trust.DesktopSocketIdentityError("unproved"), None])
    monkeypatch.setattr(trust, "verify_connected_desktop_socket", verify)

    result = trust.connect_verified_desktop_socket(
        connect, "http://127.0.0.1:3001", tmp_path, attempts=2
    )

    assert result is second
    first.close.assert_called_once_with()
    second.close.assert_not_called()
    assert connect.call_count == 2


def test_pre_send_reconnect_stays_closed_without_proof(monkeypatch, tmp_path):
    sockets = [Mock() for _ in range(3)]
    monkeypatch.setattr(
        trust, "verify_connected_desktop_socket",
        Mock(side_effect=trust.DesktopSocketIdentityError("unproved")),
    )

    with pytest.raises(trust.DesktopSocketIdentityError):
        trust.connect_verified_desktop_socket(
            Mock(side_effect=sockets), "http://127.0.0.1:3001", tmp_path, attempts=3
        )

    for sock in sockets:
        sock.close.assert_called_once_with()
