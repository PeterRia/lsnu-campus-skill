"""Validate cross-runner selection using offline, actual collector snapshots."""

# ruff: noqa: E402 -- folder Skill has no separately installed Python package.

import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_public as collector
import collection_ci as ci
from public_knowledge import seal

FEED = "https://jiaowc.lsnu.edu.cn/"
NOTICE_A = FEED + "info/1015/1.htm"
NOTICE_B = FEED + "info/1015/2.htm"
FIRST = "2026-09-20T01:00:00+00:00"
SECOND = "2026-09-20T02:00:00+00:00"
THIRD = "2026-09-20T03:00:00+00:00"


class CollectionCITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.feeds = [
            {"name": "合成通知入口", "url": FEED, "sections": ["1015"], "limit": 2}
        ]
        self.pages = {
            FEED: {"links": [(NOTICE_A, "合成通知甲"), (NOTICE_B, "合成通知乙")]},
            NOTICE_A: {
                "title": "合成通知甲",
                "published_on": "2026-09-19",
                "content_sha256": "a" * 64,
                "links": [],
            },
            NOTICE_B: {
                "title": "合成通知乙",
                "published_on": "2026-09-19",
                "content_sha256": "b" * 64,
                "links": [],
            },
        }
        self.previous = collector.collect(
            [], self.feeds, fetch=self.pages.__getitem__, collected_at=FIRST
        )

    def attempt(self, name, at=SECOND, fetch=None, previous=None):
        end = (datetime.fromisoformat(at) + timedelta(seconds=30)).isoformat()
        ticks = iter([at, end])
        value, report = collector.collect_with_recovery(
            [],
            self.feeds,
            self.previous if previous is None else previous,
            fetch=fetch or self.pages.__getitem__,
            now=lambda: next(ticks),
            wait=Mock(),
        )
        folder = self.root / name
        folder.mkdir()
        self.write(folder / "snapshot.json", value)
        self.write(folder / "collection-report.json", report)
        return folder, value, report

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def select_via_cli(self, primary, fallback):
        output, report = self.root / "selected.json", self.root / "selected-report.json"
        argv = [
            "collection_ci.py",
            "select",
            "--primary", str(primary),
            "--fallback", str(fallback),
            "--output", str(output),
            "--report", str(report),
        ]
        with patch.object(sys, "argv", argv):
            ci.main()
        return output, report

    def test_healthy_primary_does_not_request_fallback(self):
        folder, expected, _ = self.attempt("ubuntu")
        primary = ci.load_attempt(folder, "ubuntu")
        self.assertTrue(primary["healthy"])
        self.assertFalse(primary["recover"])
        value, report = ci.choose_attempt(primary)
        self.assertEqual(value, expected)
        self.assertEqual(report["selected_runner"], "ubuntu")
        self.assertEqual(len(report["attempts"]), 1)
        ci.require_healthy(report)

    def test_retryable_network_failure_requests_fallback(self):
        def network_failure(url):
            if url == NOTICE_A:
                raise TimeoutError("synthetic outage")
            return self.pages[url]

        folder, _, _ = self.attempt("ubuntu", fetch=network_failure)
        primary = ci.load_attempt(folder, "ubuntu")
        self.assertFalse(primary["healthy"])
        self.assertTrue(primary["recover"])
        self.assertEqual(primary["report"]["retryable_failed_urls"], [NOTICE_A])

    def test_permanent_error_does_not_request_fallback(self):
        errors = [
            urllib.error.HTTPError(NOTICE_A, 404, "synthetic missing", {}, None),
            ValueError("synthetic invalid official page"),
        ]
        for number, error in enumerate(errors):
            with self.subTest(error=type(error).__name__):
                def permanent_failure(url):
                    if url == NOTICE_A:
                        raise error
                    return self.pages[url]

                folder, _, _ = self.attempt(f"ubuntu-{number}", fetch=permanent_failure)
                primary = ci.load_attempt(folder, "ubuntu")
                self.assertFalse(primary["healthy"])
                self.assertFalse(primary["recover"])

    def test_report_statistics_corruption_cannot_hide_behind_healthy_fallback(self):
        primary, _, report = self.attempt("ubuntu")
        fallback, _, _ = self.attempt("macos", THIRD)
        report["succeeded"] += 1
        self.write(primary / "collection-report.json", report)
        with self.assertRaises(ValueError):
            ci.load_attempt(primary, "ubuntu")
        with self.assertRaises(ValueError):
            self.select_via_cli(primary, fallback)
        self.assertFalse((self.root / "selected.json").exists())
        self.assertFalse((self.root / "selected-report.json").exists())

    def test_snapshot_hash_corruption_cannot_hide_behind_healthy_fallback(self):
        primary, value, _ = self.attempt("ubuntu")
        fallback, _, _ = self.attempt("macos", THIRD)
        value["items"][0]["title"] = "tampered synthetic title"
        self.write(primary / "snapshot.json", value)
        with self.assertRaises(ValueError):
            ci.load_attempt(primary, "ubuntu")
        with self.assertRaises(ValueError):
            self.select_via_cli(primary, fallback)
        self.assertFalse((self.root / "selected.json").exists())
        self.assertFalse((self.root / "selected-report.json").exists())

    def test_matching_but_false_report_and_snapshot_stats_are_rejected(self):
        def partial_failure(url):
            if url == NOTICE_A:
                raise TimeoutError("synthetic outage")
            return self.pages[url]

        primary, value, report = self.attempt("ubuntu", fetch=partial_failure)
        fallback, _, _ = self.attempt("macos", THIRD)
        # A new checksum is not proof that the statistics describe the records.
        value["stats"].update(failed=0, succeeded=value["stats"]["attempted"])
        report.update(failed=0, succeeded=report["attempted"])
        self.write(primary / "snapshot.json", seal(value))
        self.write(primary / "collection-report.json", report)
        with self.assertRaises(ValueError):
            ci.load_attempt(primary, "ubuntu")
        with self.assertRaises(ValueError):
            self.select_via_cli(primary, fallback)
        self.assertFalse((self.root / "selected.json").exists())

    def test_retained_historical_error_is_outside_current_attempt_statistics(self):
        def old_failure(url):
            if url == NOTICE_A:
                raise TimeoutError("synthetic old outage")
            return self.pages[url]

        _, previous, _ = self.attempt("failed-before", fetch=old_failure)
        self.pages[FEED]["links"] = [(NOTICE_B, "合成通知乙")]
        folder, value, report = self.attempt("ubuntu", THIRD, previous=previous)
        attempt = ci.load_attempt(folder, "ubuntu")
        self.assertEqual(value["active_item_urls"], [NOTICE_B])
        self.assertTrue(attempt["healthy"])
        self.assertFalse(attempt["recover"])
        self.assertEqual(report["attempted"], 1)
        self.assertEqual(report["failed"], 0)
        retained = next(item for item in value["items"] if item["url"] == NOTICE_A)
        self.assertEqual(retained["status"], "error")
        self.assertEqual(retained["last_success_at"], FIRST)

    def test_healthy_fallback_is_selected_with_its_actual_source_times(self):
        def primary_failure(url):
            if url == NOTICE_A:
                raise TimeoutError("synthetic primary outage")
            return self.pages[url]

        primary_folder, _, _ = self.attempt("ubuntu", fetch=primary_failure)
        calls = []

        def late_fallback_recovery(url):
            calls.append(url)
            if url == NOTICE_A and calls.count(NOTICE_A) == 1:
                raise TimeoutError("synthetic short fallback outage")
            return self.pages[url]

        fallback_folder, expected, _ = self.attempt(
            "macos", THIRD, fetch=late_fallback_recovery
        )
        value, report = ci.choose_attempt(
            ci.load_attempt(primary_folder, "ubuntu"),
            ci.load_attempt(fallback_folder, "macos"),
        )
        self.assertEqual(value, expected)
        self.assertEqual(report["selected_runner"], "macos")
        self.assertTrue(report["verified_healthy"])
        rows = {row["url"]: row for row in value["items"]}
        self.assertEqual(value["feeds"][0]["last_success_at"], THIRD)
        self.assertEqual(rows[NOTICE_B]["last_success_at"], THIRD)
        self.assertEqual(rows[NOTICE_A]["last_success_at"], "2026-09-20T03:00:30+00:00")
        ci.require_healthy(report)

    def test_two_failed_runners_remain_failed_with_original_success_times(self):
        offline = Mock(side_effect=TimeoutError("synthetic sustained outage"))
        primary, _, _ = self.attempt("ubuntu", fetch=offline)
        fallback, _, _ = self.attempt("macos", THIRD, fetch=offline)
        value, report = ci.choose_attempt(
            ci.load_attempt(primary, "ubuntu"), ci.load_attempt(fallback, "macos")
        )
        self.assertFalse(report["verified_healthy"])
        self.assertEqual(value["content_revision"], self.previous["content_revision"])
        for row in value["feeds"] + value["items"]:
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["last_success_at"], FIRST)
        with self.assertRaises(ValueError):
            ci.require_healthy(report)

    def exercise_folder(self):
        folder = self.root / "exercise"
        folder.mkdir()
        self.write(folder / "attempt-mode.json", {"mode": "exercise_primary_unavailability"})
        # The marker, rather than fabricated notices, records the exercise.
        return folder

    def test_exercise_marker_is_ubuntu_only_and_cannot_publish_without_live_pages(self):
        folder = self.exercise_folder()
        primary = ci.load_attempt(folder, "ubuntu")
        self.assertFalse(primary["healthy"])
        self.assertTrue(primary["recover"])
        self.assertIsNone(primary["snapshot"])
        with self.assertRaises(ValueError):
            ci.load_attempt(folder, "macos")
        with self.assertRaises(ValueError):
            ci.choose_attempt(primary)
        # An arbitrary file alongside the marker is not accepted as live collection.
        self.write(folder / "snapshot.json", {"fabricated": "synthetic exercise only"})
        with self.assertRaises(ValueError):
            ci.choose_attempt(ci.load_attempt(folder, "ubuntu"))

    def test_exercise_can_only_publish_a_real_validated_fallback_snapshot(self):
        folder = self.exercise_folder()
        fallback_folder, expected, _ = self.attempt("macos", THIRD)
        value, report = ci.choose_attempt(
            ci.load_attempt(folder, "ubuntu"),
            ci.load_attempt(fallback_folder, "macos"),
        )
        self.assertEqual(value, expected)
        self.assertEqual(report["selected_runner"], "macos")
        self.assertEqual(report["attempts"][0]["mode"], "exercise_primary_unavailability")
        self.assertIsNone(report["attempts"][0]["report"])
        ci.require_healthy(report)

    def test_different_baseline_hashes_are_rejected(self):
        primary_folder, _, _ = self.attempt("ubuntu")
        fallback_folder, _, report = self.attempt("macos", THIRD)
        report["previous_snapshot_sha256"] = "f" * 64
        self.write(fallback_folder / "collection-report.json", report)
        primary = ci.load_attempt(primary_folder, "ubuntu")
        fallback = ci.load_attempt(fallback_folder, "macos")
        with self.assertRaises(ValueError):
            ci.choose_attempt(primary, fallback)

    def test_zero_scope_cannot_be_passed_off_as_healthy_collection(self):
        value, report = collector.collect_with_recovery(
            [], [], now=lambda: SECOND, wait=Mock()
        )
        folder = self.root / "empty"
        folder.mkdir()
        self.write(folder / "snapshot.json", value)
        self.write(folder / "collection-report.json", report)
        with self.assertRaises(ValueError):
            ci.load_attempt(folder, "ubuntu")


if __name__ == "__main__":
    unittest.main()
