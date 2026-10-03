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

    def trip_circuit(self, address=(HOST, 443)):
        last = None
        for _ in range(transport.CIRCUIT_FAILURE_THRESHOLD):
            with self.assertRaises(transport.ConnectionAttemptsError) as caught:
                transport.connect_official(address)
            last = caught.exception
        return last

    @staticmethod
    def synthetic_addresses(host, port, family):
        row = IPV4 if family == socket.AF_INET else IPV6
        endpoint = (row[4][0], port, *row[4][2:])
        return ((*row[:4], endpoint),)

    def test_circuit_suppresses_new_tcp_attempts_and_keeps_actual_errors(self):
        def failed_socket(*args):
            sock = Mock()
            sock.connect.side_effect = TimeoutError("synthetic TCP outage")
            return sock

        with (
            patch.object(transport, "resolve_addresses", side_effect=self.synthetic_addresses) as dns,
            patch.object(transport.socket, "socket", side_effect=failed_socket) as tcp,
            patch.object(transport.time, "monotonic", return_value=10),
        ):
            last = self.trip_circuit()
            queried, connected = dns.call_count, tcp.call_count
            with self.assertRaises(transport.HostCircuitOpen) as caught:
                transport.connect_official((HOST, 443))
            self.assertEqual(dns.call_count, queried)
            self.assertEqual(tcp.call_count, connected)
        self.assertIsInstance(caught.exception, ConnectionError)
        self.assertEqual(caught.exception.errors, last.errors)
        self.assertEqual(caught.exception.attempts, last.attempts)
        self.assertEqual(caught.exception.retry_after_seconds, 30)
        self.assertEqual(len(last.errors), 2)
        self.assertEqual(connected, transport.CIRCUIT_FAILURE_THRESHOLD * 2)

    def test_dns_failures_also_open_circuit_without_caching_failed_dns(self):
        with (
            patch.object(
                transport.socket,
                "getaddrinfo",
                side_effect=socket.gaierror(socket.EAI_AGAIN, "synthetic DNS outage"),
            ) as dns,
            patch.object(transport.socket, "socket") as tcp,
            patch.object(transport.time, "monotonic", return_value=10),
        ):
            last = self.trip_circuit()
            queries = dns.call_count
            with self.assertRaises(transport.HostCircuitOpen):
                transport.connect_official((HOST, 443))
            self.assertEqual(dns.call_count, queries)
            tcp.assert_not_called()
        self.assertEqual(queries, transport.CIRCUIT_FAILURE_THRESHOLD * 2)
        self.assertTrue(all(isinstance(exc, socket.gaierror) for exc in last.errors))

    def test_cooldown_expiry_allows_retry_and_success_resets_failures(self):
        failing = True

        def make_socket(*args):
            sock = Mock()
            if failing:
                sock.connect.side_effect = TimeoutError("synthetic outage")
            return sock

        with (
            patch.object(transport, "resolve_addresses", side_effect=self.synthetic_addresses),
            patch.object(transport.socket, "socket", side_effect=make_socket) as tcp,
            patch.object(transport.time, "monotonic", return_value=10) as clock,
        ):
            self.trip_circuit()
            clock.return_value = 10 + transport.CIRCUIT_COOLDOWN_SECONDS - 0.001
            connected = tcp.call_count
            with self.assertRaises(transport.HostCircuitOpen):
                transport.connect_official((HOST, 443))
            self.assertEqual(tcp.call_count, connected)
            failing = False
            clock.return_value = 10 + transport.CIRCUIT_COOLDOWN_SECONDS
            self.assertIsNotNone(transport.connect_official((HOST, 443)))
            failing = True
            # A success clears prior consecutive failures; new failures are allowed.
            for _ in range(transport.CIRCUIT_FAILURE_THRESHOLD - 1):
                with self.assertRaises(transport.ConnectionAttemptsError):
                    transport.connect_official((HOST, 443))
            failing = False
            self.assertIsNotNone(transport.connect_official((HOST, 443)))

    def test_circuit_key_is_host_and_port_not_a_global_outage_flag(self):
        blocked = True

        def make_socket(*args):
            sock = Mock()
            if blocked:
                sock.connect.side_effect = TimeoutError("synthetic outage")
            return sock

        with (
            patch.object(transport, "resolve_addresses", side_effect=self.synthetic_addresses),
            patch.object(transport.socket, "socket", side_effect=make_socket),
            patch.object(transport.time, "monotonic", return_value=10),
        ):
            self.trip_circuit()
            blocked = False
            self.assertIsNotNone(transport.connect_official((HOST, 80)))
            self.assertIsNotNone(transport.connect_official(("xuesc.lsnu.edu.cn", 443)))
            with self.assertRaises(transport.HostCircuitOpen):
                transport.connect_official((HOST, 443))

    def test_clearing_dns_cache_also_clears_circuit_state(self):
        failing = True

        def query(host, port, family, *args):
            return self.synthetic_addresses(host, port, family)

        def make_socket(*args):
            sock = Mock()
            if failing:
                sock.connect.side_effect = TimeoutError("synthetic outage")
            return sock

        with (
            patch.object(transport.socket, "getaddrinfo", side_effect=query) as dns,
            patch.object(transport.socket, "socket", side_effect=make_socket),
            patch.object(transport.time, "monotonic", return_value=10),
        ):
            self.trip_circuit()
            before = dns.call_count
            with self.assertRaises(transport.HostCircuitOpen):
                transport.connect_official((HOST, 443))
            transport.clear_dns_cache()
            failing = False
            self.assertIsNotNone(transport.connect_official((HOST, 443)))
            self.assertEqual(dns.call_count, before + 1)

    def test_concurrent_failures_open_circuit_then_suppress_fresh_calls(self):
        start = threading.Barrier(8)

        def make_socket(*args):
            sock = Mock()
            sock.connect.side_effect = TimeoutError("synthetic outage")
            return sock

        def connect(_):
            start.wait(timeout=5)
            try:
                transport.connect_official((HOST, 443))
            except (transport.ConnectionAttemptsError, transport.HostCircuitOpen) as exc:
                return exc
            self.fail("synthetic outage unexpectedly connected")

        with (
            patch.object(transport, "resolve_addresses", side_effect=self.synthetic_addresses) as dns,
            patch.object(transport.socket, "socket", side_effect=make_socket) as tcp,
            patch.object(transport.time, "monotonic", return_value=10),
        ):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(connect, range(8)))
            actual = [exc for exc in results if isinstance(exc, transport.ConnectionAttemptsError)]
            self.assertGreaterEqual(len(actual), transport.CIRCUIT_FAILURE_THRESHOLD)
            # Calls already in flight may exceed the threshold; no strict cap is claimed.
            connected, queried = tcp.call_count, dns.call_count
            with self.assertRaises(transport.HostCircuitOpen):
                transport.connect_official((HOST, 443))
            self.assertEqual(tcp.call_count, connected)
            self.assertEqual(dns.call_count, queried)

    def test_tls_certificate_errors_never_increment_tcp_circuit(self):
        context = ssl.create_default_context()
        socket_type = socket.socket

        def make_socket(*args):
            return Mock(spec=socket_type)

        with (
            patch.object(transport, "resolve_addresses", side_effect=self.synthetic_addresses),
            patch.object(transport.socket, "socket", side_effect=make_socket) as tcp,
            patch.object(transport.time, "monotonic", return_value=10),
            patch.object(
                context,
                "wrap_socket",
                side_effect=ssl.SSLCertVerificationError("synthetic certificate error"),
            ),
        ):
            for _ in range(transport.CIRCUIT_FAILURE_THRESHOLD + 2):
                connection = transport.OfficialHTTPSConnection(HOST, timeout=20, context=context)
                with self.assertRaises(ssl.SSLCertVerificationError):
                    connection.connect()
            self.assertIsNotNone(transport.connect_official((HOST, 443)))
            self.assertEqual(tcp.call_count, transport.CIRCUIT_FAILURE_THRESHOLD + 3)


if __name__ == "__main__":
    unittest.main()
