"""选股研究数据：全市场（当前在市，沪深，不含北交所）2020-01 ~ 2026-10 前复权日线。

产出（gitignored）：
- data/cache/full/daily/<code>.csv   date,open,high,low,close,volume,amount,outstanding_share,turnover
- data/cache/full/panel.pkl          长表，build 时合并

已知局限：新浪源拿不到已退市股票（2020 年后退市约数百只，多为小票/ST），存在退市幸存者偏差。

用法：
    .venv/bin/python tools/select_data.py download [--workers 8]
    .venv/bin/python tools/select_data.py build
"""
import argparse
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SPOT = ROOT / "data" / "cache" / "spot_20261027.csv"
OUT = ROOT / "data" / "cache" / "full"
DAILY = OUT / "daily"
START, END = "20200101", "20261031"


def codes() -> list[str]:
    spot = pd.read_csv(SPOT, dtype={"代码": str})
    return [c for c in spot["代码"] if c[:2] in ("sh", "sz")]


def fetch_one(code: str):
    p = DAILY / f"{code}.csv"
    if p.exists() and p.stat().st_size > 100:
        return code, "cached"
    import akshare as ak

    for attempt in range(3):
        try:
            df = ak.stock_zh_a_daily(symbol=code, start_date=START, end_date=END, adjust="qfq")
            if df is not None and len(df):
                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                tmp = p.with_suffix(".tmp")
                df.to_csv(tmp, index=False)
                tmp.replace(p)
                return code, "ok"
        except Exception:
            pass
        time.sleep(1.5 * (attempt + 1))
    return code, "failed"


def download(workers: int):
    DAILY.mkdir(parents=True, exist_ok=True)
    cs = codes()
    stats = {"ok": 0, "cached": 0, "failed": 0}
    t0 = time.time()
    with ProcessPoolExecutor(workers) as ex:
        for i, (c, s) in enumerate(ex.map(fetch_one, cs, chunksize=8), 1):
            stats[s] += 1
            if i % 500 == 0:
                print(f"{i}/{len(cs)} {stats} {time.time() - t0:.0f}s", flush=True)
    print("done", stats, flush=True)


def build():
    frames = []
    for p in sorted(DAILY.glob("*.csv")):
        df = pd.read_csv(p)
        df["code"] = p.stem
        frames.append(df)
    panel = pd.concat(frames, ignore_index=True)
    panel.to_pickle(OUT / "panel.pkl")
    print(panel.shape, panel.code.nunique(), panel.date.min(), panel.date.max())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["download", "build"])
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    download(a.workers) if a.cmd == "download" else build()
