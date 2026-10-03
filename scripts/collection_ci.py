"""Validate collection attempts and publish only a truthfully selected snapshot."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from collect_public import write_job_summary
from public_knowledge import instant, validate
from public_update import atomic_json


def load_attempt(folder, runner):
    folder = Path(folder)
    mode_path = folder / "attempt-mode.json"
    mode = json.loads(mode_path.read_text()) if mode_path.is_file() else {"mode": "live"}
    if mode.get("mode") == "exercise_primary_unavailability":
        if runner != "ubuntu":
            raise ValueError("模拟标记只能用于手动测试的主采集环境")
        return {"runner": runner, "mode": mode["mode"], "healthy": False,
                "recover": True, "snapshot": None, "report": None}
    value = validate(json.loads((folder / "snapshot.json").read_text()))
    report = json.loads((folder / "collection-report.json").read_text())
    for key in ("collected_at", "content_revision"):
        if report.get(key) != value[key]:
            raise ValueError("采集报告与快照不匹配: " + key)
    for key, expected in value["stats"].items():
        if report.get(key) != expected:
            raise ValueError("采集统计与快照不匹配: " + key)
    if report.get("failed_feeds") != sum(f["status"] != "ok" for f in value["feeds"]):
        raise ValueError("通知入口统计与快照不匹配")
    if (not isinstance(report["attempted"], int) or report["attempted"] <= 0
            or report["attempted"] != report["succeeded"] + report["failed"]
            or not value["feeds"]):
        raise ValueError("采集范围或成功失败数量异常")
    active = value.get("active_item_urls")
    records = {item["url"]: item for item in value["items"]}
    if (not isinstance(active, list) or len(active) != len(set(active))
            or len(active) != report["attempted"] or any(url not in records for url in active)):
        raise ValueError("快照缺少明确的当轮采集范围")
    if report["failed"] != sum(records[url]["status"] != "ok" for url in active):
        raise ValueError("当轮来源状态与成功失败统计不匹配")
    started = instant(report["collection_started_at"])
    for row in [*[records[url] for url in active], *value["feeds"]]:
        if instant(row["checked_at"]) < started:
            raise ValueError("当轮采集不能用历史检查记录代替")
        if row["status"] == "ok" and row["last_success_at"] != row["checked_at"]:
            raise ValueError("成功状态与本轮成功时间不匹配")
    healthy = report["failed"] == 0 and report["failed_feeds"] == 0
    return {"runner": runner, "mode": "live", "healthy": healthy,
            "recover": not healthy and bool(report.get("retryable_failed_urls")),
            "snapshot": value, "report": report}


def choose_attempt(primary, fallback=None):
    attempts = [a for a in (primary, fallback) if a is not None]
    live = [a for a in attempts if a["snapshot"] is not None]
    if not live:
        raise ValueError("没有经过校验的实际采集快照，不能发布")
    baselines = {a["report"].get("previous_snapshot_sha256") for a in live}
    if len(baselines) != 1:
        raise ValueError("两个环境的原始快照不同，不能混用内容变化统计")
    chosen = max(live, key=lambda a: (
        a["healthy"], a["report"]["succeeded"], -a["report"]["failed_feeds"],
        a["snapshot"]["collected_at"],
    ))
    report = {
        **chosen["report"], "selected_runner": chosen["runner"],
        "verified_healthy": chosen["healthy"],
        "attempts": [
            {"runner": a["runner"], "mode": a["mode"], "healthy": a["healthy"],
             "report": a["report"]} for a in attempts
        ],
    }
    return chosen["snapshot"], report


def require_healthy(report):
    if not report.get("verified_healthy") or report["failed"] or report["failed_feeds"]:
        raise ValueError("所有实际采集环境均未完整恢复；已发布保留原成功时间的错误快照")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    health = commands.add_parser("health")
    health.add_argument("--folder", type=Path, required=True)
    health.add_argument("--runner", required=True)
    select = commands.add_parser("select")
    select.add_argument("--primary", type=Path, required=True)
    select.add_argument("--fallback", type=Path, required=True)
    select.add_argument("--output", type=Path, required=True)
    select.add_argument("--report", type=Path, required=True)
    select.add_argument("--summary", type=Path)
    final = commands.add_parser("require-healthy")
    final.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "health":
        attempt = load_attempt(args.folder, args.runner)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(f"healthy={str(attempt['healthy']).lower()}\n")
            stream.write(f"recover={str(attempt['recover']).lower()}\n")
        print(json.dumps({k: attempt[k] for k in ("runner", "mode", "healthy", "recover")}))
    elif args.command == "select":
        primary = load_attempt(args.primary, "ubuntu")
        fallback = load_attempt(args.fallback, "macos") if args.fallback.is_dir() else None
        value, report = choose_attempt(primary, fallback)
        atomic_json(args.output, value)
        atomic_json(args.report, report)
        if args.summary:
            write_job_summary(report, args.summary)
            with args.summary.open("a") as stream:
                stream.write(f"\nSelected runner: **{report['selected_runner']}**. "
                             f"Verified complete: **{report['verified_healthy']}**.\n\n")
                for attempt in report["attempts"]:
                    stream.write(f"- {attempt['runner']}: mode={attempt['mode']}, "
                                 f"healthy={attempt['healthy']}\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        require_healthy(json.loads(args.report.read_text()))


if __name__ == "__main__":
    main()
