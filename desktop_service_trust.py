"""OS evidence binding an auto-discovered credential to its Desktop instance."""
import hashlib
import os
from pathlib import Path
import threading
import urllib.parse

import psutil

_credentials = {}
_lock = threading.Lock()


def desktop_listener(api_url, storage_dir):
    """Fail closed: a ping contract or loopback hostname is not identity."""
    try:
        parsed = urllib.parse.urlsplit(api_url)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return None
        port = parsed.port or 80
        storage = Path(storage_dir).resolve()
        username = psutil.Process().username()
        for connection in psutil.net_connections(kind="tcp"):
            if (connection.status != psutil.CONN_LISTEN or not connection.pid
                    or connection.laddr.port != port
                    or connection.laddr.ip not in {"127.0.0.1", "::1", "0.0.0.0", "::"}):
                continue
            process = psutil.Process(connection.pid)
            created = process.create_time()
            executable = Path(process.exe()).resolve()
            if process.username() != username or executable.name.casefold() != "anythingllm.exe":
                continue
            trusted_roots = [Path(value) / "AnythingLLM" for value in (
                os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")) if value]
            if os.environ.get("LOCALAPPDATA"):
                trusted_roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "AnythingLLM")
            if not any(executable.is_relative_to(root.resolve()) for root in trusted_roots):
                continue
            environment = process.environ()
            configured_storage = environment.get("STORAGE_DIR")
            if not configured_storage or Path(configured_storage).resolve() != storage:
                continue
            if int(environment.get("SERVER_PORT") or 0) != port:
                continue
            if psutil.Process(process.pid).create_time() != created:
                continue
            return {"pid": process.pid, "created": created, "port": port, "storage": str(storage)}
    except (OSError, ValueError, psutil.Error):
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
    descriptor = desktop_listener(api_url, storage)
    if descriptor and sock is not None:
        try:
            local, peer = sock.getsockname(), sock.getpeername()
            process = psutil.Process(descriptor["pid"])
            if process.create_time() == descriptor["created"]:
                for connection in process.net_connections(kind="tcp"):
                    if (connection.status == psutil.CONN_ESTABLISHED and connection.raddr
                            and connection.laddr.ip == peer[0] and connection.laddr.port == peer[1]
                            and connection.raddr.ip == local[0] and connection.raddr.port == local[1]):
                        return
        except (OSError, psutil.Error):
            pass
    raise ValueError("Connected API socket is not owned by the expected Desktop instance; no credential was sent.")


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
