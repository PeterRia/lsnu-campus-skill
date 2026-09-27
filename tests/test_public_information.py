"""Behavior checks for scheduled public discovery and independent local synchronization."""

# ruff: noqa: E402 -- installed folder Skill, no package installation.

import errno
import json
import socket
import ssl
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_public as collector
import public_knowledge as knowledge
import public_update
from campus import OfficialRedirect

FEED = "https://libnew.lsnu.edu.cn/"
NOTICE = FEED + "info/1004/4061.htm"
FIRST = "2026-09-20T01:00:00+00:00"
SECOND = "2026-09-20T02:00:00+00:00"
RECOVERY = "2026-09-20T02:00:30+00:00"


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.feeds = [{"name": "图书馆", "url": FEED, "sections": ["1004"], "limit": 8}]
        self.pages = {
            FEED: {"links": [(NOTICE, "图书馆中秋国庆开馆通知")]},
            NOTICE: {
                "title": "图书馆中秋国庆开馆通知",
                "published_on": "2026-09-19",
                "content_sha256": "a" * 64,
                "links": [],
            },
        }

    def snapshot(self, previous=None, at=FIRST, fetch=None):
        return collector.collect(
            [], self.feeds, previous, fetch or self.pages.__getitem__, at
        )

    def test_new_notice_discovered_without_a_reviewed_card(self):
        result = self.snapshot()
        self.assertEqual(
            result["stats"],
            {"attempted": 1, "succeeded": 1, "failed": 0, "new": 1, "changed": 0},
        )
        self.assertEqual(result["items"][0]["url"], NOTICE)
        self.assertEqual(result["items"][0]["review_status"], "automatic_observation")
        self.assertEqual(result["items"][0]["tracked_card_ids"], [])
        self.assertNotIn("text", result["items"][0])

    def test_poll_time_changes_without_a_false_content_change(self):
        first = self.snapshot()
        second = self.snapshot(first, SECOND)
        self.assertEqual(first["content_revision"], second["content_revision"])
        self.assertNotEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertEqual(second["items"][0]["change"], "unchanged")
        self.pages[NOTICE]["content_sha256"] = "b" * 64
        changed = self.snapshot(second, SECOND)
        self.assertNotEqual(second["content_revision"], changed["content_revision"])
        self.assertEqual(changed["stats"]["changed"], 1)
        self.pages[NOTICE]["published_on"] = "2026-09-20"
        self.assertEqual(self.snapshot(changed, SECOND)["stats"]["changed"], 1)

    def test_failed_refresh_preserves_last_success_and_content(self):
        self.pages[NOTICE]["title"] += "-乐山师范学院图书馆"
        first = self.snapshot()

        def offline(url):
            raise OSError("synthetic network failure")

        failed = self.snapshot(first, SECOND, offline)
        self.assertEqual(failed["items"][0]["last_success_at"], FIRST)
        self.assertEqual(failed["items"][0]["content_sha256"], "a" * 64)
        self.assertEqual(first["content_revision"], failed["content_revision"])
        self.assertEqual(failed["feeds"][0]["last_success_at"], FIRST)
        self.assertEqual(
            knowledge.summary(failed, datetime.fromisoformat(SECOND))["freshness"],
            "partial",
        )
        self.assertEqual(
            knowledge.summary(
                failed, datetime.fromisoformat("2026-09-20T06:00:00+00:00")
            )["freshness"],
            "stale",
        )

    def test_empty_feed_is_failure_and_retains_previous_discovery(self):
        first = self.snapshot()
        self.pages[FEED]["links"] = []
        result = self.snapshot(first, SECOND)
        self.assertEqual(result["feeds"][0]["error"], "NoUsableNoticeLinks")
        self.assertEqual(result["feeds"][0]["last_success_at"], FIRST)
        self.assertEqual(result["items"][0]["url"], NOTICE)

    def test_transport_retry_is_bounded_and_does_not_retry_invalid_content(self):
        transient = urllib.error.URLError(TimeoutError())
        with (
            patch.object(
                collector, "_fetch_page", side_effect=[transient, self.pages[NOTICE]]
            ) as fetch,
            patch.object(collector.time, "sleep"),
        ):
            self.assertEqual(collector.fetch_page(NOTICE), self.pages[NOTICE])
            self.assertEqual(fetch.call_count, 2)
        with (
            patch.object(collector, "_fetch_page", side_effect=transient) as fetch,
            patch.object(collector.time, "sleep"),
        ):
            with self.assertRaises(urllib.error.URLError):
                collector.fetch_page(NOTICE)
            self.assertEqual(fetch.call_count, 2)
        with patch.object(
            collector, "_fetch_page", side_effect=ValueError("untrusted")
        ) as fetch:
            with self.assertRaises(ValueError):
                collector.fetch_page(NOTICE)
            self.assertEqual(fetch.call_count, 1)

    def recover(self, previous=None, fetch=None):
        clock = iter([SECOND, RECOVERY])
        wait = Mock()
        result = collector.collect_with_recovery(
            [],
            self.feeds,
            previous,
            fetch or self.pages.__getitem__,
            now=lambda: next(clock),
            wait=wait,
        )
        return (*result, wait)

    def test_late_feed_recovery_discovers_new_notice_without_refetching_good_pages(
        self,
    ):
        first = self.snapshot()
        added = FEED + "info/1004/4062.htm"
        self.pages[FEED]["links"].append((added, "新通知"))
        self.pages[added] = {**self.pages[NOTICE], "title": "新通知"}
        self.pages[NOTICE]["content_sha256"] = "b" * 64
        calls = []

        def fetch(url):
            calls.append(url)
            if url == FEED and calls.count(FEED) == 1:
                raise urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, "DNS"))
            return self.pages[url]

        value, report, wait = self.recover(first, fetch)
        wait.assert_called_once_with(30)
        self.assertEqual(calls.count(FEED), 2)
        self.assertEqual(calls.count(NOTICE), 1)
        self.assertEqual(calls.count(added), 1)
        self.assertEqual(
            value["stats"],
            {
                "attempted": 2,
                "succeeded": 2,
                "failed": 0,
                "new": 1,
                "changed": 1,
            },
        )
        records = {row["url"]: row for row in value["items"]}
        self.assertEqual(records[NOTICE]["last_success_at"], SECOND)
        self.assertEqual(records[added]["last_success_at"], RECOVERY)
        self.assertEqual(value["feeds"][0]["last_success_at"], RECOVERY)
        self.assertEqual(report["recovered_urls"], [FEED])
        self.assertEqual(report["remaining_failures"], [])
        self.assertEqual(
            report["initial_failures"][0]["error"], "URLError:gaierror:EAI_AGAIN"
        )

    def test_late_page_recovery_preserves_successful_feed_time(self):
        first = self.snapshot()
        calls = []

        def fetch(url):
            calls.append(url)
            if url == NOTICE and calls.count(NOTICE) == 1:
                raise urllib.error.URLError(TimeoutError())
            return self.pages[url]

        value, report, _ = self.recover(first, fetch)
        self.assertEqual(calls.count(FEED), 1)
        self.assertEqual(calls.count(NOTICE), 2)
        self.assertEqual(value["feeds"][0]["last_success_at"], SECOND)
        self.assertEqual(value["items"][0]["last_success_at"], RECOVERY)
        self.assertEqual(report["recovered_urls"], [NOTICE])

    def test_sustained_outage_stays_failed_and_preserves_original_success_time(self):
        first = self.snapshot()
        fetch = Mock(
            side_effect=urllib.error.URLError(
                OSError(errno.ENETUNREACH, "network unreachable")
            )
        )
        value, report, wait = self.recover(first, fetch)
        self.assertEqual(fetch.call_count, 4)
        wait.assert_called_once_with(30)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["failed_feeds"], 1)
        self.assertEqual(report["recovered_urls"], [])
        self.assertEqual(value["content_revision"], first["content_revision"])
        for row in value["items"] + value["feeds"]:
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["last_success_at"], FIRST)
            self.assertEqual(row["checked_at"], RECOVERY)
            self.assertEqual(row["error"], "URLError:OSError:ENETUNREACH")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.md"
            collector.write_job_summary(report, path)
            summary = path.read_text()
        self.assertIn("ENETUNREACH", summary)
        self.assertIn(FIRST, summary)
        self.assertIn(NOTICE, summary)

    def test_permanent_errors_and_empty_feeds_do_not_start_recovery(self):
        first = self.snapshot()
        errors = [
            ValueError("untrusted origin or invalid page"),
            urllib.error.HTTPError(NOTICE, 404, "missing", {}, None),
            urllib.error.URLError(ssl.SSLCertVerificationError("certificate")),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                fetch = Mock(side_effect=error)
                value, report, wait = self.recover(first, fetch)
                wait.assert_not_called()
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(report["collection_rounds"], 1)
                self.assertEqual(report["failed_feeds"], 1)
                self.assertEqual(value["items"][0]["last_success_at"], FIRST)
        self.pages[FEED]["links"] = []
        value, report, wait = self.recover(first)
        wait.assert_not_called()
        self.assertEqual(value["feeds"][0]["error"], "NoUsableNoticeLinks")

    def test_healthy_collection_never_waits_or_repeats_requests(self):
        fetch = Mock(side_effect=self.pages.__getitem__)
        value, report, wait = self.recover(fetch=fetch)
        wait.assert_not_called()
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(report["collection_rounds"], 1)
        self.assertEqual(report["initial_failures"], [])
        self.assertEqual(value["collected_at"], SECOND)

    def test_error_diagnostics_keep_codes_without_exception_text(self):
        error = urllib.error.URLError(OSError(errno.ENETUNREACH, "response body"))
        self.assertEqual(collector.error_code(error), "URLError:OSError:ENETUNREACH")
        self.assertEqual(
            collector.error_code(
                urllib.error.HTTPError(NOTICE, 503, "response body", {}, None)
            ),
            "HTTPError:503",
        )

    def test_source_scope_and_redirect_are_enforced(self):
        self.pages[FEED]["links"] += [
            ("https://other.example/info/1004/1.htm", "outside"),
            (FEED + "info/9999/1.htm", "other section"),
        ]
        self.assertEqual(len(self.snapshot()["items"]), 1)
        self.feeds[0]["url"] = "https://other.example/"
        with self.assertRaises(ValueError):
            self.snapshot()
        with self.assertRaises(ValueError):
            OfficialRedirect().redirect_request(
                None, None, 302, "", {}, "https://other.example/"
            )

    def test_explicit_publication_date_is_distinct_from_deadline(self):
        html = '<title>图书馆通知</title><meta name="PubDate" content="2026-09-19"><div class="v_news_content">本通知安排假期开放服务，申请截止2026年10月7日，详细时间请核对官方通知。</div>'
        self.assertEqual(
            collector.parse_page(html, NOTICE)["published_on"], "2026-09-19"
        )
        without = html.replace('<meta name="PubDate" content="2026-09-19">', "")
        self.assertIsNone(collector.parse_page(without, NOTICE)["published_on"])
        header = without.replace(
            '<div class="v_news_content">',
            '<p>日期：2026-09-19 作者：图书馆</p><div class="v_news_content">',
        )
        self.assertEqual(
            collector.parse_page(header, NOTICE)["published_on"], "2026-09-19"
        )
        with self.assertRaises(ValueError):
            collector.parse_page(
                "<title>验证</title><p>请输入验证码下载附件，请输入验证码以后才能下载官方文件，否则不能继续完成此次操作。</p>",
                NOTICE,
            )

    def test_tamper_and_impossible_status_rejected(self):
        value = self.snapshot()
        value["items"][0]["title"] = "tampered"
        with self.assertRaises(ValueError):
            knowledge.validate(value)
        value = self.snapshot()
        value["items"][0]["last_success_at"] = SECOND
        with self.assertRaises(ValueError):
            knowledge.validate(knowledge.seal(value))
        value = self.snapshot()
        value["feeds"][0]["notices"][0]["url"] = "https://other.example/"
        with self.assertRaises(ValueError):
            knowledge.validate(knowledge.seal(value))

    def test_corrupt_or_older_remote_keeps_verified_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            newest = self.snapshot(at=SECOND)
            public_update.atomic_json(Path(tmp) / "public-knowledge.json", newest)
            for raw in (b'{"invalid": true}', json.dumps(self.snapshot()).encode()):
                result = knowledge.sync(tmp, lambda *a, **kw: (raw, None))
                self.assertEqual(result["status"], "unavailable_using_cached")
                self.assertEqual(result["collected_at"], SECOND)
                self.assertEqual(result["freshness"], "stale")
                self.assertEqual(
                    json.loads((Path(tmp) / "public-knowledge.json").read_text()),
                    newest,
                )

    def test_search_returns_new_notice_with_its_own_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            public_update.atomic_json(path, self.snapshot())
            result = knowledge.search(path, "图书馆 中秋 国庆")
            self.assertEqual(result["hits"][0]["url"], NOTICE)
            self.assertEqual(result["hits"][0]["freshness"], "stale_or_failed")

    def test_code_unchanged_still_downloads_new_public_information(self):
        current_version = (ROOT / "VERSION").read_text().strip()
        manifest = {
            "schema_version": 1,
            "skill_id": knowledge.SKILL,
            "version": current_version,
            "python_min": "3.10",
            "permissions": public_update.PERMISSIONS,
            "archive_url": f"https://github.com/{public_update.REPO}/releases/download/v{current_version}/{public_update.SKILL}-{current_version}.zip",
            "archive_sha256": "a" * 64,
        }
        snapshot = self.snapshot()
        requests = []

        def fetch(url, limit, **kwargs):
            requests.append(url)
            return json.dumps(
                manifest if url == public_update.MANIFEST_URL else snapshot
            ).encode(), None

        with tempfile.TemporaryDirectory() as tmp:
            first = public_update.update({"installed": str(ROOT), "cache": tmp}, fetch)
            snapshot = self.snapshot(snapshot, SECOND)
            second = public_update.update({"installed": str(ROOT), "cache": tmp}, fetch)
        self.assertEqual(first["update_status"], "current")
        self.assertEqual(second["update_status"], "current")
        self.assertEqual(second["knowledge"]["status"], "updated")
        self.assertEqual(second["knowledge"]["collected_at"], SECOND)
        self.assertEqual(
            requests, [public_update.MANIFEST_URL, knowledge.SNAPSHOT_URL] * 2
        )

    def test_failure_does_not_invent_a_cached_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = knowledge.sync(tmp, lambda *a, **kw: (b"{}", None))
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["snapshot_path"])
            self.assertFalse((Path(tmp) / "public-knowledge.json").exists())


if __name__ == "__main__":
    unittest.main()
