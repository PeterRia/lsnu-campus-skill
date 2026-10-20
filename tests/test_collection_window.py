"""Daytime scheduling and network guards, with no real school requests."""

# ruff: noqa: E402 -- folder Skill has no separately installed Python package.

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_public as collector
import collection_window as window

NOTICE = "https://jiaowc.lsnu.edu.cn/info/1015/1.htm"


class CollectionWindowTests(unittest.TestCase):
    def test_daytime_boundaries_use_campus_time(self):
        for timestamp, expected in (
            ("2026-10-20T07:59:59+08:00", False),
            ("2026-10-20T08:00:00+08:00", True),
            ("2026-10-20T10:00:00+08:00", True),
            ("2026-10-20T17:59:59+08:00", True),
            ("2026-10-20T18:00:00+08:00", False),
            ("2026-10-20T23:00:00+08:00", False),
        ):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(window.window_status(datetime.fromisoformat(timestamp))["allowed"], expected)

    def test_host_timezone_does_not_change_policy(self):
        instants = (
            "2026-10-20T02:00:00+00:00",
            "2026-10-20T10:00:00+08:00",
            "2026-10-19T19:00:00-07:00",
        )
        statuses = [window.window_status(datetime.fromisoformat(value)) for value in instants]
        self.assertTrue(all(value["allowed"] for value in statuses))
        self.assertEqual(len({value["checked_at"] for value in statuses}), 1)
        with self.assertRaises(ValueError):
            window.window_status(datetime(2026, 10, 20, 10))

    def test_night_fetch_never_constructs_network_opener_or_retries(self):
        with (
            patch.dict(os.environ, {window.ENFORCEMENT_ENV: "1"}),
            patch.object(window, "current_time", return_value=datetime(2026, 10, 20, 15, tzinfo=timezone.utc)),
            patch.object(collector.urllib.request, "build_opener") as opener,
            patch.object(collector.time, "sleep") as sleep,
        ):
            with self.assertRaises(window.CollectionWindowClosed):
                collector.fetch_page(NOTICE)
        opener.assert_not_called()
        sleep.assert_not_called()
        self.assertFalse(collector.transient_error(window.CollectionWindowClosed()))

    def test_boundary_during_immediate_retry_stops_next_network_request(self):
        times = iter([
            datetime(2026, 10, 20, 9, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc),
        ])
        opener = Mock()
        opener.open.side_effect = TimeoutError("synthetic")
        with (
            patch.dict(os.environ, {window.ENFORCEMENT_ENV: "1"}),
            patch.object(window, "current_time", side_effect=lambda: next(times)),
            patch.object(collector.urllib.request, "build_opener", return_value=opener),
            patch.object(collector.time, "sleep") as sleep,
        ):
            with self.assertRaises(window.CollectionWindowClosed):
                collector.fetch_page(NOTICE)
        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_called_once_with(1)

    def test_redirects_have_the_same_request_guard(self):
        guard = collector.CollectionWindowRequest()
        request = Mock()
        with (
            patch.dict(os.environ, {window.ENFORCEMENT_ENV: "1"}),
            patch.object(window, "current_time", return_value=datetime(2026, 10, 20, 10, tzinfo=timezone.utc)),
        ):
            for method in (guard.http_request, guard.https_request):
                with self.assertRaises(window.CollectionWindowClosed):
                    method(request)

    def test_boundary_error_aborts_collection_instead_of_creating_error_snapshot(self):
        feeds = [{"name": "合成入口", "url": "https://jiaowc.lsnu.edu.cn/", "sections": ["1015"], "limit": 1}]
        fetch = Mock(side_effect=window.CollectionWindowClosed())
        wait = Mock()
        with self.assertRaises(window.CollectionWindowClosed):
            collector.collect_with_recovery([], feeds, fetch=fetch, wait=wait)
        wait.assert_not_called()

    def test_night_cli_records_deferred_without_any_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output, summary = root / "output", root / "summary.md"
            folder = root / "attempt"
            with (
                patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}),
                patch.object(window, "current_time", return_value=datetime(2026, 10, 20, 15, tzinfo=timezone.utc)),
                patch.object(sys, "argv", ["collection_window.py", "--attempt-folder", str(folder)]),
            ):
                window.main()
            marker = json.loads((folder / "attempt-mode.json").read_text())
            self.assertEqual(marker["mode"], window.DEFERRED_MODE)
            self.assertEqual(output.read_text(), "allowed=false\n")
            self.assertIn("not a successful collection", summary.read_text())
            self.assertEqual({p.name for p in folder.iterdir()}, {"attempt-mode.json", "window-check.json"})

    def test_offline_collection_without_workflow_flag_is_unrestricted(self):
        with (
            patch.dict(os.environ, {window.ENFORCEMENT_ENV: "0"}),
            patch.object(window, "window_status") as status,
        ):
            window.require_collection_window()
        status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
