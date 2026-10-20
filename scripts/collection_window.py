"""Keep automated campus collection inside 08:00–18:00 at UTC+8."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

CAMPUS_TIMEZONE = timezone(timedelta(hours=8))
START_HOUR = 8
END_HOUR = 18
ENFORCEMENT_ENV = "LSNU_ENFORCE_COLLECTION_WINDOW"
DEFERRED_MODE = "deferred_outside_collection_window"


class CollectionWindowClosed(RuntimeError):
    """A school request was stopped by the daytime collection policy."""


def current_time():
    return datetime.now(timezone.utc)


def window_status(at=None):
    at = current_time() if at is None else at
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("采集时间必须带有时区")
    local = at.astimezone(CAMPUS_TIMEZONE)
    allowed = START_HOUR <= local.hour < END_HOUR
    return {
        "allowed": allowed,
        "checked_at": at.astimezone(timezone.utc).isoformat(),
        "campus_time": local.isoformat(),
        "window": "08:00 <= UTC+8 time < 18:00",
        "mode": "live" if allowed else DEFERRED_MODE,
        "reason": None if allowed else "outside_collection_window",
    }


def require_collection_window():
    # Only the automated collection workflow sets this switch. Normal Skill
    # lookups and offline tests do not inherit a machine-local time restriction.
    if os.environ.get(ENFORCEMENT_ENV) == "1" and not window_status()["allowed"]:
        raise CollectionWindowClosed("已超过学校白天采集时段（UTC+8 08:00—18:00），停止后续请求")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-folder", type=Path)
    args = parser.parse_args()
    status = window_status()
    if args.attempt_folder:
        args.attempt_folder.mkdir(parents=True, exist_ok=True)
        (args.attempt_folder / "window-check.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if not status["allowed"]:
            (args.attempt_folder / "attempt-mode.json").write_text(
                json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            stream.write(f"allowed={str(status['allowed']).lower()}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(f"\nCollection window checked at `{status['campus_time']}`.\n\n")
            if status["allowed"]:
                stream.write("Inside the UTC+8 08:00–18:00 collection window.\n")
            else:
                stream.write("**Collection deferred:** outside the UTC+8 08:00–18:00 window. "
                             "No school requests or new snapshot from this skipped attempt. "
                             "This is not a successful collection.\n")
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
