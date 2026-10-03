"""Prefer IPv4, retain address errors, and cache this process's official DNS answers.

HTTPS keeps the original hostname for SNI and certificate verification. No proxy,
fixed IP, alternate DNS provider, or disk-backed address cache is introduced.
"""

from __future__ import annotations

import errno
import http.client
import socket
import threading
import time
import urllib.request

from campus import official_url

_CACHE = {}
_LOCKS = {}
_GUARD = threading.Lock()
DNS_TTL_SECONDS = 60


def clear_dns_cache():
    with _GUARD:
        _CACHE.clear()
        _LOCKS.clear()


def resolve_addresses(host, port, family):
    key = (host, port, family)
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] < DNS_TTL_SECONDS:
            return cached[1]
        # Only positive answers are cached. Recovery can retry a DNS failure.
        answers = tuple(dict.fromkeys(socket.getaddrinfo(
            host, port, family, socket.SOCK_STREAM, socket.IPPROTO_TCP
        )))
        _CACHE[key] = (time.monotonic(), answers)
        return answers


def connection_code(exc):
    code = type(exc).__name__
    number = getattr(exc, "errno", None)
    if number is not None:
        if isinstance(exc, socket.gaierror):
            names = {getattr(socket, k): k for k in dir(socket) if k.startswith("EAI_")}
        else:
            names = errno.errorcode
        code += ":" + names.get(number, str(number))
    return code


class ConnectionAttemptsError(OSError):
    def __init__(self, errors, attempts):
        self.errors = tuple(errors)
        self.attempts = tuple(attempts)
        super().__init__("official connection attempts failed")


def connect_official(address, timeout=20, source_address=None):
    host, port = address
    if not official_url(f"http://{host}:{port}/"):
        raise ValueError("连接目标不属于官方来源")
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT or timeout is None:
        timeout = 20
    deadline = time.monotonic() + timeout
    errors, attempts = [], []
    # Request A answers first, without waiting on an unused AAAA lookup. IPv6
    # remains a fallback when IPv4 cannot connect and the time budget allows it.
    for family, label in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        if time.monotonic() >= deadline:
            break
        try:
            answers = resolve_addresses(host, port, family)
        except OSError as exc:
            errors.append(exc)
            attempts.append({"family": label, "stage": "dns", "error": connection_code(exc)})
            continue
        for af, kind, protocol, _, endpoint in answers[:8]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock = None
            try:
                sock = socket.socket(af, kind, protocol)
                sock.settimeout(remaining)
                if source_address:
                    sock.bind(source_address)
                sock.connect(endpoint)
                # The caller's original read/TLS timeout remains in force.
                sock.settimeout(timeout)
                return sock
            except OSError as exc:
                if sock is not None:
                    sock.close()
                errors.append(exc)
                attempts.append({"family": label, "stage": "tcp", "error": connection_code(exc)})
    if not errors:
        exc = TimeoutError()
        errors.append(exc)
        attempts.append({"family": "unresolved", "stage": "deadline", "error": connection_code(exc)})
    raise ConnectionAttemptsError(errors, attempts)


class OfficialHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_official


class OfficialHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_official


class OfficialHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(OfficialHTTPConnection, request)


class OfficialHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(OfficialHTTPSConnection, request, context=self._context)
