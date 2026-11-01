"""OS evidence binding an auto-discovered credential to its Desktop instance."""
import hashlib
import os
from pathlib import Path
import socket
import threading
import time
import urllib.parse

import psutil

_credentials = {}
_lock = threading.Lock()


class DesktopSocketIdentityError(ValueError):
    """The request's connected socket failed ownership proof before any send."""


def _loopback_port(api_url):
    parsed = urllib.parse.urlsplit(api_url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return None
    return parsed.port or 80


def _trusted_desktop_process(pid, storage, port):
    """Bind a socket owner to this user's installed Desktop and storage."""
    try:
        process = psutil.Process(pid)
        created = process.create_time()
        executable = Path(process.exe()).resolve()
        if process.username() != psutil.Process().username() or executable.name.casefold() != "anythingllm.exe":
            return None
        trusted_roots = [Path(value) / "AnythingLLM" for value in (
            os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")) if value]
        if os.environ.get("LOCALAPPDATA"):
            trusted_roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "AnythingLLM")
        if not any(executable.is_relative_to(root.resolve()) for root in trusted_roots):
            return None
        environment = process.environ()
        configured_storage = environment.get("STORAGE_DIR")
        if not configured_storage or Path(configured_storage).resolve() != storage:
            return None
        if int(environment.get("SERVER_PORT") or 0) != port:
            return None
        if psutil.Process(process.pid).create_time() != created:
            return None
        return {"pid": process.pid, "created": created, "port": port, "storage": str(storage)}
    except (OSError, ValueError, RuntimeError, psutil.Error):
        return None


def _connected_desktop_owner(sock, api_url, storage):
    """Verify the exact peer socket, including when LISTEN has owner PID 0."""
    port = _loopback_port(api_url)
    if port is None or sock is None:
        return None
    try:
        local, peer = sock.getsockname(), sock.getpeername()
        if peer[0] not in {"127.0.0.1", "::1"} or peer[1] != port:
            return None
        for connection in psutil.net_connections(kind="tcp"):
            if (connection.status == psutil.CONN_ESTABLISHED and connection.pid
                    and connection.laddr and connection.raddr
                    and connection.laddr.ip == peer[0] and connection.laddr.port == peer[1]
                    and connection.raddr.ip == local[0] and connection.raddr.port == local[1]):
                descriptor = _trusted_desktop_process(connection.pid, storage, port)
                if descriptor:
                    return descriptor
    except (OSError, ValueError, RuntimeError, psutil.Error):
        return None
    return None


def desktop_listener(api_url, storage_dir):
    """Fail closed: verify the listener or an exact owned probe connection."""
    try:
        port = _loopback_port(api_url)
        if port is None:
            return None
        storage = Path(storage_dir).resolve()
        try:
            listeners = psutil.net_connections(kind="tcp")
        except (OSError, ValueError, RuntimeError, psutil.Error):
            listeners = []
        for connection in listeners:
            if (connection.status != psutil.CONN_LISTEN or not connection.pid
                    or connection.laddr.port != port
                    or connection.laddr.ip not in {"127.0.0.1", "::1", "0.0.0.0", "::"}):
                continue
            descriptor = _trusted_desktop_process(connection.pid, storage, port)
            if descriptor:
                return descriptor
        # Windows can report a live Electron listener with PID 0. A bare
        # loopback response is insufficient; prove the owner of this socket.
        for _ in range(8):
            with socket.create_connection((urllib.parse.urlsplit(api_url).hostname, port), timeout=0.5) as probe:
                descriptor = _connected_desktop_owner(probe, api_url, storage)
                if descriptor:
                    return descriptor
    except (OSError, ValueError, RuntimeError, psutil.Error):
        pass
    return None


def register_managed_key(key, storage_dir):
    with _lock:
        _credentials[hashlib.sha256(key.encode()).digest()] = str(Path(storage_dir).resolve())


def managed_key_storage(key):
    with _lock:
        return _credentials.get(hashlib.sha256(key.encode()).digest())


def require_managed_key_origin(api_url, key):
    storage = managed_key_storage(key)
    if storage and not desktop_listener(api_url, storage):
        raise ValueError("Managed Desktop API key target has no matching owned Desktop listener/storage identity; no credential was sent.")
    return storage


def verify_connected_desktop_socket(sock, api_url, storage):
    """Bind trust to the established connection before any bearer is sent."""
    expected_storage = Path(storage).resolve()
    # Windows may omit a just-established connection from one table snapshot.
    # Keep the socket open and require positive ownership proof before sending.
    for attempt in range(3):
        if _connected_desktop_owner(sock, api_url, expected_storage):
            return
        if attempt < 2:
            time.sleep(0.05)
    raise DesktopSocketIdentityError(
        "Connected API socket is not owned by the expected Desktop instance; no credential was sent."
    )


def connect_verified_desktop_socket(connect, api_url, storage, *, attempts=8):
    """Retry only fresh, untrusted connections; never replay a sent request."""
    for attempt in range(attempts):
        sock = connect()
        try:
            verify_connected_desktop_socket(sock, api_url, storage)
            return sock
        except DesktopSocketIdentityError:
            sock.close()
            if attempt + 1 >= attempts:
                raise
        except BaseException:
            sock.close()
            raise
    raise AssertionError("unreachable")


def managed_async_transport(api_url, key):
    """HTTPX transport using httpcore's public network-backend interface."""
    storage = managed_key_storage(key)
    if not storage:
        return None
    import asyncio
    import httpcore
    import httpx

    async def verify_socket(sock):
        loop = asyncio.get_running_loop()
        completion = loop.create_future()
        def check():
            try:
                verify_connected_desktop_socket(sock, api_url, storage)
                error = None
            except Exception as exception:
                error = exception
            def finish():
                if not completion.done():
                    if error is not None:
                        completion.set_exception(error)
                    else:
                        completion.set_result(None)
            try:
                loop.call_soon_threadsafe(finish)
            except RuntimeError:
                pass
        # A stalled OS identity probe must not make asyncio.run wait for a
        # default executor during SSE cancellation or shutdown.
        threading.Thread(target=check, daemon=True, name="desktop-socket-identity").start()
        await asyncio.wait_for(completion, timeout=5)

    class Backend(httpcore.AsyncNetworkBackend):
        def __init__(self):
            self.delegate = httpcore.AnyIOBackend()

        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            stream = await self.delegate.connect_tcp(host, port, timeout, local_address, socket_options)
            try:
                await verify_socket(stream.get_extra_info("socket"))
            except BaseException:
                await stream.aclose()
                raise
            return stream

        async def sleep(self, seconds):
            await self.delegate.sleep(seconds)

    class ResponseStream(httpx.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        async def __aiter__(self):
            async for chunk in self.stream:
                yield chunk

        async def aclose(self):
            await self.stream.aclose()

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.pool = httpcore.AsyncConnectionPool(network_backend=Backend())

        async def handle_async_request(self, request):
            response = await self.pool.handle_async_request(httpcore.Request(
                method=request.method, url=httpcore.URL(scheme=request.url.raw_scheme,
                    host=request.url.raw_host, port=request.url.port, target=request.url.raw_path),
                headers=request.headers.raw, content=request.stream, extensions=request.extensions))
            return httpx.Response(response.status, headers=response.headers,
                                  stream=ResponseStream(response.stream), extensions=response.extensions)

        async def aclose(self):
            await self.pool.aclose()

    return Transport()
