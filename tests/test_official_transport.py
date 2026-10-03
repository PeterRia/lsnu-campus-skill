"""Official transport behavior with synthetic DNS, sockets, and TLS only."""

# ruff: noqa: E402 -- folder Skill has no separately installed Python package.

import errno
import socket
import ssl
import sys
import threading
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch, sentinel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import official_transport as transport

HOST = "jiaowc.lsnu.edu.cn"
IPV4 = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.1", 443))
IPV6 = (
    socket.AF_INET6,
    socket.SOCK_STREAM,
    socket.IPPROTO_TCP,
    "",
    ("2001:db8::1", 443, 0, 0),
)


class OfficialTransportTests(unittest.TestCase):
    def setUp(self):
        transport.clear_dns_cache()

    def tearDown(self):
        transport.clear_dns_cache()

    def test_ipv4_success_never_queries_unused_ipv6(self):
        connected = Mock()
        with (
            patch.object(transport.socket, "getaddrinfo", return_value=[IPV4]) as dns,
            patch.object(transport.socket, "socket", return_value=connected),
            patch.object(transport.time, "monotonic", return_value=10),
        ):
            result = transport.connect_official((HOST, 443), timeout=20)
        self.assertIs(result, connected)
        dns.assert_called_once_with(
            HOST, 443, socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP
        )
        connected.connect.assert_called_once_with(IPV4[4])
        self.assertEqual(connected.settimeout.call_count, 2)
        self.assertEqual(connected.settimeout.call_args.args, (20,))
        connected.close.assert_not_called()

    def test_failed_ipv4_and_ipv6_attempts_keep_both_original_errors(self):
        ipv4_error = TimeoutError("synthetic IPv4 timeout")
        ipv6_error = OSError(errno.ENETUNREACH, "synthetic IPv6 route failure")
        sockets = [Mock(), Mock()]
        sockets[0].connect.side_effect = ipv4_error
        sockets[1].connect.side_effect = ipv6_error
        with (
            patch.object(transport.socket, "getaddrinfo", side_effect=[[IPV4], [IPV6]]) as dns,
            patch.object(transport.socket, "socket", side_effect=sockets),
            patch.object(transport.time, "monotonic", return_value=10),
        ):
            with self.assertRaises(transport.ConnectionAttemptsError) as caught:
                transport.connect_official((HOST, 443), timeout=20)
        self.assertEqual(caught.exception.errors, (ipv4_error, ipv6_error))
        self.assertEqual(
            caught.exception.attempts,
            (
                {"family": "IPv4", "stage": "tcp", "error": "TimeoutError"},
                {"family": "IPv6", "stage": "tcp", "error": "OSError:ENETUNREACH"},
            ),
        )
        self.assertEqual([call.args[2] for call in dns.call_args_list], [socket.AF_INET, socket.AF_INET6])
        for sock in sockets:
            sock.close.assert_called_once_with()
        self.assertNotIn("synthetic", str(caught.exception))

    def test_ipv6_can_recover_an_ipv4_dns_failure(self):
        connected = Mock()
        with (
            patch.object(
                transport.socket,
                "getaddrinfo",
                side_effect=[socket.gaierror(socket.EAI_AGAIN, "synthetic"), [IPV6]],
            ) as dns,
            patch.object(transport.socket, "socket", return_value=connected),
            patch.object(transport.time, "monotonic", return_value=10),
        ):
            result = transport.connect_official((HOST, 443), timeout=20)
        self.assertIs(result, connected)
        self.assertEqual([call.args[2] for call in dns.call_args_list], [socket.AF_INET, socket.AF_INET6])
        connected.connect.assert_called_once_with(IPV6[4])

    def test_concurrent_positive_dns_resolution_is_coalesced(self):
        start = threading.Barrier(8)

        def query(*args):
            # A small bounded delay makes simultaneous cache misses realistic.
            threading.Event().wait(0.02)
            return [IPV4]

        def resolve(_):
            start.wait(timeout=5)
            return transport.resolve_addresses(HOST, 443, socket.AF_INET)

        with patch.object(transport.socket, "getaddrinfo", side_effect=query) as dns:
            with ThreadPoolExecutor(max_workers=8) as pool:
                answers = list(pool.map(resolve, range(8)))
        self.assertEqual(answers, [(IPV4,)] * 8)
        dns.assert_called_once_with(
            HOST, 443, socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP
        )

    def test_failed_dns_answer_is_not_cached_and_can_recover(self):
        failures = [
            socket.gaierror(socket.EAI_AGAIN, "synthetic first failure"),
            socket.gaierror(socket.EAI_AGAIN, "synthetic second failure"),
        ]
        with patch.object(
            transport.socket, "getaddrinfo", side_effect=[*failures, [IPV4]]
        ) as dns:
            for failure in failures:
                with self.assertRaises(socket.gaierror) as caught:
                    transport.resolve_addresses(HOST, 443, socket.AF_INET)
                self.assertIs(caught.exception, failure)
            self.assertEqual(transport.resolve_addresses(HOST, 443, socket.AF_INET), (IPV4,))
            self.assertEqual(transport.resolve_addresses(HOST, 443, socket.AF_INET), (IPV4,))
        self.assertEqual(dns.call_count, 3)

    def test_dns_answer_refreshes_when_ttl_expires(self):
        newer = (*IPV4[:4], ("192.0.2.2", 443))
        with (
            patch.object(transport.socket, "getaddrinfo", side_effect=[[IPV4], [newer]]) as dns,
            patch.object(transport.time, "monotonic", return_value=10) as clock,
        ):
            self.assertEqual(transport.resolve_addresses(HOST, 443, socket.AF_INET), (IPV4,))
            clock.return_value = 10 + transport.DNS_TTL_SECONDS - 0.001
            self.assertEqual(transport.resolve_addresses(HOST, 443, socket.AF_INET), (IPV4,))
            self.assertEqual(dns.call_count, 1)
            clock.return_value = 10 + transport.DNS_TTL_SECONDS
            self.assertEqual(transport.resolve_addresses(HOST, 443, socket.AF_INET), (newer,))
        self.assertEqual(dns.call_count, 2)

    def test_dns_cache_keeps_host_port_and_family_separate(self):
        def query(host, port, family, *args):
            source = IPV4 if family == socket.AF_INET else IPV6
            endpoint = (source[4][0], port, *source[4][2:])
            return [(*source[:4], endpoint)]

        keys = [
            (HOST, 443, socket.AF_INET),
            (HOST, 80, socket.AF_INET),
            (HOST, 443, socket.AF_INET6),
            ("xuesc.lsnu.edu.cn", 443, socket.AF_INET),
        ]
        with patch.object(transport.socket, "getaddrinfo", side_effect=query) as dns:
            for key in keys * 2:
                result = transport.resolve_addresses(*key)
                self.assertEqual(result[0][0], key[2])
                self.assertEqual(result[0][4][1], key[1])
        self.assertEqual(dns.call_count, len(keys))

    def test_https_uses_original_hostname_and_verified_context(self):
        context = ssl.create_default_context()
        raw_socket = Mock(spec=socket.socket)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        with (
            patch.object(transport, "connect_official", return_value=raw_socket) as connect,
            patch.object(context, "wrap_socket", return_value=sentinel.tls_socket) as wrap,
        ):
            connection = transport.OfficialHTTPSConnection(HOST, timeout=20, context=context)
            connection.connect()
        connect.assert_called_once_with((HOST, 443), 20, None)
        wrap.assert_called_once_with(raw_socket, server_hostname=HOST)
        self.assertIs(connection.sock, sentinel.tls_socket)
        self.assertIs(connection._context, context)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

        # The urllib handler passes the same verified context to the connection.
        handler = transport.OfficialHTTPSHandler(context=context)
        request = urllib.request.Request(f"https://{HOST}/")
        with patch.object(handler, "do_open", return_value=sentinel.response) as open_request:
            self.assertIs(handler.https_open(request), sentinel.response)
        open_request.assert_called_once_with(transport.OfficialHTTPSConnection, request, context=context)

    def test_default_https_context_keeps_certificate_failure_visible(self):
        raw_socket = Mock(spec=socket.socket)
        with patch.object(transport, "connect_official", return_value=raw_socket):
            connection = transport.OfficialHTTPSConnection(HOST, timeout=20)
            self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(connection._context.check_hostname)
            with patch.object(
                connection._context,
                "wrap_socket",
                side_effect=ssl.SSLCertVerificationError("synthetic invalid certificate"),
            ):
                with self.assertRaises(ssl.SSLCertVerificationError):
                    connection.connect()

    def test_unofficial_targets_are_rejected_before_dns_or_tcp(self):
        targets = [
            ("outside.example", 443),
            ("lsnu.edu.cn.outside.example", 443),
            ("127.0.0.1", 80),
            (HOST, 8080),
        ]
        with (
            patch.object(transport.socket, "getaddrinfo") as dns,
            patch.object(transport.socket, "socket") as tcp,
        ):
            for address in targets:
                with self.subTest(address=address):
                    with self.assertRaises(ValueError):
                        transport.connect_official(address)
        dns.assert_not_called()
        tcp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
