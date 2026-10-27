"""回测数据面板构建：Agent 选股策略 2024-10 ~ 2026-10 两年回测（Lead 用）。

产出（全部在 data/cache/backtest/，gitignored）：
- daily/<code>.csv            前复权日线 2024-01-02 ~ 2026-10-23（新浪 stock_zh_a_daily）
- index_sh000300.csv          沪深 300 收盘
- etf_sh510300.csv            底仓 ETF 日线
- panel.pkl                   long DataFrame: date, code, open, high, low, close, volume, amount
- meta.pkl                    每只代码: code, name, industry, pe_snapshot, pb, nmc, eps_implied, close_snapshot

用法：
    .venv/bin/python tools/bt_agent_data.py download [--workers 8] [--force]
    .venv/bin/python tools/bt_agent_data.py build
    .venv/bin/python tools/bt_agent_data.py all
"""
import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FEATURES = ROOT / "data" / "cache" / "pool" / "2026-10-23_features.csv"
OUT = ROOT / "data" / "cache" / "backtest"
DAILY = OUT / "daily"
START, END = "20240102", "20261023"
CUTOFF = "2026-10-23"


def _fetch_one(job):
    code, start, end = job
    p = DAILY / f"{code}.csv"
    if p.exists() and p.stat().st_size > 100:
        try:
            df = pd.read_csv(p)
            if len(df) > 50 and str(df["date"].iloc[0])[:10] <= "2024-01-10":
                return code, "cached", len(df)
        except Exception:
            pass
    import akshare as ak

    for attempt in range(3):
        try:
            df = ak.stock_zh_a_daily(symbol=code, start_date=start, end_date=end, adjust="qfq")
            if df is not None and len(df) > 0:
                break
        except Exception:
            df = None
        time.sleep(1.2 * (attempt + 1))
    if df is None or len(df) == 0:
        return code, "failed", 0
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    cols = [c for c in ("date", "open", "high", "low", "close", "volume", "amount", "turnover") if c in df.columns]
    tmp = p.with_suffix(".csv.tmp")
    df[cols].to_csv(tmp, index=False)
    tmp.replace(p)
    return code, "ok", len(df)


def download(workers: int = 8, force: bool = False):
    DAILY.mkdir(parents=True, exist_ok=True)
    feat = pd.read_csv(FEATURES, dtype={"code": str})
    codes = [str(c) for c in feat["code"].tolist()]
    print(f"[download] {len(codes)} codes -> {DAILY}")
    if force:
        for c in codes:
            (DAILY / f"{c}.csv").unlink(missing_ok=True)
    jobs = [(c, START, END) for c in codes]
    t0 = time.time()
    stats = {"ok": 0, "cached": 0, "failed": 0}
    failures = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, (code, status, n) in enumerate(ex.map(_fetch_one, jobs, chunksize=8), 1):
            stats[status] += 1
            if status == "failed":
                failures.append(code)
            if i % 200 == 0 or i == len(jobs):
                print(f"[download] {i}/{len(jobs)} ok={stats['ok']} cached={stats['cached']} "
                      f"failed={stats['failed']} {time.time() - t0:.0f}s", flush=True)
    if failures:
        (OUT / "download_failures.txt").write_text("\n".join(failures), encoding="utf-8")
    print(f"[download] done in {time.time() - t0:.1f}s, failed={len(failures)}"
          + (f": {failures[:20]}" if failures else ""), flush=True)
    return failures


def fetch_benchmarks(force: bool = False):
    import akshare as ak

    idx_p, etf_p = OUT / "index_sh000300.csv", OUT / "etf_sh510300.csv"
    if force or not idx_p.exists():
        df = ak.stock_zh_index_daily(symbol="sh000300")
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df = df[df["date"] <= CUTOFF]
        df[["date", "close"]].to_csv(idx_p, index=False)
        print(f"[benchmark] index {len(df)} rows")
    if force or not etf_p.exists():
        df = ak.fund_etf_hist_sina(symbol="sh510300")
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df = df[df["date"] <= CUTOFF]
        df[["date", "open", "high", "low", "close", "volume", "amount"]].to_csv(etf_p, index=False)
        print(f"[benchmark] etf {len(df)} rows")


def build():
    feat = pd.read_csv(FEATURES, dtype={"code": str})
    files = sorted(DAILY.glob("*.csv"))
    frames, missing = [], []
    codes = [str(c) for c in feat["code"].tolist()]
    have = {p.stem for p in files}
    for c in codes:
        if c not in have:
            missing.append(c)
            continue
        df = pd.read_csv(DAILY / f"{c}.csv")
        if len(df) < 130:
            missing.append(c)
            continue
        df["code"] = c
        frames.append(df)
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.sort_values(["date", "code"]).reset_index(drop=True)
    panel["date"] = panel["date"].astype(str)
    keep = [c for c in ("date", "code", "open", "high", "low", "close", "volume", "amount", "turnover") if c in panel.columns]
    panel = panel[keep]
    panel.to_pickle(OUT / "panel.pkl")

    meta = feat.copy()
    meta["code"] = meta["code"].astype(str)
    meta["eps_implied"] = meta["close"] / meta["pe"].where(meta["pe"] > 0)
    keep_meta = [c for c in ("code", "name", "industry", "pe", "pb", "nmc", "close", "eps_implied") if c in meta.columns]
    meta = meta[keep_meta].rename(columns={"pe": "pe_snapshot", "close": "close_snapshot"})
    meta.to_pickle(OUT / "meta.pkl")

    print(f"[build] panel rows={len(panel):,} codes={panel['code'].nunique()} "
          f"dates={panel['date'].nunique()} range={panel['date'].min()}~{panel['date'].max()}")
    print(f"[build] missing/short codes={len(missing)}" + (f" e.g. {missing[:10]}" if missing else ""))
    # 覆盖率：每年有数据的股票数 & 每日股票数分位
    cnt = panel.groupby("date")["code"].nunique()
    print(f"[build] per-date stocks: min={cnt.min()} p5={cnt.quantile(.05):.0f} "
          f"median={cnt.median():.0f} max={cnt.max()}")
    print("[build] written panel.pkl / meta.pkl")
    return panel, meta, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["download", "build", "all"])
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.cmd in ("download", "all"):
        fetch_benchmarks(force=a.force)
        download(workers=a.workers, force=a.force)
    if a.cmd in ("build", "all"):
        build()


if __name__ == "__main__":
    sys.exit(main() or 0)
