"""第六轮全市场评估（预注册 docs/research/选股策略_预注册_第六轮_2026-11-02.md §4、§6）：
全市场统计、Y4 门槛 E（入选股下一期被 ST / 退市的占比）、信号覆盖率与相关性、R6 敏感性（15/20 只、固定 80% 仓位）。

用法：.venv/bin/python tools/select_r6_eval.py [--r6 y1,y4] [--skip-r6]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import select_bt as bt

PCT = ["CAGR", "vol", "maxDD", "worst_m", "q05", "2020-22", "2023-24", "2025-26", "2025-26_ex"]


def table(res):
    t = pd.DataFrame(res).T
    cols = [c for c in ["CAGR", "vol", "maxDD", "worst_m", "q05", "2020-22", "2023-24", "2025-26", "2025-26_ex", "t_ex", "turn", "n"] if c in t]
    t = t[cols].astype(float)
    return t.assign(**{c: (t[c] * 100).round(1) for c in PCT if c in t}).round(2)


def rebal_dates(dates):
    month = pd.Series(dates.str[:7], index=dates)
    return [d for d, nxt in zip(dates[:-1], month.shift(-1).iloc[:-1]) if month[d] != nxt and d >= "2020-03-31"]


def tail_risk(rule, codes, n=50):
    """门槛 E：每期入选股中，在 (e, 下一期 e] 内被 ST（时点口径）或停止交易（退市）的只次占比。"""
    w = bt.load("r5")
    cols = [c for c in w["close"].columns if c in codes]
    w = {k: v[cols] for k, v in w.items()}
    elig, f = bt.features(w, **bt.feature_args(rule))
    close, st = w["close"], w["st"]
    dates = close.index
    pos = {d: i for i, d in enumerate(dates)}
    last_trade = close.apply(lambda s: s.last_valid_index())
    rb = rebal_dates(dates)
    hit = tot = 0
    held = []
    for d, d2 in zip(rb[:-1], rb[1:]):
        e, e2 = dates[pos[d] + 1], dates[pos[d2] + 1]
        ok = elig.loc[d]
        held = bt.fpick(rule, f, d, ok[ok].index, n, held=held)
        for c in held:
            tot += 1
            st_hit = st.loc[e:e2, c].any()
            gone = last_trade[c] is not None and last_trade[c] < e2 and last_trade[c] < dates[-1]
            hit += bool(st_hit or gone)
    return hit, tot


def coverage_corr(codes):
    """信号覆盖率（K3 池内非缺失占比）与月度截面 Spearman 相关的均值（只作信息）。"""
    w = bt.load("r5")
    cols = [c for c in w["close"].columns if c in codes]
    w = {k: v[cols] for k, v in w.items()}
    elig, f = bt.features(w, r6_signals=True)
    keys = ["sue", "sue_fc", "ep", "at", "acc", "y1"] + (["y2"] if "y2" in f else [])
    cov, corr = [], []
    for d in rebal_dates(w["close"].index):
        ok = elig.loc[d]
        m = f["mcap"].loc[d].reindex(ok[ok].index)
        k3 = m[m <= m.quantile(0.2)].index
        x = pd.DataFrame({k: f[k].loc[d].reindex(k3) for k in keys})
        x["at"], x["acc"] = -x["at"], -x["acc"]
        cov.append(x.notna().mean().rename(d))
        corr.append(x.corr(method="spearman"))
    keys_order = list(corr[0].columns)
    avg = pd.concat(corr).groupby(level=0).mean().reindex(index=keys_order, columns=keys_order)  # 跳过个别月份的缺失
    return pd.concat(cov, axis=1).T, avg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--r6", default="")
    ap.add_argument("--skip-r6", action="store_true")
    a = ap.parse_args()
    bt.R6_PARTS.update(x for x in a.r6.split(",") if x)
    codes = set(bt.load("r5")["close"].columns)
    ew = bt.run("EW", codes, cost=False, st_mode="r5")
    has_y2 = (bt.r6.CACHE / "jgdy.pkl").exists()
    rules = ["R4", "L0", "Y1", *(["Y2"] if has_y2 else []), "Y3", "Y4", "R5"]
    res = {"EW": bt.stats(ew)}
    for r in rules:
        res[r] = bt.stats(bt.run(r, codes, 50, st_mode="r5"), ew)
    if not a.skip_r6 and bt.R6_PARTS:
        r6df = bt.run("R6", codes, 10, st_mode="r5")
        res["R6"] = bt.stats(r6df, ew)
        for nn, buf in ((15, 45), (20, 60)):
            bt.R5_N, bt.R5_BUFFER = nn, buf
            res[f"R6 {nn}只"] = bt.stats(bt.run("R6", codes, nn, st_mode="r5"), ew)
        bt.R5_N, bt.R5_BUFFER = 10, 30
        res["R6 80%仓位"] = bt.stats(r6df.assign(ret=r6df.ret * 0.8), ew)
    pd.set_option("display.width", 250)
    print(f"全市场（--st r5），R6 组成 = {sorted(bt.R6_PARTS) or '—'}")
    print(table(res).to_string())
    for r in ("L0", "Y4"):
        h, t = tail_risk(r, codes)
        print(f"门槛 E {r}：入选 {t} 只次，下一期内 ST 或退市 {h} 只次（{h / t:.2%}）")
    cov, corr = coverage_corr(codes)
    print("K3 池内覆盖率（各期均值）：")
    print(cov.mean().round(3).to_string())
    print("月度截面 Spearman 相关（均值）：")
    print(corr.round(2).to_string())


if __name__ == "__main__":
    main()
