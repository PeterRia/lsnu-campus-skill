#!/usr/bin/env python3
"""Bounded public-entry network diagnostics; standard library, no page bodies.

Paths supplied to --sources and --output are relative to this script's directory.
When copied into project/scripts, supply --sources ../kb/public-sources.json.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import errno
import ipaddress
import json
from pathlib import Path
import platform
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit


SCRIPT_DIR = Path(__file__).resolve().parent
ALLOWED_URLS = {
    "https://jiaowc.lsnu.edu.cn/",
    "https://xuesc.lsnu.edu.cn/",
    "http://libnew.lsnu.edu.cn/",
}
DNS_TIMEOUT = 5
PROBE_TIMEOUT = 12
SOCKET_TIMEOUT = 4
MAX_ADDRESSES_PER_FAMILY = 2
MAX_HEADER_BYTES = 16384


def exception_details(exc: BaseException) -> dict:
    data = {"type": type(exc).__name__}
    number = getattr(exc, "errno", None)
    if number is not None:
        data["errno"] = number
        if isinstance(exc, socket.gaierror):
            names = [name for name in dir(socket)
                     if name.startswith("EAI_") and getattr(socket, name) == number]
            data["code"] = names[0] if names else str(number)
        else:
            data["code"] = errno.errorcode.get(number, str(number))
    if isinstance(exc, ssl.SSLCertVerificationError):
        data["verify_code"] = exc.verify_code
        data["verify_message"] = exc.verify_message
    return data


def resolve_worker(host: str, port: int, family_name: str) -> dict:
    family = socket.AF_INET if family_name == "A" else socket.AF_INET6
    start = time.monotonic()
    try:
        answers = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM,
                                     socket.IPPROTO_TCP)
        addresses = []
        for answer in answers:
            address = answer[4][0]
            if address not in addresses:
                addresses.append(address)
        return {"status": "ok", "addresses": addresses,
                "elapsed_seconds": round(time.monotonic() - start, 3)}
    except OSError as exc:
        return {"status": "error", "error": exception_details(exc),
                "elapsed_seconds": round(time.monotonic() - start, 3)}


def probe_worker(url: str, address: str) -> dict:
    """Connect only to a supplied numeric address; read HEAD headers only."""
    parsed = urlsplit(url)
    host = parsed.hostname
    port = 443 if parsed.scheme == "https" else 80
    numeric = ipaddress.ip_address(address)
    family = socket.AF_INET if numeric.version == 4 else socket.AF_INET6
    result = {"address": address, "family": "IPv4" if numeric.version == 4 else "IPv6"}
    start = time.monotonic()
    deadline = start + PROBE_TIMEOUT - 1
    sock = None
    stage = "tcp"
    try:
        sock = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        sock.settimeout(SOCKET_TIMEOUT)
        connect_start = time.monotonic()
        sock.connect((address, port) if family == socket.AF_INET else (address, port, 0, 0))
        result["tcp"] = {"status": "ok", "elapsed_seconds":
                         round(time.monotonic() - connect_start, 3)}
        if parsed.scheme == "https":
            stage = "tls"
            context = ssl.create_default_context()
            tls_start = time.monotonic()
            sock.settimeout(min(SOCKET_TIMEOUT, max(0.1, deadline - time.monotonic())))
            sock = context.wrap_socket(sock, server_hostname=host)
            result["tls"] = {"status": "ok", "certificate_verified": True,
                             "hostname_verified": True, "sni_hostname": host,
                             "version": sock.version(),
                             "elapsed_seconds": round(time.monotonic() - tls_start, 3)}
        else:
            result["tls"] = {"status": "not_applicable"}
        stage = "http"
        request = (f"HEAD / HTTP/1.1\r\nHost: {host}\r\n"
                   "User-Agent: lsnu-campus-skill-network-diagnostic/1\r\n"
                   "Accept: */*\r\nConnection: close\r\n\r\n").encode("ascii")
        sock.settimeout(min(SOCKET_TIMEOUT, max(0.1, deadline - time.monotonic())))
        sock.sendall(request)
        headers = bytearray()
        # Read one byte at a time so no response body is read, including when a
        # server incorrectly sends one in response to HEAD.
        while not headers.endswith(b"\r\n\r\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("header deadline")
            sock.settimeout(min(SOCKET_TIMEOUT, remaining))
            byte = sock.recv(1)
            if not byte:
                raise ConnectionError("incomplete response headers")
            headers.extend(byte)
            if len(headers) > MAX_HEADER_BYTES:
                raise ValueError("response headers exceed limit")
        first_line = bytes(headers).split(b"\r\n", 1)[0].decode("ascii", "replace")
        parts = first_line.split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
            raise ValueError("invalid HTTP status")
        status = int(parts[1])
        if not 100 <= status <= 599:
            raise ValueError("invalid HTTP status")
        result["http"] = {"status": "response_received", "method": "HEAD",
                          "status_code": status, "redirect_followed": False,
                          "header_bytes": len(headers), "body_bytes_read": 0}
    except (OSError, ValueError, ssl.SSLError) as exc:
        result[stage] = {"status": "error", "error": exception_details(exc)}
    finally:
        if sock is not None:
            sock.close()
    result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    return result


def bounded_worker(arguments: list[str], timeout: int) -> dict:
    start = time.monotonic()
    try:
        completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), *arguments],
                                   capture_output=True, text=True, check=False,
                                   timeout=timeout)
        if completed.returncode != 0:
            return {"status": "diagnostic_error", "worker_exit_code": completed.returncode,
                    "elapsed_seconds": round(time.monotonic() - start, 3)}
        return json.loads(completed.stdout)
    except subprocess.TimeoutExpired:
        return {"status": "deadline_exceeded", "limit_seconds": timeout,
                "elapsed_seconds": round(time.monotonic() - start, 3)}
    except (OSError, ValueError) as exc:
        return {"status": "diagnostic_error", "error": exception_details(exc),
                "elapsed_seconds": round(time.monotonic() - start, 3)}


def diagnose_source(feed: dict) -> dict:
    url = feed["url"]
    parsed = urlsplit(url)
    port = 443 if parsed.scheme == "https" else 80
    result = {"name": feed["name"], "url": url, "dns": {}, "probes": []}
    start = time.monotonic()
    for family in ("A", "AAAA"):
        dns = bounded_worker(["--_resolve", parsed.hostname, str(port), family], DNS_TIMEOUT)
        result["dns"][family] = dns
        addresses = dns.get("addresses", [])
        dns["probe_limit"] = MAX_ADDRESSES_PER_FAMILY
        dns["unprobed_addresses"] = addresses[MAX_ADDRESSES_PER_FAMILY:]
        for address in addresses[:MAX_ADDRESSES_PER_FAMILY]:
            probe = bounded_worker(["--_probe", url, address], PROBE_TIMEOUT)
            probe.setdefault("address", address)
            probe.setdefault("family", "IPv4" if family == "A" else "IPv6")
            result["probes"].append(probe)
    result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    return result


def script_relative(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (SCRIPT_DIR / path).resolve()


def main() -> int:
    # Private subprocess protocol. Validate targets here as well, so calling
    # worker options directly cannot turn this into a general network probe.
    if len(sys.argv) > 1 and sys.argv[1] == "--_resolve":
        if len(sys.argv) != 5:
            return 2
        host, port_text, family = sys.argv[2:]
        allowed = {(urlsplit(url).hostname, "443" if url.startswith("https:") else "80")
                   for url in ALLOWED_URLS}
        if (host, port_text) not in allowed or family not in {"A", "AAAA"}:
            return 2
        print(json.dumps(resolve_worker(host, int(port_text), family)))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--_probe":
        if len(sys.argv) != 4 or sys.argv[2] not in ALLOWED_URLS:
            return 2
        try:
            address = ipaddress.ip_address(sys.argv[3])
        except ValueError:
            return 2
        # Refuse local and special addresses even if a resolver returned them.
        if not address.is_global:
            return 2
        print(json.dumps(probe_worker(sys.argv[2], str(address))))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default="../project/kb/public-sources.json",
                        help="JSON source list, relative to this script")
    parser.add_argument("--output", default="../evidence/network-local.json",
                        help="JSON report, relative to this script")
    args = parser.parse_args()
    source_path = script_relative(args.sources)
    if source_path.stat().st_size > 32768:
        parser.error("sources file exceeds 32 KiB")
    config = json.loads(source_path.read_text(encoding="utf-8"))
    feeds = config.get("feeds")
    if (not isinstance(feeds, list) or len(feeds) != 3
            or any(not isinstance(feed, dict) or not isinstance(feed.get("name"), str)
                   or feed.get("url") not in ALLOWED_URLS for feed in feeds)
            or {feed["url"] for feed in feeds} != ALLOWED_URLS):
        parser.error("sources must contain exactly the three existing official entry URLs")
    start = time.monotonic()
    report = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {"python": platform.python_version(), "system": platform.system()},
        "limits": {"sources": 3, "dns_calls": 6, "dns_timeout_seconds": DNS_TIMEOUT,
                   "addresses_per_family": MAX_ADDRESSES_PER_FAMILY,
                   "max_tcp_connections": 12, "address_deadline_seconds": PROBE_TIMEOUT,
                   "socket_timeout_seconds": SOCKET_TIMEOUT,
                   "http_method": "HEAD", "max_response_header_bytes": MAX_HEADER_BYTES,
                   "max_body_bytes_read": 0, "retries": 0, "redirects_followed": 0,
                   "max_workers": 3, "per_source_worker_budget_seconds": 58},
        "policy": {"system_resolver_only": True, "proxy_used": False,
                   "tls_certificate_verification": True, "private_memory_read": False},
    }
    with ThreadPoolExecutor(max_workers=3) as executor:
        report["sources"] = list(executor.map(diagnose_source, feeds))
    report["elapsed_seconds"] = round(time.monotonic() - start, 3)
    output = script_relative(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "elapsed_seconds": report["elapsed_seconds"],
                      "http_responses": sum("http" in probe and
                                            probe["http"].get("status") == "response_received"
                                            for source in report["sources"]
                                            for probe in source["probes"])}, ensure_ascii=False))
    # Reachability failures are report data, not diagnostic-program failures.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
