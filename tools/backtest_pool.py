"""候选池通道选股能力离线回测（只评估选股，不模拟下单）。

回答三个问题：
1) `sim/pool.py` 的六个召回通道，前向 5/20/60 日是否有稳定的横截面超额？
2) momentum / breakout 选出来的"极端上涨"名字是不是反而跑输？"多通道共振"是不是动量代理？
3) 换几个替代通道（反转、缩量、52 周新高、行业中性动量），有没有更值得并入 v1.1 的？

口径（详见 evolution/analysis/2026-10-25_pool_channel_backtest.md）：
- 信号日 T：2026-04-01 ~ 2026-09-18 的交易日；特征只用 T 及之前的前复权日线（≥130 个观测），逐只按自身
  交易日序列计算，等价于把 sim/pool.py 的 features() 在每个历史交易日重算一遍。
- 通道：按 config/strategy.yaml 的 pool 参数重建 momentum/trend/pullback/breakout；defensive 没有历史 PE，
  用 lowvol（close>MA120 按 vol_60 升序）替代；sector 用当前 industry 标签（轻微前视）。
- 收益：T+1 收盘买入，持有 h 个交易日（h=5/20/60），前复权收盘价；超额 = 个股收益 − 当日合格股票横截面中位数。
- 合格池近似 risk.yaml 的 universe：上市观测≥60、20 日均成交额≥1 亿、T 日涨幅≤7%、5 日涨幅≤25%、非 ST（当前名称）。

用法：
    .venv/bin/python tools/backtest_pool.py download [--workers 6] [--force]
    .venv/bin/python tools/backtest_pool.py run
    .venv/bin/python tools/backtest_pool.py all        # 下载 + 回测
"""
import argparse
import datetime as dt
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DAILY = ROOT / "data" / "cache" / "daily"
FEATURES = ROOT / "data" / "cache" / "pool" / "2026-10-23_features.csv"
OUT = ROOT / "evolution" / "analysis"
INDEX_CACHE = DAILY / "_index_sh000300.csv"

START = "2025-06-01"
END = "2026-10-23"
SIG_START, SIG_END = "2026-04-01", "2026-09-18"
HORIZONS = (5, 20, 60)
PER_CHANNEL = 25

# 与 config/strategy.yaml 的 pool: 段保持一致（脚本只读配置的默认值，改配置时同步这里）
POOL_CFG = {
    "per_channel": 25,
    "defensive_max_pe": 25,
    "breakout_amt_ratio": 1.5,
    "sector_top": 5,
    "sector_per": 4,
    "sector_min_size": 8,
}

CHANNELS = ["momentum", "trend", "pullback", "breakout", "sector", "lowvol"]
ALTS = ["reversal", "squeeze", "high52w", "ind_mom"]
LABELS = {
    "momentum": "momentum 短期动量",
    "trend": "trend 中期趋势",
    "pullback": "pullback 趋势回调",
    "breakout": "breakout 放量突破",
    "sector": "sector 强势行业",
    "lowvol": "lowvol 低波(替 defensive)",
    "reversal": "reversal 短期反转",
    "squeeze": "squeeze 缩量整理",
    "high52w": "high52w 52周新高",
    "ind_mom": "ind_mom 行业中性动量",
    "__pool__": "候选池全体",
    "__s_mom_top15__": "池内 s_mom 前15",
}


# ---------------- 下载 ----------------
def _download_one(job):
    code, start, end = job
    p = DAILY / f"{code}.csv"
    if p.exists() and p.stat().st_size > 0:
        return code, "cached", 0
    import akshare as ak

    for attempt in range(2):
        try:
            df = ak.stock_zh_a_daily(symbol=code, start_date=start, end_date=end, adjust="qfq")
            break
        except Exception:
            df = None
            if attempt == 0:
                time.sleep(1.5)
    if df is None or len(df) == 0:
        return code, "failed", 0
    df = df.copy()
    df["date"] = df["date"].astype(str).str[:10]
    cols = [c for c in ("date", "open", "high", "low", "close", "volume", "amount", "turnover") if c in df.columns]
    tmp = p.with_suffix(".csv.tmp")
    df[cols].to_csv(tmp, index=False)
    tmp.replace(p)
    return code, "ok", len(df)


def download(workers: int = 6, force: bool = False):
    DAILY.mkdir(parents=True, exist_ok=True)
    feat = pd.read_csv(FEATURES, dtype={"code": str})
    codes = feat["code"].tolist()
    if force:
        for c in codes:
            (DAILY / f"{c}.csv").unlink(missing_ok=True)
    jobs = [(c, START.replace("-", ""), END.replace("-", "")) for c in codes]
    t0 = time.time()
    stats = {"ok": 0, "cached": 0, "failed": 0}
    failures = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, (code, status, n) in enumerate(ex.map(_download_one, jobs, chunksize=10), 1):
            stats[status] += 1
            if status == "failed":
                failures.append(code)
            if i % 200 == 0 or i == len(jobs):
                print(f"[download] {i}/{len(jobs)} ok={stats['ok']} cached={stats['cached']} "
                      f"failed={stats['failed']} {time.time() - t0:.0f}s", flush=True)
    # 指数：市场状态判定与交易日历
    if force or not INDEX_CACHE.exists():
        import akshare as ak
        idx = ak.stock_zh_index_daily(symbol="sh000300")
        idx["date"] = idx["date"].astype(str).str[:10]
        idx[["date", "close"]].to_csv(INDEX_CACHE, index=False)
    print(f"[download] done in {time.time() - t0:.1f}s, failed={len(failures)}"
          + (f": {failures[:20]}" if failures else ""), flush=True)
    return failures


# ---------------- 特征矩阵 ----------------
def load_features(meta: pd.DataFrame, signal_dates: list) -> pd.DataFrame:
    """逐只股票按自身交易日序列重算 features()，只保留 signal_dates 上的行。"""
    rows = []
    sig = set(signal_dates)
    codes = meta["code"].tolist()
    missing = 0
    for code in codes:
        p = DAILY / f"{code}.csv"
        if not p.exists():
            missing += 1
            continue
        try:
            df = pd.read_csv(p)
        except Exception:
            missing += 1
            continue
        if len(df) < 130:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        c, h, a = df["close"].astype(float), df["high"].astype(float), df["amount"].astype(float)
        ret = c.pct_change()
        f = pd.DataFrame({"date": df["date"]})
        f["close"] = c
        f["listed_days"] = np.arange(1, len(df) + 1, dtype=float)
        f["chg_1d"] = ret
        f["chg_5d"] = c / c.shift(5) - 1
        f["ret_20"] = c / c.shift(20) - 1
        f["ret_60"] = c / c.shift(60) - 1
        f["ret_120"] = c / c.shift(120) - 1
        f["vol_20"] = ret.rolling(20).std() * 250 ** 0.5
        f["vol_60"] = ret.rolling(60).std() * 250 ** 0.5
        f["ma20"] = c.rolling(20).mean()
        f["ma60"] = c.rolling(60).mean()
        f["ma120"] = c.rolling(120).mean()
        hi60 = h.rolling(60).max()
        f["dd_60"] = c / hi60 - 1
        f["near_high_60"] = c >= 0.97 * hi60
        f["avg_amount"] = a.rolling(20).mean()
        f["amt_ratio"] = a.rolling(5).mean() / a.rolling(20).mean()
        f["hi250"] = h.rolling(250, min_periods=200).max()
        f["code"] = code
        rows.append(f.loc[f["date"].isin(sig)])
    print(f"[features] {len(rows)} stocks kept, {missing} missing daily files", flush=True)
    panel = pd.concat(rows, ignore_index=True)
    panel["date"] = panel["date"].astype(str).str[:10]
    return panel


def close_matrix(meta: pd.DataFrame, dates: list) -> pd.DataFrame:
    out = {}
    for code in meta["code"]:
        p = DAILY / f"{code}.csv"
        if not p.exists():
            continue
        d = pd.read_csv(p)
        if len(d) == 0:
            continue
        s = d.set_index(d["date"].astype(str).str[:10])["close"].astype(float)
        out[code] = s.reindex(dates)
    m = pd.DataFrame(out, index=dates)
    return m


# ---------------- 通道（与 pool.channels 同口径） ----------------
def build_channels(f: pd.DataFrame, cfg: dict = POOL_CFG) -> dict:
    """f：某日合格股票横截面。返回 {通道: DataFrame(code,score)，按得分降序}。"""
    n = cfg["per_channel"]
    out = {}
    fc = f
    trend_short = (fc["close"] > fc["ma20"]) & (fc["ma20"] > fc["ma60"])
    trend_mid = (fc["close"] > fc["ma120"]) & (fc["ma60"] > fc["ma120"])
    s_mom = fc["ret_20"] / fc["vol_20"].where(fc["vol_20"] > 0)
    s_trend = fc["ret_120"] / fc["vol_60"].where(fc["vol_60"] > 0)

    def top(mask, score, asc=False, k=n):
        sub = pd.DataFrame({"code": fc["code"].to_numpy()[mask.to_numpy()],
                            "score": score[mask].to_numpy()})
        sub = sub.dropna(subset=["score"]).sort_values("score", ascending=asc).head(k)
        return sub.reset_index(drop=True)

    out["momentum"] = top(trend_short, s_mom)
    out["trend"] = top(trend_mid, s_trend)
    mask = trend_mid & fc["dd_60"].between(-0.20, -0.08) & (fc["chg_5d"] < 0)
    out["pullback"] = top(mask, fc["ret_120"])
    mask = fc["near_high_60"].astype(bool) & (fc["amt_ratio"] >= cfg["breakout_amt_ratio"])
    out["breakout"] = top(mask, fc["amt_ratio"])
    # defensive 没有历史 PE → lowvol
    out["lowvol"] = top(fc["close"] > fc["ma120"], fc["vol_60"], asc=True)
    # sector：当前 industry 标签（轻微前视）
    sec = []
    if fc["industry"].notna().any():
        g = fc[fc["industry"].notna()].groupby("industry")
        med = g["ret_20"].median()[g.size() >= cfg["sector_min_size"]].sort_values(ascending=False)
        for ind in med.head(cfg["sector_top"]).index:
            m = trend_short & (fc["industry"] == ind)
            sub = pd.DataFrame({"code": fc["code"].to_numpy()[m.to_numpy()],
                                "score": s_mom[m].to_numpy(),
                                "industry": ind})
            sub = sub.dropna(subset=["score"]).sort_values("score", ascending=False).head(cfg["sector_per"])
            sec.append(sub)
    out["sector"] = (pd.concat(sec, ignore_index=True) if sec
                     else pd.DataFrame(columns=["code", "score", "industry"]))

    # ---- 替代通道 ----
    mask = (fc["close"] > fc["ma120"]) & fc["ret_20"].notna()
    out["reversal"] = top(mask, -fc["ret_20"])  # 升序 = 得分取负
    mask = (fc["amt_ratio"] < 0.8) & (fc["close"] > fc["ma60"]) & fc["amt_ratio"].notna()
    out["squeeze"] = top(mask, -fc["amt_ratio"])  # 缩量越极致分越高
    if fc["hi250"].notna().any():
        mask = fc["close"] >= 0.98 * fc["hi250"]
        out["high52w"] = top(mask, fc["close"] / fc["hi250"])
    else:
        out["high52w"] = pd.DataFrame(columns=["code", "score"])
    # 行业中性动量：行业内 ret_20/vol_20 的百分位排名，取全局最高
    if fc["industry"].notna().any():
        g = fc[fc["industry"].notna()].copy()
        g["s_mom"] = g["ret_20"] / g["vol_20"].where(g["vol_20"] > 0)
        g = g.dropna(subset=["s_mom"])
        cnt = g.groupby("industry")["s_mom"].transform("count")
        g = g[cnt >= cfg["sector_min_size"]]
        g["score"] = g.groupby("industry")["s_mom"].rank(pct=True)
        g = g.dropna(subset=["score"]).sort_values(["score", "s_mom"], ascending=False).head(n)
        out["ind_mom"] = g[["code", "score"]].reset_index(drop=True)
    else:
        out["ind_mom"] = pd.DataFrame(columns=["code", "score"])

    # 记录每条通道的"候选域"（过滤后、截断前），供 IC 用
    return out


def channel_universe(f: pd.DataFrame, name: str, cfg: dict = POOL_CFG) -> pd.DataFrame:
    """通道过滤域（截断前），用于计算 IC：score 越高越应进池（asc 通道已取负）。"""
    trend_short = (f["close"] > f["ma20"]) & (f["ma20"] > f["ma60"])
    trend_mid = (f["close"] > f["ma120"]) & (f["ma60"] > f["ma120"])
    s_mom = f["ret_20"] / f["vol_20"].where(f["vol_20"] > 0)
    s_trend = f["ret_120"] / f["vol_60"].where(f["vol_60"] > 0)
    if name == "momentum":
        return pd.DataFrame({"code": f.loc[trend_short, "code"], "score": s_mom[trend_short]})
    if name == "trend":
        return pd.DataFrame({"code": f.loc[trend_mid, "code"], "score": s_trend[trend_mid]})
    if name == "pullback":
        m = trend_mid & f["dd_60"].between(-0.20, -0.08) & (f["chg_5d"] < 0)
        return pd.DataFrame({"code": f.loc[m, "code"], "score": f.loc[m, "ret_120"]})
    if name == "breakout":
        m = f["near_high_60"].astype(bool) & (f["amt_ratio"] >= cfg["breakout_amt_ratio"])
        return pd.DataFrame({"code": f.loc[m, "code"], "score": f.loc[m, "amt_ratio"]})
    if name == "lowvol":
        m = f["close"] > f["ma120"]
        return pd.DataFrame({"code": f.loc[m, "code"], "score": -f.loc[m, "vol_60"]})
    if name == "sector":
        if not f["industry"].notna().any():
            return pd.DataFrame(columns=["code", "score"])
        g = f[f["industry"].notna()].groupby("industry")
        med = g["ret_20"].median()[g.size() >= cfg["sector_min_size"]].sort_values(ascending=False)
        top_ind = set(med.head(cfg["sector_top"]).index)
        m = trend_short & f["industry"].isin(top_ind)
        return pd.DataFrame({"code": f.loc[m, "code"], "score": s_mom[m]})
    if name == "reversal":
        m = (f["close"] > f["ma120"]) & f["ret_20"].notna()
        return pd.DataFrame({"code": f.loc[m, "code"], "score": -f.loc[m, "ret_20"]})
    if name == "squeeze":
        m = (f["amt_ratio"] < 0.8) & (f["close"] > f["ma60"]) & f["amt_ratio"].notna()
        return pd.DataFrame({"code": f.loc[m, "code"], "score": -f.loc[m, "amt_ratio"]})
    if name == "high52w":
        m = f["hi250"].notna() & (f["close"] >= 0.98 * f["hi250"])
        return pd.DataFrame({"code": f.loc[m, "code"], "score": (f["close"] / f["hi250"])[m]})
    if name == "ind_mom":
        g = f[f["industry"].notna()].copy()
        g["s_mom"] = g["ret_20"] / g["vol_20"].where(g["vol_20"] > 0)
        g = g.dropna(subset=["s_mom"])
        cnt = g.groupby("industry")["s_mom"].transform("count")
        g = g[cnt >= cfg["sector_min_size"]]
        g["score"] = g.groupby("industry")["s_mom"].rank(pct=True)
        return g[["code", "score"]]
    raise KeyError(name)


# ---------------- 回测主流程 ----------------
def eligible_mask(f: pd.DataFrame) -> pd.Series:
    names = f["name"].astype(str)
    num = f["code"].str[2:]
    keep = ~(f["code"].str.startswith("bj") | num.str.startswith(("688", "689", "8", "4", "92")))
    keep &= ~names.str.upper().str.contains("ST|退", regex=True)
    keep &= f["listed_days"] >= 60
    keep &= f["avg_amount"] >= 1e8
    keep &= f["chg_1d"] <= 0.07
    keep &= f["chg_5d"] <= 0.25
    keep &= f["close"] > 0
    return keep.fillna(False)


def run_backtest():
    t0 = time.time()
    meta = pd.read_csv(FEATURES, dtype={"code": str})
    meta = meta[["code", "name", "industry", "pe", "nmc"]]
    idx = pd.read_csv(INDEX_CACHE)
    idx["date"] = idx["date"].astype(str).str[:10]
    idx = idx[(idx["date"] >= START) & (idx["date"] <= END)].reset_index(drop=True)
    idx["ma120"] = idx["close"].rolling(120).mean()
    dates = idx["date"].tolist()
    regime = dict(zip(idx["date"], np.where(idx["close"] > idx["ma120"], "bull", "bear")))
    signal_dates = [d for d in dates if SIG_START <= d <= SIG_END]
    print(f"[backtest] {len(dates)} trading days, {len(signal_dates)} signal days {signal_dates[0]}..{signal_dates[-1]}", flush=True)

    panel = load_features(meta, signal_dates)
    panel = panel.merge(meta[["code", "name", "industry"]], on="code", how="left")
    C = close_matrix(meta, dates)
    pos = {d: i for i, d in enumerate(dates)}

    n = len(dates)
    rows = []
    ic_rows = []
    pool_rows = []
    for sd in signal_dates:
        i = pos[sd]
        f = panel[panel["date"] == sd].copy()
        if len(f) == 0:
            continue
        m = eligible_mask(f)
        f = f[m].reset_index(drop=True)
        if len(f) < 30:
            continue
        # 前向收益（T+1 收盘买入）
        def fwd(h):
            if i + 1 + h >= n:
                return pd.Series(np.nan, index=f.index)
            buy = C.iloc[i + 1]
            sell = C.iloc[i + 1 + h]
            return (sell / buy - 1).reindex(f["code"]).to_numpy()

        fwd_df = pd.DataFrame({f"fwd_{h}": fwd(h) for h in HORIZONS})
        # 基准：当日合格股票横截面中位数（每个 horizon 各自的有效样本）
        bench = {h: fwd_df[f"fwd_{h}"].median() for h in HORIZONS}
        for h in HORIZONS:
            fwd_df[f"exc_{h}"] = fwd_df[f"fwd_{h}"] - bench[h]
        f = pd.concat([f.reset_index(drop=True), fwd_df], axis=1)
        f["s_mom"] = f["ret_20"] / f["vol_20"].where(f["vol_20"] > 0)

        chans = build_channels(f)
        # 共振：六个通道的并集，n_channels 计数
        hit = {}
        for ch in CHANNELS:
            for code in chans[ch]["code"]:
                hit[code] = hit.get(code, 0) + 1
        f["n_channels"] = f["code"].map(hit).fillna(0).astype(int)

        for ch in CHANNELS + ALTS:
            dfc = chans[ch]
            if len(dfc) == 0:
                continue
            sub = f[f["code"].isin(dfc["code"])].copy()
            rank = {c: k + 1 for k, c in enumerate(dfc["code"])}
            score = dict(zip(dfc["code"], dfc["score"]))
            sub["channel"] = ch
            sub["rank"] = sub["code"].map(rank)
            sub["score"] = sub["code"].map(score)
            rows.append(sub[["date", "channel", "code", "name", "industry", "rank", "score",
                             "close", "ret_20", "vol_20", "n_channels", "s_mom",
                             "fwd_5", "fwd_20", "fwd_60", "exc_5", "exc_20", "exc_60"]])

            # IC：通道过滤域内 score 与 fwd 的 Spearman
            uni = channel_universe(f, ch)
            uni = uni.dropna(subset=["score"])
            for h in HORIZONS:
                col = f"fwd_{h}"
                d2 = uni.merge(f[["code", col]], on="code", how="inner").dropna()
                if len(d2) >= 10 and d2["score"].nunique() > 1 and d2[col].nunique() > 1:
                    ic = _spearman(d2["score"], d2[col])
                else:
                    ic = np.nan
                ic_rows.append({"date": sd, "channel": ch, "horizon": h, "ic": ic,
                                "n_universe": len(d2), "regime": regime.get(sd)})

        # 候选池全体 + s_mom 前 15
        pool = f[f["code"].isin(hit)].copy()
        if len(pool):
            pool["channel"] = "__pool__"
            pool["score"] = pool["s_mom"]
            pool["rank"] = pool["s_mom"].rank(ascending=False, method="first")
            pool_rows.append(pool)
            top15 = pool.nlargest(15, "s_mom").copy()
            top15["channel"] = "__s_mom_top15__"
            rows.append(top15[["date", "channel", "code", "name", "industry", "rank", "score",
                               "close", "ret_20", "vol_20", "n_channels", "s_mom",
                               "fwd_5", "fwd_20", "fwd_60", "exc_5", "exc_20", "exc_60"]])
            rows.append(pool[["date", "channel", "code", "name", "industry", "rank", "score",
                              "close", "ret_20", "vol_20", "n_channels", "s_mom",
                              "fwd_5", "fwd_20", "fwd_60", "exc_5", "exc_20", "exc_60"]])

    raw = pd.concat(rows, ignore_index=True)
    raw["regime"] = raw["date"].map(regime)
    raw = raw.sort_values(["date", "channel", "rank"])
    OUT.mkdir(parents=True, exist_ok=True)
    raw.to_csv(OUT / "pool_channel_backtest_raw.csv", index=False, encoding="utf-8")
    ic = pd.DataFrame(ic_rows)
    ic.to_csv(OUT / "pool_channel_backtest_ic.csv", index=False, encoding="utf-8")
    print(f"[backtest] raw {raw.shape} ic {ic.shape} in {time.time() - t0:.0f}s", flush=True)
    return raw, ic, pd.concat(pool_rows, ignore_index=True) if pool_rows else pd.DataFrame()


# ---------------- 指标汇总 ----------------
def _spearman(a, b) -> float:
    """秩相关 = 秩上的 Pearson；避免依赖 scipy。"""
    x, y = pd.Series(a), pd.Series(b)
    m = x.notna() & y.notna()
    if m.sum() < 5 or x[m].nunique() < 2 or y[m].nunique() < 2:
        return float("nan")
    return float(x[m].rank().corr(y[m].rank()))


def _tstat(x):
    x = pd.Series(x).dropna()
    if len(x) < 3:
        return np.nan
    sd = x.std(ddof=1)
    return x.mean() / (sd / np.sqrt(len(x))) if sd > 0 else np.nan


def channel_summary(raw: pd.DataFrame, ic: pd.DataFrame, channels: list) -> pd.DataFrame:
    out = []
    for ch in channels:
        d = raw[raw["channel"] == ch]
        if len(d) == 0:
            continue
        for h in HORIZONS:
            s = d.dropna(subset=[f"exc_{h}"])
            if len(s) == 0:
                out.append({"channel": ch, "horizon": h, "days": 0})
                continue
            daily = s.groupby("date")[f"exc_{h}"].mean()
            icd = ic[(ic["channel"] == ch) & (ic["horizon"] == h)]["ic"].dropna()
            out.append({
                "channel": ch, "horizon": h, "days": len(daily), "obs": len(s),
                "mean_exc": s[f"exc_{h}"].mean(), "med_exc": s[f"exc_{h}"].median(),
                "win": (s[f"exc_{h}"] > 0).mean(), "mean_ret": s[f"fwd_{h}"].mean(),
                "t_day": _tstat(daily), "ic": icd.mean(), "ic_t": _tstat(icd), "ic_days": len(icd),
            })
    return pd.DataFrame(out)


def regime_summary(raw: pd.DataFrame, channels: list) -> pd.DataFrame:
    out = []
    for ch in channels:
        for h in HORIZONS:
            for rg in ("bull", "bear"):
                s = raw[(raw["channel"] == ch) & (raw["regime"] == rg)].dropna(subset=[f"exc_{h}"])
                if len(s) == 0:
                    continue
                daily = s.groupby("date")[f"exc_{h}"].mean()
                out.append({"channel": ch, "horizon": h, "regime": rg, "days": len(daily),
                            "obs": len(s), "mean_exc": s[f"exc_{h}"].mean(),
                            "win": (s[f"exc_{h}"] > 0).mean(), "t_day": _tstat(daily)})
    return pd.DataFrame(out)


def resonance_summary(raw: pd.DataFrame) -> pd.DataFrame:
    pool = raw[raw["channel"] == "__pool__"].copy()
    if len(pool) == 0:
        return pd.DataFrame()
    pool["n_group"] = pool["n_channels"].clip(upper=3).astype(int).astype(str).replace({"3": "3+"})
    out = []
    for g, d in pool.groupby("n_group"):
        for h in HORIZONS:
            s = d.dropna(subset=[f"exc_{h}"])
            if len(s) == 0:
                continue
            daily = s.groupby("date")[f"exc_{h}"].mean()
            out.append({"n_channels": g, "horizon": h, "days": len(daily), "obs": len(s),
                        "mean_exc": s[f"exc_{h}"].mean(), "med_exc": s[f"exc_{h}"].median(),
                        "win": (s[f"exc_{h}"] > 0).mean(), "t_day": _tstat(daily)})
    return pd.DataFrame(out)


def resonance_corr(raw: pd.DataFrame) -> dict:
    pool = raw[raw["channel"] == "__pool__"]
    res = {}
    for col in ("ret_20", "fwd_20", "exc_20"):
        cors = [_spearman(x["n_channels"], x[col])
                for _, x in pool.dropna(subset=[col]).groupby("date") if len(x) > 5]
        res[col] = (float(np.nanmean(cors)) if cors else np.nan, len(cors))
    return res


def quintile_summary(raw: pd.DataFrame, ch: str) -> pd.DataFrame:
    d = raw[(raw["channel"] == ch)].dropna(subset=["ret_20"]).copy()
    if len(d) == 0:
        return pd.DataFrame()
    def _q(s):
        if len(s) < 5:
            return pd.Series(np.nan, index=s.index)
        return np.ceil(s.rank(method="first") / len(s) * 5).clip(1, 5)
    d["q"] = d.groupby("date")["ret_20"].transform(_q)
    d = d.dropna(subset=["q"])
    d["q"] = d["q"].astype(int).astype(str)
    out = []
    for q, g in d.groupby("q", observed=True):
        row = {"q": str(q), "n": len(g), "ret_20": g["ret_20"].mean()}
        for h in HORIZONS:
            s = g.dropna(subset=[f"exc_{h}"])
            row[f"exc_{h}"] = s[f"exc_{h}"].mean()
            row[f"win_{h}"] = (s[f"exc_{h}"] > 0).mean()
        out.append(row)
    return pd.DataFrame(out)


def extreme_corr(raw: pd.DataFrame, ch: str) -> dict:
    d = raw[(raw["channel"] == ch)].dropna(subset=["ret_20"])
    res = {}
    for h in HORIZONS:
        cors = [_spearman(x["ret_20"], x[f"exc_{h}"])
                for _, x in d.dropna(subset=[f"exc_{h}"]).groupby("date") if len(x) > 5]
        res[h] = float(np.nanmean(cors)) if cors else np.nan
    return res


def _md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False)


def pct(x):
    return f"{x * 100:+.1f}%"


def report():
    raw = pd.read_csv(OUT / "pool_channel_backtest_raw.csv", dtype={"code": str})
    ic = pd.read_csv(OUT / "pool_channel_backtest_ic.csv")
    chans = CHANNELS + ALTS
    summ = channel_summary(raw, ic, chans + ["__pool__", "__s_mom_top15__"])
    reg = regime_summary(raw, chans + ["__pool__", "__s_mom_top15__"])
    reso = resonance_summary(raw)
    rcorr = resonance_corr(raw)

    def fmt(s, horizons=HORIZONS):
        s = s.copy()
        s["通道"] = s["channel"].map(LABELS).fillna(s["channel"])
        s["h"] = s["horizon"].map(lambda h: f"{h}日")
        s["日数"] = s["days"]
        s["平均超额"] = s["mean_exc"].map(pct)
        s["中位超额"] = s["med_exc"].map(pct)
        s["胜率"] = s["win"].map(lambda x: f"{x * 100:.0f}%")
        s["平均收益"] = s["mean_ret"].map(pct)
        s["近似t"] = s["t_day"].map(lambda x: f"{x:.1f}")
        s["IC"] = s["ic"].map(lambda x: f"{x:.3f}")
        s["IC_t"] = s["ic_t"].map(lambda x: f"{x:.1f}")
        return s[["通道", "h", "日数", "平均超额", "中位超额", "胜率", "平均收益", "近似t", "IC", "IC_t"]]

    lines = []
    lines.append("## 表 A1 现有六通道（口径：T+1 收盘买入，超额相对当日合格股票中位数）\n")
    lines.append(_md(fmt(summ[summ["channel"].isin(CHANNELS)])))
    lines.append("\n\n## 表 A2 替代通道\n")
    lines.append(_md(fmt(summ[summ["channel"].isin(ALTS)])))
    lines.append("\n\n## 表 A3 候选池全体 / s_mom 前 15\n")
    lines.append(_md(fmt(summ[summ["channel"].isin(["__pool__", "__s_mom_top15__"])])))

    pool = raw[raw["channel"] == "__pool__"]
    if len(pool):
        size = pool.groupby("date").size()
        lines.append(f"\n\n候选池规模：日均 {size.mean():.1f} 只（{size.min()}~{size.max()}），"
                     f"覆盖 {len(size)} 个信号日")
    # 各 horizon 实际样本区间
    for h in HORIZONS:
        s = raw[raw["channel"] == "__pool__"].dropna(subset=[f"exc_{h}"])
        if len(s):
            lines.append(f"\n\n{h} 日 horizon 实际信号日：{s['date'].min()} ~ {s['date'].max()}，{s['date'].nunique()} 天")

    # 市场状态（20 日为主）
    r20 = reg[reg["horizon"] == 20].copy()
    wide = r20.pivot_table(index="channel", columns="regime", values="mean_exc", aggfunc="first")
    days = r20.pivot_table(index="channel", columns="regime", values="days", aggfunc="first")
    lines.append("\n\n## 表 B 市场状态分组（20 日超额，牛=沪深300 收盘>120 日均线）\n")
    tab = pd.DataFrame({
        "通道": [LABELS.get(c, c) for c in wide.index],
        "牛 平均超额": [pct(wide.loc[c].get("bull", np.nan)) for c in wide.index],
        "牛 日数": [int(days.loc[c].get("bull", 0)) for c in wide.index],
        "熊 平均超额": [pct(wide.loc[c].get("bear", np.nan)) for c in wide.index],
        "熊 日数": [int(days.loc[c].get("bear", 0)) for c in wide.index],
    })
    lines.append(_md(tab))

    # 共振
    r = reso.copy()
    r["n_channels"] = r["n_channels"]
    r["h"] = r["horizon"].map(lambda h: f"{h}日")
    r["样本"] = r["obs"]
    r["平均超额"] = r["mean_exc"].map(pct)
    r["中位超额"] = r["med_exc"].map(pct)
    r["胜率"] = r["win"].map(lambda x: f"{x * 100:.0f}%")
    r["近似t"] = r["t_day"].map(lambda x: f"{x:.1f}")
    lines.append("\n\n## 表 C 多通道共振（候选池内，n_channels=1/2/3+）\n")
    lines.append(_md(r[["n_channels", "h", "样本", "平均超额", "中位超额", "胜率", "近似t"]]))
    lines.append(f"\n\n共振数 vs 20 日动量 Spearman（按日平均）：{rcorr['ret_20'][0]:.3f}（{rcorr['ret_20'][1]} 天）；"
                 f"vs 20 日前向超额：{rcorr['exc_20'][0]:.3f}；vs 20 日前向收益：{rcorr['fwd_20'][0]:.3f}")

    # 极端上涨
    lines.append("\n\n## 表 D momentum 内按 ret_20 五分组（Q5=涨得最多）\n")
    q = quintile_summary(raw, "momentum")
    qt = pd.DataFrame({
        "组": q["q"], "n": q["n"], "平均 ret_20": q["ret_20"].map(pct),
        "5日超额": q["exc_5"].map(pct), "20日超额": q["exc_20"].map(pct),
        "60日超额": q["exc_60"].map(pct), "20日胜率": q["win_20"].map(lambda x: f"{x * 100:.0f}%"),
    })
    lines.append(_md(qt))
    ec = extreme_corr(raw, "momentum")
    lines.append(f"\n\nmomentum 内 ret_20 与超额 Spearman：5日 {ec[5]:.3f}，20日 {ec[20]:.3f}，60日 {ec[60]:.3f}")
    qb = quintile_summary(raw, "breakout")
    if len(qb):
        lines.append("\n\n## 表 D2 breakout 内按 ret_20 五分组\n")
        qbt = pd.DataFrame({
            "组": qb["q"], "n": qb["n"], "平均 ret_20": qb["ret_20"].map(pct),
            "5日超额": qb["exc_5"].map(pct), "20日超额": qb["exc_20"].map(pct),
            "60日超额": qb["exc_60"].map(pct),
        })
        lines.append(_md(qbt))
        eb = extreme_corr(raw, "breakout")
        lines.append(f"\n\nbreakout 内 ret_20 与超额 Spearman：5日 {eb[5]:.3f}，20日 {eb[20]:.3f}，60日 {eb[60]:.3f}")

    # 月度稳定性（20 日超额，按信号日所在月）
    pivot = raw[raw["channel"].isin(chans)].dropna(subset=["exc_20"]).groupby(
        [raw["date"].str[:7], "channel"])["exc_20"].mean().unstack()
    if len(pivot):
        mo = pd.DataFrame({"月份": pivot.index})
        for c in pivot.columns:
            mo[LABELS.get(c, c).split(" ")[0]] = [pct(v) for v in pivot[c]]
        lines.append("\n\n## 表 E 月度稳定性（20 日平均超额，按信号日所在月）\n")
        lines.append(_md(mo))

    # 非重叠 cohort（每 h 天一批）
    order = {d: i for i, d in enumerate(sorted(raw["date"].unique()))}
    lines.append("\n\n## 表 F 非重叠 cohort（每 h 个信号日一批，20 日超额）\n")
    coh = []
    for ch in chans + ["__pool__", "__s_mom_top15__"]:
        d = raw[raw["channel"] == ch].dropna(subset=["exc_20"]).copy()
        if len(d) == 0:
            continue
        d["cohort"] = d["date"].map(order) // 20
        g = d.groupby("cohort")["exc_20"].mean()
        coh.append({"通道": LABELS.get(ch, ch), "批数": len(g), "正收益批数": int((g > 0).sum()),
                    "均值": pct(g.mean()),
                    "各批": " / ".join(pct(v) for v in g.values)})
    lines.append(_md(pd.DataFrame(coh)))

    # 风格同质性：日度 20 日超额的相关系数
    sel = ["momentum", "trend", "pullback", "breakout", "sector", "lowvol", "squeeze", "high52w", "__pool__"]
    piv = raw[raw["channel"].isin(sel)].dropna(subset=["exc_20"]).groupby(
        ["date", "channel"])["exc_20"].mean().unstack()
    if len(piv):
        cor = piv.corr().round(2)
        cor.index = [LABELS.get(c, c).split(" ")[0] for c in cor.index]
        cor.columns = [LABELS.get(c, c).split(" ")[0] for c in cor.columns]
        lines.append("\n\n## 表 G 通道日度 20 日超额的相关系数（风格同质性）\n")
        lines.append(_md(cor.reset_index().rename(columns={"index": ""})))

    txt = "\n".join(lines)
    (OUT / "pool_channel_backtest_tables.md").write_text(txt, encoding="utf-8")
    print(txt)
    return summ, reg, reso


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["download", "run", "report", "all", "tables"], nargs="?", default="all")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd in ("download", "all"):
        download(a.workers, a.force)
    if a.cmd in ("run", "all"):
        run_backtest()
    if a.cmd in ("report", "tables", "all"):
        report()


if __name__ == "__main__":
    main()
