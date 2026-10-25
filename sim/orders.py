"""订单冻结与校验：订单写入后计算 SHA-256 记入 MANIFEST.json 并设为只读，执行前校验哈希。

orders/<交易日>/
  <账户>.draft.json   Agent 写的草稿（第二阶段）
  <账户>.json         风控校验后的冻结订单
  MANIFEST.json       各订单文件的哈希与冻结时间
"""
import datetime as dt
import hashlib
import json
import os
import stat
import subprocess
import time

from . import config


def _dir(trade_date: str):
    return config.orders_dir() / trade_date


def exists(trade_date: str, account: str) -> bool:
    return (_dir(trade_date) / f"{account}.json").exists()


def freeze(trade_date: str, account: str, doc: dict) -> str:
    d = _dir(trade_date)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{account}.json"
    if path.exists():
        raise FileExistsError(f"{path} 已冻结，不能覆盖")
    data = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(data)
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    digest = hashlib.sha256(data).hexdigest()
    mpath = d / "MANIFEST.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}
    manifest[account] = {"sha256": digest, "frozen_at": dt.datetime.now().isoformat(timespec="seconds")}
    if mpath.exists():
        os.chmod(mpath, stat.S_IRUSR | stat.S_IWUSR)
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(mpath, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return digest


def load_verified(trade_date: str, account: str) -> dict:
    d = _dir(trade_date)
    data = (d / f"{account}.json").read_bytes()
    manifest = json.loads((d / "MANIFEST.json").read_text(encoding="utf-8"))
    if hashlib.sha256(data).hexdigest() != manifest[account]["sha256"]:
        raise RuntimeError(f"{trade_date}/{account}.json 哈希不符：订单在冻结后被修改，拒绝执行")
    return json.loads(data)


def git_commit(message: str) -> bool:
    """把当天的订单与账本提交，留下不可抵赖的时间记录。

    返回 False 表示提交真的失败（调用方应让命令返回非 0，runlog 记 failed）；
    "没有改动"是正常情况，返回 True；不是 git 仓库时跳过，也返回 True。
    """
    home = config.home()
    if not (home / ".git").exists():
        return True
    paths = [p for p in ("orders", "accounts", "data/screen", "data/pool", "data/watchlist.json") if (home / p).exists()]
    add = subprocess.run(["git", "-C", str(home), "add", *paths], check=False, capture_output=True, text=True)
    if add.returncode != 0:
        print(f"git add 失败：{add.stderr.strip()}", flush=True)
        return False
    for attempt in range(4):
        r = subprocess.run(["git", "-C", str(home), "commit", "-m", message], check=False, capture_output=True, text=True)
        if r.returncode == 0:
            return True
        out = f"{r.stdout}\n{r.stderr}"
        if "nothing to commit" in out or "nothing added to commit" in out or "无文件要提交" in out:
            return True
        if "index.lock" in out and attempt < 3:  # 并发任务抢锁：等几秒重试
            time.sleep(2)
            continue
        print(f"git commit 失败：{(r.stderr or r.stdout).strip()}", flush=True)
        return False
    return False
