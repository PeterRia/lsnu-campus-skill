"""Old failed notices can recover without redefining the current discovery set."""
# ruff: noqa: E402
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import collect_public as collector
from public_knowledge import seal, validate

FIRST = "2026-09-20T01:00:00+00:00"
SECOND = "2026-09-20T02:00:00+00:00"
THIRD = "2026-09-20T03:00:00+00:00"
FOURTH = "2026-09-20T04:00:00+00:00"


class RetainedRecheckTests(unittest.TestCase):
    def setUp(self):
        self.urls = [f"https://jiaowc.lsnu.edu.cn/info/1015/{n}.htm" for n in range(5)]
        self.cards = [{"source_url": url, "id": str(i), "title": "合成公告"}
                      for i, url in enumerate(self.urls)]
        self.pages = {url: {"title": "合成公告", "published_on": "2026-09-19",
                           "content_sha256": "a" * 64, "links": []} for url in self.urls}
        good = collector.collect(self.cards, [], fetch=self.pages.__getitem__, collected_at=FIRST)
        self.previous = collector.collect(
            self.cards, [], good, Mock(side_effect=TimeoutError()), SECOND,
        )

    def collect_current(self, previous=None, at=THIRD, fetch=None):
        return collector.collect_with_recovery(
            self.cards[:1], [], previous or self.previous,
            fetch or self.pages.__getitem__, now=lambda: at, wait=Mock(),
        )

    def test_recovery_only_refreshes_actually_fetched_old_errors_and_is_bounded(self):
        fetch = Mock(side_effect=self.pages.__getitem__)
        value, report = self.collect_current(fetch=fetch)
        self.assertEqual(fetch.call_count, 3)  # one current, two retained
        self.assertEqual(value["active_item_urls"], self.urls[:1])
        self.assertEqual(value["stats"]["attempted"], 1)
        self.assertEqual(value["stats"]["failed"], 0)
        self.assertEqual(len(report["retained_rechecks"]), 2)
        rows = {row["url"]: row for row in value["items"]}
        self.assertEqual(rows[self.urls[1]]["last_success_at"], THIRD)
        self.assertEqual(rows[self.urls[3]]["last_success_at"], FIRST)
        self.assertEqual(rows[self.urls[3]]["checked_at"], SECOND)
        validate(value)

    def test_failed_retained_checks_keep_content_and_success_time_and_rotate(self):
        def failing_old(url):
            if url != self.urls[0]:
                raise TimeoutError()
            return self.pages[url]
        value, report = self.collect_current(fetch=failing_old)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["retryable_failed_urls"], [])
        for row in report["retained_rechecks"]:
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["last_success_at"], FIRST)
        self.assertEqual(value["content_revision"], self.previous["content_revision"])
        _, next_report = self.collect_current(value, FOURTH, failing_old)
        self.assertEqual([row["url"] for row in next_report["retained_rechecks"]], self.urls[3:])

    def test_recovered_historical_content_changes_revision_but_not_active_counts(self):
        self.pages[self.urls[1]]["content_sha256"] = "b" * 64
        value, report = self.collect_current()
        self.assertNotEqual(value["content_revision"], self.previous["content_revision"])
        self.assertEqual(value["stats"]["changed"], 0)
        self.assertEqual(report["retained_rechecks"][0]["status"], "ok")

    def test_successful_retained_records_are_not_fetched_again(self):
        previous, _ = self.collect_current()
        value, report = self.collect_current(previous, FOURTH)
        self.assertEqual([row["url"] for row in report["retained_rechecks"]], self.urls[3:])
        self.assertTrue(all(row["status"] == "ok" for row in value["items"]))

    def test_no_retained_failure_means_no_extra_fetch(self):
        previous = collector.collect(self.cards, [], fetch=self.pages.__getitem__, collected_at=FIRST)
        fetch = Mock(side_effect=self.pages.__getitem__)
        _, report = self.collect_current(previous, fetch=fetch)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(report["retained_rechecks"], [])


if __name__ == "__main__":
    unittest.main()
