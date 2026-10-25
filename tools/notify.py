#!/usr/bin/env python3
"""通过 Bark 推送一条通知。

用法：
  .venv/bin/python tools/notify.py --title "标题" --body "内容"
  .venv/bin/python tools/notify.py --title "标题" --body-file /tmp/body.txt
  .venv/bin/python tools/notify.py --title "测试" --body "hi" --dry-run

配置优先级：环境变量 BARK_KEY / BARK_SERVER > config/notify.local.yaml。
未配置 key 时打印一行说明并以退出码 0 结束（不影响定时任务）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "notify.local.yaml"


def _read_config() -> dict:
    if not CONFIG.exists():
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
        return data.get("bark") or {}
    except Exception as exc:  # 配置错误不应拖垮主任务
        print(f"notify: 读取 {CONFIG} 失败：{exc}", file=sys.stderr)
        return {}


def _setting(name: str, default: str = "") -> str:
    env_name = {"key": "BARK_KEY", "server": "BARK_SERVER", "group": "BARK_GROUP"}.get(name)
    env = os.environ.get(env_name or "", "")
    if env:
        return env
    cfg = _read_config()
    value = cfg.get(name) or ""
    return str(value) if value else default


def main() -> int:
    parser = argparse.ArgumentParser(description="发送 Bark 通知")
    parser.add_argument("--title", required=True, help="通知标题")
    parser.add_argument("--body", default="", help="通知正文，支持字面量 \\n 换行")
    parser.add_argument("--body-file", help="从文件读取正文（优先于 --body）")
    parser.add_argument("--level", default="active", help="active / timeSensitive / passive")
    parser.add_argument("--group", default="", help="通知分组，默认取配置")
    parser.add_argument("--url", default="", help="点击通知打开的链接")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容")
    args = parser.parse_args()

    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    else:
        body = args.body.replace("\\n", "\n")

    key = _setting("key")
    server = (_setting("server", "https://api.day.app") or "https://api.day.app").rstrip("/")
    group = args.group or _setting("group", "A股模拟盘")

    if not key or key == "YOUR_DEVICE_KEY":
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "device_key": "(未配置)",
                        "title": args.title,
                        "body": body,
                        "group": group,
                        "level": args.level,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print("notify: 未配置 Bark key，跳过推送（见 config/notify.example.yaml）")
        return 0

    payload = {
        "device_key": key,
        "title": args.title,
        "body": body,
        "group": group,
        "level": args.level,
    }
    if args.url:
        payload["url"] = args.url

    if args.dry_run:
        safe = {**payload, "device_key": key[:4] + "***"}
        print(json.dumps(safe, ensure_ascii=False, indent=2))
        return 0

    req = urllib.request.Request(
        f"{server}/push",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            print(f"notify: Bark 返回非 JSON：{raw[:200]}", file=sys.stderr)
            return 1
        if data.get("code") != 200:
            print(f"notify: Bark 返回失败：{data}", file=sys.stderr)
            return 1
        print("notify: 已发送")
        return 0
    except urllib.error.URLError as exc:
        print(f"notify: 网络错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"notify: 发送失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
