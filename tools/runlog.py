#!/usr/bin/env python
"""定时任务执行日志：追加/查看 reports/sim/runlog.json。

用法：
  .venv/bin/python tools/runlog.py add --task execute --status ok --line "main: 成交 1 笔" [--trade-date 2026-10-26]
  .venv/bin/python tools/runlog.py show [--n 20]

status 取值：ok / skipped / failed / alert
"""
import argparse
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATH = ROOT / "reports" / "sim" / "runlog.json"
MAX_ENTRIES = 300


def load() -> list:
    if PATH.exists():
        try:
            data = json.loads(PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return []


def cmd_add(args) -> int:
    entries = load()
    entry = {
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "task": args.task,
        "status": args.status,
        "lines": args.line or [],
    }
    if args.trade_date:
        entry["trade_date"] = args.trade_date
    entries.append(entry)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(entries[-MAX_ENTRIES:], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"runlog: 已追加 {args.task}/{args.status} -> {PATH.relative_to(ROOT)}")
    return 0


def cmd_show(args) -> int:
    for e in load()[-args.n:]:
        lines = " | ".join(e.get("lines", []))
        print(f"{e['ts']}  {e['task']:8s} {e['status']:8s} {lines}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="追加一条执行记录")
    a.add_argument("--task", required=True, choices=["execute", "evening", "research", "gold", "manual"])
    a.add_argument("--status", required=True, choices=["ok", "skipped", "failed", "alert"])
    a.add_argument("--trade-date", default=None)
    a.add_argument("--line", action="append", default=[], help="汇报行，可重复")
    a.set_defaults(fn=cmd_add)
    s = sub.add_parser("show", help="查看最近记录")
    s.add_argument("--n", type=int, default=20)
    s.set_defaults(fn=cmd_show)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
