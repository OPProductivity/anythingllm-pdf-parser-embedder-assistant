"""Process-local routing and lifecycle guard; never imported by production."""
import importlib.abc
import importlib.machinery
import ipaddress
import os
from pathlib import Path
import socket
import subprocess
import sys
import weakref
from urllib.parse import urlsplit


def install():
    api = os.environ['ISOLATED_FULL_PDF_API']
    root = Path(os.environ['ISOLATED_FULL_PDF_BASE']).resolve()
    parsed = urlsplit(api)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.port not in range(43100, 44000):
        raise RuntimeError('Invalid isolated endpoint')
    if not Path(os.environ['APPDATA']).resolve().is_relative_to(root):
        raise RuntimeError('APPDATA outside isolation root')
    if not Path(os.environ['ANYTHINGLLM_PDF_ASSISTANT_HOME']).resolve().is_relative_to(root):
        raise RuntimeError('Assistant home outside isolation root')
    allowed_ports = {parsed.port, int(os.environ['ISOLATED_FULL_PDF_COLLECTOR_PORT'])}
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    original_bind, original_listen = socket.socket.bind, socket.socket.listen
    ephemeral_bound = weakref.WeakKeyDictionary()
    owned_listeners = weakref.WeakKeyDictionary()

    def bind(sock, address):
        result = original_bind(sock, address)
        if isinstance(address, tuple) and int(address[1]) == 0:
            host = ipaddress.ip_address(sock.getsockname()[0].split('%')[0])
            if host.is_loopback:
                ephemeral_bound[sock] = sock.getsockname()[1]
        return result

    def listen(sock, backlog=128):
        result = original_listen(sock, backlog)
        if sock in ephemeral_bound:
            owned_listeners[sock] = ephemeral_bound[sock]
        return result

    socket.socket.bind, socket.socket.listen = bind, listen

    def check(address):
        if not isinstance(address, tuple):
            raise PermissionError('Non-IP sockets blocked by isolated harness')
        host, port = str(address[0]), int(address[1])
        addresses = original_getaddrinfo(host, port, type=socket.SOCK_STREAM)
        for item in addresses:
            resolved = ipaddress.ip_address(item[4][0].split('%')[0])
            if resolved.is_loopback:
                # Windows implements asyncio's socketpair with an ephemeral
                # loopback listener. Permit only this process's live listeners
                # originally bound to port zero, never an arbitrary local port.
                internal = any(listener.fileno() >= 0 and bound_port == port
                               for listener, bound_port in list(owned_listeners.items()))
                if port not in allowed_ports and not internal:
                    raise PermissionError('Non-isolated loopback endpoint blocked')
            elif port != 443:
                raise PermissionError('Only HTTPS is allowed outside the isolated loopback ports')

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    original_popen = subprocess.Popen

    class GuardedPopen(original_popen):
        def __init__(self, args, *positional, **kwargs):
            words = args if isinstance(args, (list, tuple)) else [args]
            command = ' '.join(str(word) for word in words).casefold()
            if any(token in command for token in ('anythingllm.exe', 'taskkill', 'stop-process', 'pkill')):
                raise PermissionError('Desktop lifecycle command blocked by isolated harness')
            supplied = kwargs.get('env')
            if supplied is not None:
                supplied = dict(supplied)
                for name in ('ISOLATED_FULL_PDF_API', 'ISOLATED_FULL_PDF_BASE',
                             'ISOLATED_FULL_PDF_COLLECTOR_PORT', 'APPDATA',
                             'ANYTHINGLLM_PDF_ASSISTANT_HOME', 'PYTHONPATH'):
                    supplied[name] = os.environ[name]
                kwargs['env'] = supplied
            super().__init__(args, *positional, **kwargs)

    subprocess.Popen = GuardedPopen

    def patch(module):
        module.DEFAULT_ANYTHINGLLM_API_URL = api
        if hasattr(module, 'ANYTHINGLLM_API_CANDIDATE_URLS'):
            module.ANYTHINGLLM_API_CANDIDATE_URLS = (api,)

        def urls(preferred_url=''):
            if preferred_url and str(preferred_url).rstrip('/') != api:
                raise PermissionError('Non-isolated API URL rejected')
            return [api]

        def lifecycle(*args, **kwargs):
            raise PermissionError('Desktop lifecycle operation disabled in isolated qualification')

        if hasattr(module, 'preferred_anythingllm_api_urls'):
            module.preferred_anythingllm_api_urls = urls
        for name in ('start_anythingllm_desktop', 'restart_anythingllm_desktop'):
            if hasattr(module, name):
                setattr(module, name, lifecycle)

    class Loader(importlib.abc.Loader):
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def create_module(self, spec):
            return self.wrapped.create_module(spec)

        def exec_module(self, module):
            self.wrapped.exec_module(module)
            patch(module)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname not in ('auto_anythingllm_pipeline', 'rag_pdf_gradio_app'):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is not None:
                spec.loader = Loader(spec.loader)
            return spec

    sys.meta_path.insert(0, Finder())
    os.environ['ISOLATED_FULL_PDF_GUARD_ACTIVE'] = '1'
