"""Transport regressions using synthetic HTTP responses, never live network I/O."""

# ruff: noqa: E402 -- folder Skill has no separately installed Python package.

import io
import ssl
import sys
import unittest
from http.client import BadStatusLine, HTTPResponse, IncompleteRead
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_public as collector
from official_transport import HostCircuitOpen


FEED = "https://jiaowc.lsnu.edu.cn/"
NOTICE = FEED + "info/1015/1.htm"
FIRST = "2026-09-20T01:00:00+00:00"
SECOND = "2026-09-20T02:00:00+00:00"
RECOVERY = "2026-09-20T02:00:30+00:00"
BODY = (
    '<title>合成教学通知</title><div class="v_news_content">'
    "这是一段用于验证完整传输的合成公告文字。"
    "通知包含足够的正文，但没有个人资料，也没有来自真实网站的正文。"
    "</div>"
).encode("utf-8")


def response_with_length(length):
    """Exercise Python's actual HTTPResponse.read, including early connection EOF."""
    headers = "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
    if length is not None:
        headers += f"Content-Length: {length}\r\n"
    raw = headers.encode("ascii") + b"\r\n" + BODY

    class SyntheticSocket:
        def makefile(self, *args):
            return io.BytesIO(raw)

    response = HTTPResponse(SyntheticSocket())
    response.begin()
    return response


class PublicTransportTests(unittest.TestCase):
    def setUp(self):
        self.feeds = [
            {"name": "合成通知入口", "url": FEED, "sections": ["1015"], "limit": 1}
        ]
        self.pages = {
            FEED: {"links": [(NOTICE, "合成教学通知")]},
            NOTICE: {
                "title": "合成教学通知",
                "published_on": "2026-09-19",
                "content_sha256": "a" * 64,
                "links": [],
            },
        }

    @staticmethod
    def transport_errors():
        # These are HTTPException subclasses, not OSError subclasses.
        return [IncompleteRead(b"synthetic partial", 17), BadStatusLine("synthetic")]

    def previous_snapshot(self):
        return collector.collect(
            [], self.feeds, fetch=self.pages.__getitem__, collected_at=FIRST
        )

    def recover(self, previous, fetch):
        ticks = iter([SECOND, RECOVERY])
        wait = Mock()
        value, report = collector.collect_with_recovery(
            [],
            self.feeds,
            previous,
            fetch=fetch,
            now=lambda: next(ticks),
            wait=wait,
        )
        return value, report, wait

    def test_protocol_transport_errors_get_bounded_immediate_retry(self):
        for exc in self.transport_errors():
            with self.subTest(error=type(exc).__name__):
                with (
                    patch.object(
                        collector, "_fetch_page", side_effect=[exc, self.pages[NOTICE]]
                    ) as fetch,
                    patch.object(collector.time, "sleep") as wait,
                ):
                    self.assertEqual(collector.fetch_page(NOTICE), self.pages[NOTICE])
                    self.assertEqual(fetch.call_count, 2)
                    wait.assert_called_once_with(1)
                with (
                    patch.object(collector, "_fetch_page", side_effect=exc) as fetch,
                    patch.object(collector.time, "sleep") as wait,
                ):
                    with self.assertRaises(type(exc)):
                        collector.fetch_page(NOTICE)
                    self.assertEqual(fetch.call_count, 2)
                    wait.assert_called_once_with(1)

    def test_tls_early_eof_retries_without_mislabeling_it_as_an_os_errno(self):
        error = ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "synthetic response text")
        with (
            patch.object(collector, "_fetch_page", side_effect=[error, self.pages[NOTICE]]) as fetch,
            patch.object(collector.time, "sleep"),
        ):
            self.assertEqual(collector.fetch_page(NOTICE), self.pages[NOTICE])
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(collector.error_code(error), "SSLEOFError:SSL_ERROR_EOF")
        self.assertFalse(collector.transient_error(
            ssl.SSLCertVerificationError(ssl.SSL_ERROR_SSL, "certificate")
        ))

    def test_suppressed_connection_is_distinct_from_its_prior_actual_failure(self):
        error = HostCircuitOpen(
            [TimeoutError()], [{"family": "IPv4", "error": "TimeoutError"}], 17
        )
        self.assertTrue(collector.transient_error(error))
        self.assertEqual(
            collector.error_code(error), "HostCircuitOpen[previous=IPv4:TimeoutError]"
        )
        previous = self.previous_snapshot()
        value, report, wait = self.recover(previous, Mock(side_effect=error))
        wait.assert_called_once_with(30)
        self.assertEqual(report["retryable_failed_urls"], sorted([FEED, NOTICE]))
        self.assertEqual(value["items"][0]["last_success_at"], FIRST)
        self.assertIn("HostCircuitOpen[previous=", report["remaining_failures"][0]["error"])

    def test_late_protocol_recovery_preserves_other_source_success_time(self):
        previous = self.previous_snapshot()
        for exc in self.transport_errors():
            with self.subTest(error=type(exc).__name__):
                calls = []

                def fetch(url):
                    calls.append(url)
                    if url == NOTICE and calls.count(NOTICE) == 1:
                        raise exc
                    return self.pages[url]

                value, report, wait = self.recover(previous, fetch)
                wait.assert_called_once_with(30)
                self.assertEqual(calls.count(FEED), 1)
                self.assertEqual(calls.count(NOTICE), 2)
                self.assertEqual(value["feeds"][0]["last_success_at"], SECOND)
                self.assertEqual(value["items"][0]["last_success_at"], RECOVERY)
                self.assertEqual(value["content_revision"], previous["content_revision"])
                self.assertEqual(report["recovered_urls"], [NOTICE])
                self.assertEqual(report["remaining_failures"], [])
                self.assertEqual(
                    report["initial_failures"][0]["error"], type(exc).__name__
                )

    def test_sustained_protocol_failure_keeps_last_verified_snapshot(self):
        previous = self.previous_snapshot()
        for exc in self.transport_errors():
            with self.subTest(error=type(exc).__name__):
                fetch = Mock(side_effect=exc)
                value, report, wait = self.recover(previous, fetch)
                wait.assert_called_once_with(30)
                self.assertEqual(fetch.call_count, 4)
                self.assertEqual(report["collection_rounds"], 2)
                self.assertEqual(report["failed"], 1)
                self.assertEqual(report["failed_feeds"], 1)
                self.assertEqual(report["recovered_urls"], [])
                self.assertEqual(value["content_revision"], previous["content_revision"])
                self.assertEqual(value["items"][0]["content_sha256"], "a" * 64)
                self.assertEqual(value["items"][0]["change"], "not_verified")
                for row in value["feeds"] + value["items"]:
                    self.assertEqual(row["status"], "error")
                    self.assertEqual(row["checked_at"], RECOVERY)
                    self.assertEqual(row["last_success_at"], FIRST)
                    self.assertEqual(row["error"], type(exc).__name__)
                # Only the exception type is public; the synthetic payload never leaks.
                self.assertNotIn("synthetic", str(report))

    def test_declared_content_length_truncation_is_not_a_success(self):
        opener = Mock()
        opener.open.return_value = response_with_length(len(BODY) + 1024)
        with patch.object(
            collector.urllib.request, "build_opener", return_value=opener
        ):
            with self.assertRaises(IncompleteRead):
                collector._fetch_page(NOTICE)

    def test_complete_content_length_response_still_parses(self):
        opener = Mock()
        opener.open.return_value = response_with_length(len(BODY))
        with patch.object(
            collector.urllib.request, "build_opener", return_value=opener
        ):
            result = collector._fetch_page(NOTICE)
        self.assertEqual(result["title"], "合成教学通知")
        self.assertRegex(result["content_sha256"], r"^[0-9a-f]{64}$")

    def test_response_without_content_length_still_parses(self):
        opener = Mock()
        opener.open.return_value = response_with_length(None)
        with patch.object(
            collector.urllib.request, "build_opener", return_value=opener
        ):
            result = collector._fetch_page(NOTICE)
        self.assertEqual(result["title"], "合成教学通知")
        self.assertRegex(result["content_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
