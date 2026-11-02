"""S-002（第四轮 F4）按真实资金规模回测：整手、5 元最低佣金、印花税、过户费、0.10% 滑点，复刻 tools/s002.py 的换仓规则。

与 select_bt.run 的区别：持仓按「元」记账、整手成交、买不起一手留现金；已持有且仍入选的票偏离目标 >25% 才调；
卖出不做跌停顺延（简化，影响很小）。未复权价用当日 成交额/成交量 与前复权价的比值估算，只用于算能买几手。

用法：.venv/bin/python tools/s002_capital_bt.py [--cash 100000 1000000] [--n 50]
"""
import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import select_bt as bt

COMM, COMM_MIN, STAMP, TRANSFER, SLIP, TOL, LOT = 0.00025, 5.0, 0.0005, 0.00001, 0.001, 0.25, 100


def fee(amount, side):
    if amount <= 0:
        return 0.0
    return max(amount * COMM, COMM_MIN) + amount * TRANSFER + (amount * STAMP if side == "sell" else 0.0)


def simulate(cash0, n=50, rule="F4", st="name"):
    w = bt.load(st)
    elig, f = bt.features(w, **bt.feature_args(rule))
    opn, close, amount, volume = w["open"], w["close"], w["amount"], w["volume"]
    dates = close.index
    month = pd.Series(dates.str[:7], index=dates)
    rebal = [d for d, nxt in zip(dates[:-1], month.shift(-1).iloc[:-1]) if month[d] != nxt and d >= "2020-03-31"]
    pos = {d: i for i, d in enumerate(dates)}
    px = opn.where(opn.notna(), close.ffill().shift(1))  # 停牌按前收估值
    raw_k = (amount / volume.replace(0, np.nan)) / ((opn + close) / 2)  # 未复权 / 前复权
    raw_k = raw_k.ffill()
    cash, hold, rows, idle, prev_e = cash0, {}, [], [], None
    for d in rebal:
        e = dates[pos[d] + 1]
        if prev_e is not None:
            for c in hold:
                r = px.at[e, c] / px.at[prev_e, c]
                hold[c] *= r if np.isfinite(r) else 1.0
        equity = cash + sum(hold.values())
        rows.append({"date": e, "equity": equity})
        ok = elig.loc[d]
        cand = ok[ok].index
        picks = set(bt.fpick(rule, f, d, cand, n, held=set(hold)))
        target = equity / n
        o, pc = opn.loc[e], close.loc[d]
        lim = bt.limit_pct(pd.Index(list(picks | set(hold))), e)
        turn = 0.0
        for c in list(hold):  # 卖：换出全卖；超配卖回目标（整手）
            v = hold[c]
            if c in picks and v <= target * (1 + TOL):
                continue
            rp = o.get(c, np.nan) * raw_k.at[e, c]
            if c in picks and np.isfinite(rp) and rp > 0:
                sell_v = math.floor((v - target) / rp / LOT) * LOT * rp
            else:
                sell_v = v
            if sell_v <= 0:
                continue
            gross = sell_v * (1 - SLIP)
            cash += gross - fee(gross, "sell")
            hold[c] = v - sell_v
            turn += sell_v
            if hold[c] <= 1e-6:
                del hold[c]
        unbuy = 0
        for c in picks:  # 买：新票或低配 >25% 的补到目标（整手）
            v = hold.get(c, 0.0)
            if c in hold and v >= target * (1 - TOL):
                continue
            oc = o.get(c, np.nan)
            if not np.isfinite(oc) or oc >= pc[c] * (1 + lim[c] - 0.005):
                continue
            rp = oc * raw_k.at[e, c] * (1 + SLIP)
            qty = math.floor((target - v) / rp / LOT) * LOT
            while qty > 0 and qty * rp + fee(qty * rp, "buy") > cash:
                qty -= LOT
            if qty <= 0:
                unbuy += 1
                continue
            cost = qty * rp
            cash -= cost + fee(cost, "buy")
            hold[c] = v + cost / (1 + SLIP)  # 持仓按无滑点市值记
            turn += cost
        idle.append({"date": e, "cash_w": cash / equity, "unbuy": unbuy, "turn": turn / equity, "n_hold": len(hold)})
        prev_e = e
    nav = pd.DataFrame(rows).set_index("date")["equity"]
    df = pd.DataFrame({"ret": nav.pct_change().shift(-1).dropna()})
    df["exit"] = [nav.index[nav.index.get_loc(i) + 1] for i in df.index]
    df["turnover"], df["n"] = 0.0, 0.0
    return df, pd.DataFrame(idle).set_index("date")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cash", type=float, nargs="+", default=[1e5, 1e6])
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--rule", default="F4")
    ap.add_argument("--st", default="name", choices=["name", "pit", "r5"])
    ap.add_argument("--r6", default="", help="第六轮 R6 的组成（如 y1,y4）")
    a = ap.parse_args()
    bt.R6_PARTS.update(x for x in a.r6.split(",") if x)
    codes = set(bt.load(a.st)["close"].columns)
    ew = bt.run("EW", codes, cost=False, st_mode=a.st)
    ideal = bt.run(a.rule, codes, a.n, st_mode=a.st)
    res = {"等权基准（不扣费）": bt.stats(ew), f"{a.rule} 理想（N={a.n}，按比例扣 0.35%）": bt.stats(ideal, ew)}
    extra = {}
    for c in a.cash:
        df, idle = simulate(c, a.n, a.rule, a.st)
        res[f"{a.rule} {c / 1e4:.0f} 万（整手+最低佣金）"] = bt.stats(df, ew)
        extra[f"{c / 1e4:.0f} 万"] = {"平均现金占比": idle.cash_w.mean(), "平均买不起一手(只)": idle.unbuy.mean(),
                                    "平均持仓(只)": idle.n_hold.mean()}
    t = pd.DataFrame(res).T[["CAGR", "vol", "maxDD", "CAGR_x", "2020-22", "2023-24", "2025-26", "t_ex"]]
    print(t.assign(**{c: (t[c] * 100).round(1) for c in t.columns if c != "t_ex"}).assign(t_ex=t.t_ex.round(2)).to_string())
    print(pd.DataFrame(extra).round(3).to_string())


if __name__ == "__main__":
    main()
