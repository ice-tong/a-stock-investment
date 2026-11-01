"""朴素选股回测：月度调仓、等权前 N、次日开盘成交（预注册见 docs/research/选股策略_预注册_2026-11-01.md）。

用法：
    .venv/bin/python tools/select_bt.py run --set A --rules H1,H2,H3,H4,H5 [--n 20]
    .venv/bin/python tools/select_bt.py run --set B --rules H5
    .venv/bin/python tools/select_bt.py run --set SPLITS --prefix r4- --cmp K3 --rules F1,F2,F3,F4,F5 --n 50（第四轮）
"""
import argparse
import hashlib
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from tools import select_screens as scr
except ImportError:  # 直接以脚本运行时
    import select_screens as scr

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "data" / "cache" / "full" / "panel.pkl"
SPOT = ROOT / "data" / "cache" / "spot_20261027.csv"

COST_ONE_WAY = 0.00175  # 佣金 0.025% + 印花税 0.05%/2 + 过户费 0.001% + 滑点 0.10% ≈ 0.35%/来回
MIN_AMT20 = 5e7
MIN_PRICE = 3.0
MIN_AGE = 250
SEGMENTS = [("2020-22", "2020", "2022"), ("2023-24", "2023", "2024"), ("2025-26", "2025", "2026")]


def half(code: str, salt: str = "") -> str:
    """第一轮 A/B：salt=""；第二轮 C/D：salt="r2:"。"""
    h = int(hashlib.md5((salt + code).encode()).hexdigest(), 16) % 2
    return ("A" if h == 0 else "B") if not salt else ("C" if h == 0 else "D")


def fin_wide(index, columns, f=None):
    """时点财务：每个交易日可见的最新 TTM（可用日 = 法定截止日次日，见 tools/select_fin.py）。"""
    f = pd.read_pickle(PANEL.parent / "fin_pit.pkl") if f is None else f
    f = f[f.code.isin(columns)].drop_duplicates(["code", "avail"], keep="last")
    out = {}
    for c in ("eps_ttm", "np_ttm", "cfps_ttm"):
        wide = f.pivot(index="avail", columns="code", values=c)
        allidx = wide.index.union(index)
        out[c] = wide.reindex(allidx).ffill().reindex(index).reindex(columns=columns)
    return out


def sue_wide(index, columns, f=None):
    """时点 SUE：实际披露日次日起可见最新一期（见 tools/select_fin.py sue_table）。"""
    f = pd.read_pickle(PANEL.parent / "sue_pit.pkl") if f is None else f
    f = f[f.code.isin(columns)].drop_duplicates(["code", "avail"], keep="last")
    wide = f.pivot(index="avail", columns="code", values="sue")
    return wide.reindex(wide.index.union(index)).ffill().reindex(index).reindex(columns=columns)


@lru_cache(maxsize=2)
def load(st_mode="name"):
    """st_mode: name = 按当前名称剔除（预注册口径）；pit = 深市按历史简称变更时点剔除、沪市曾戴帽的整只剔除（更严）。"""
    p = pd.read_pickle(PANEL)
    spot = pd.read_csv(SPOT, dtype={"代码": str})
    st = set(spot.loc[spot["名称"].str.contains("ST|退", na=False), "代码"])
    if st_mode == "pit":
        ever = pd.read_pickle(PANEL.parent / "sh_ever_st.pkl")
        st |= set(ever[ever != False].index)  # 拉取失败（None）也剔除
    p = p[~p.code.isin(st)]
    if st_mode == "r5":  # 第五轮：并入 2020 年后退市的股票（腾讯日线），按时点 ST 剔除
        p = pd.concat([p, delisted_panel()], ignore_index=True)
    w = {c: p.pivot(index="date", columns="code", values=c).sort_index() for c in
         ("open", "close", "amount", "volume", "turnover")}
    if st_mode == "pit":
        sz_st = pd.read_pickle(PANEL.parent / "st_true_sz.pkl").reindex(index=w["close"].index, columns=w["close"].columns)
        w["st"] = sz_st.astype("boolean").fillna(False).astype(bool)
    if st_mode == "r5":
        w["st"] = delisted_st(w["close"].index, w["close"].columns)
    return w


def delisted_panel():
    d = PANEL.parent / "delist_daily"
    frames = []
    for f in sorted(d.glob("*.csv")):
        x = pd.read_csv(f)
        x["code"] = f.stem
        frames.append(x[["date", "open", "close", "amount", "volume", "turnover", "code"]])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def delisted_st(index, columns):
    """退市股的时点 ST：深市按简称变更（含 ST/退 起不可投）；沪市没有简称历史，按退市前 250 个交易日起不可投（近似）。
    存续股沿用当前名称口径（已在 load 里整只剔除），这里恒为 False。"""
    st = pd.DataFrame(False, index=index, columns=columns)
    lst = pd.read_pickle(PANEL.parent / "delist_list.pkl").sort_values("delist").drop_duplicates("code", keep="last")
    lst = lst.set_index("code")["delist"]
    names = pd.read_pickle(PANEL.parent / "sz_name_change.pkl")
    names["code"] = "sz" + names["证券代码"].astype(str).str.zfill(6)
    names["date"] = pd.to_datetime(names["变更日期"]).dt.strftime("%Y-%m-%d")
    for c in columns.intersection(lst.index):
        if c.startswith("sz"):
            h = names[names.code == c].sort_values("date")
            flag = h["变更后简称"].str.contains("ST|退", na=False).values
            state = pd.Series(flag, index=h["date"].values)
            state = state[~state.index.duplicated(keep="last")]
            st[c] = state.reindex(state.index.union(index)).ffill().reindex(index).fillna(False).astype(bool).values
        else:
            k = index.searchsorted(lst[c])
            st.iloc[max(k - 250, 0):, st.columns.get_loc(c)] = True
    return st


def features(w, fin_pit=None, sue_pit=None):
    """fin_pit / sue_pit 不传时读研究缓存；前向账户（tools/s002.py）传入自己刷新的表。"""
    close, amount, volume, turnover = w["close"], w["amount"], w["volume"], w["turnover"]
    ret = close.pct_change(fill_method=None)
    obs = close.notna().cumsum()
    seasoned = close.iloc[0].notna()  # 2020-01-02 已在交易视为老股
    age_ok = (obs >= MIN_AGE) | seasoned
    amt20 = amount.rolling(20, min_periods=15).mean()
    vwap20 = amount.rolling(20, min_periods=15).sum() / volume.rolling(20, min_periods=15).sum()
    elig = age_ok & (amt20 >= MIN_AMT20) & (vwap20 >= MIN_PRICE) & close.notna()
    if "st" in w:
        elig &= ~w["st"]
    mcap = (amount / turnover.replace(0, np.nan)).rolling(20, min_periods=15).mean()
    f = {
        "vol60": ret.rolling(60, min_periods=50).std(),
        "turn20": turnover.rolling(20, min_periods=15).mean(),
        "r20": close / close.shift(20) - 1,
        "mcap": mcap,
    }
    fin = fin_wide(close.index, close.columns, fin_pit)
    f["ep"] = fin["eps_ttm"] / (amount / volume.replace(0, np.nan))
    f["np_ttm"], f["cfps_ttm"] = fin["np_ttm"], fin["cfps_ttm"]
    # 第四轮：SUE、异常换手（20 日 / 250 日）、应计（(EPS − 每股经营现金流) / 价格）
    f["sue"] = sue_wide(close.index, close.columns, sue_pit)
    f["at"] = f["turn20"] / turnover.rolling(250, min_periods=200).mean()
    f["acc"] = (fin["eps_ttm"] - fin["cfps_ttm"]) / (amount / volume.replace(0, np.nan))
    f["px_raw"] = amount / volume.replace(0, np.nan)          # 第五轮 X2：未复权价
    f["fmcap_d"] = amount / turnover.replace(0, np.nan)       # 第五轮 X2/X3：当日流通市值
    return elig, f


def composite(sue, ep, at, acc):
    """第四轮合成分 Z：各因子截面百分位（越高越好）等权平均；缺 2 个及以上为 NaN。输入为同一截面的 Series。"""
    ranks = pd.concat([sue.rank(pct=True), ep.rank(pct=True), (-at).rank(pct=True), (-acc).rank(pct=True)], axis=1)
    return ranks.mean(axis=1).where(ranks.notna().sum(axis=1) >= 3)


def fpick(rule, f, d, cand, n, held=()):
    """第四轮 F1/F3/F4/F5、第五轮 R1–R5 选股（因子排名在可投资格 cand 内计算）；held 只用于 R5 缓冲。"""
    at = {k: f[k].loc[d].reindex(cand) for k in ("sue", "ep", "at", "acc", "mcap")}
    if rule == "F1":
        return list(at["sue"].dropna().nlargest(n).index)
    z = composite(at["sue"], at["ep"], at["at"], at["acc"])
    m = at["mcap"]
    if rule == "F3":
        ok = z.notna() & m.notna()
        if ok.sum() < n:
            return []
        grp = pd.qcut(m[ok].rank(method="first"), 5, labels=False)
        return [c for g in range(5) for c in z[ok][grp == g].nlargest(n // 5).index]
    if rule == "F4":
        return list(z[m <= m.quantile(0.2)].dropna().nlargest(n).index)
    if rule == "F5":
        return list(z[m > m.quantile(0.3)].dropna().nlargest(n).index)
    if rule in RRULES:
        pool = z[m <= m.quantile(0.2)].dropna()
        x = screens(rule, d, pool.index, f)
        pool = (pool + x["bonus"] * scr.BONUS).where(~x["bad"]).dropna().sort_values(ascending=False)
        if rule != "R5":
            return list(pool.index[:n])
        ranked = list(pool.index)
        keep = [c for c in ranked[:R5_BUFFER] if c in set(held)]
        return keep + [c for c in ranked if c not in keep][: R5_N - len(keep)]
    raise ValueError(rule)


# 第五轮：R1 = F4+X1，R2 = F4+X2，R3 = F4+X3，R4 = 三道叠加，R5 = R4 取 10 只 + 前 30 名缓冲
RRULES = {"R1": ("x1",), "R2": ("x2",), "R3": ("x3",), "R4": ("x1", "x2", "x3"), "R5": ("x1", "x2", "x3")}
R5_N, R5_BUFFER = 10, 30


def screens(rule, d, codes, f):
    t = scr.tables()
    use = RRULES[rule]
    idx = pd.Index(codes)
    bad = pd.Series(False, index=idx)
    bonus = pd.Series(0.0, index=idx)
    if "x1" in use:
        bad |= scr.x1_bad(d, idx, t["fin"], t["annual_np"], t["fhps"])
    dates = f["mcap"].index
    if "x2" in use:
        k = dates.get_loc(d) + scr.UNLOCK_DAYS
        d31 = dates[k] if k < len(dates) else scr._shift(d, 45)
        bad |= scr.x2_bad(d, idx, d31, f["px_raw"].loc[d], f["fmcap_d"].loc[d], t["unlock"], t["ggcg"])
    if "x3" in use:
        bonus += scr.x3_bonus(d, idx, f["fmcap_d"].loc[d], t["ggcg"], t["repo"]).astype(float)
    return {"bad": bad, "bonus": bonus}


FRULES = ("F1", "F3", "F4", "F5", *RRULES)


# 第三轮组合构造：(持股数, 缓冲名次, 分位)；分位非空时持有市值最小的该比例全部股票
KRULES = {"K1": (50, None, None), "K2": (100, None, None), "K3": (None, None, 0.2),
          "K4": (50, 100, None), "K5": (100, 200, None)}


def score(rule, f, d):
    """返回截面分数，越小越优先。"""
    if rule == "H1":
        return f["vol60"].loc[d]
    if rule == "H2":
        return f["turn20"].loc[d]
    if rule == "H3":
        return f["r20"].loc[d]
    if rule == "H4":
        return f["mcap"].loc[d]
    if rule == "H5":
        return f["vol60"].loc[d].rank(pct=True) + f["turn20"].loc[d].rank(pct=True)
    if rule == "G1":
        return f["mcap"].loc[d].where(f["np_ttm"].loc[d] > 0)
    if rule == "G2":
        ep = f["ep"].loc[d].where(f["ep"].loc[d] > 0)
        m = f["mcap"].loc[d].where(ep.notna())
        return m.rank(pct=True) + (-ep).rank(pct=True)
    if rule == "G3":
        ep = f["ep"].loc[d]
        return (-ep).where(ep > 0)
    if rule == "G4":
        ok = (f["np_ttm"].loc[d] > 0) & (f["cfps_ttm"].loc[d] > 0)
        return f["mcap"].loc[d].where(ok)
    if rule == "G5":
        m, ep = f["mcap"].loc[d], f["ep"].loc[d]
        ok = (m > m.quantile(0.3)) & (ep > 0)
        return (-ep).where(ok)
    if rule in KRULES:
        return f["mcap"].loc[d]
    if rule in FRULES:
        return True  # 由 fpick 选股
    if rule == "EW":
        return None
    raise ValueError(rule)


def limit_pct(codes, date):
    lim = pd.Series(0.10, index=codes)
    big = codes.str[2:].str.startswith(("300", "301", "688"))
    if date < "2020-08-24":
        big &= ~codes.str[2:].str.startswith("300")
    lim[big] = 0.20
    return lim


def run(rule, universe, n=20, cost=True, st_mode="name", veto=None):
    """veto: 与 close 同形的布尔表，True 的格子不可选（用于「完美否决」上界等实验）。"""
    if rule == "F2":
        return run_event(universe, st_mode=st_mode, cost=cost)
    w = load(st_mode)
    cols = [c for c in w["close"].columns if c in universe]
    w = {k: v[cols] for k, v in w.items()}
    elig, f = features(w)
    if veto is not None:
        elig &= ~veto.reindex(index=elig.index, columns=elig.columns).fillna(False).astype(bool)
    opn, close = w["open"], w["close"]
    dates = close.index
    month = pd.Series(dates.str[:7], index=dates)
    rebal = [d for d, nxt in zip(dates[:-1], month.shift(-1).iloc[:-1]) if month[d] != nxt]
    rebal = [d for d in rebal if d >= "2020-03-31"]
    pos = {d: i for i, d in enumerate(dates)}
    close_ff = close.ffill()

    rows, prev_w = [], pd.Series(dtype=float)
    for k in range(len(rebal) - 1):
        d, d_next = rebal[k], rebal[k + 1]
        e, e_next = dates[pos[d] + 1], dates[pos[d_next] + 1]
        ok = elig.loc[d]
        cand = ok[ok].index
        s = score(rule, f, d)
        if s is None:
            picks = list(cand)
        elif rule in FRULES:
            picks = fpick(rule, f, d, cand, n, held=prev_w.index)
        else:
            s = s.reindex(cand).dropna()
            if rule in KRULES:
                kn, buf, q = KRULES[rule]
                if q is not None:
                    picks = list(s[s <= s.quantile(q)].index)
                else:
                    ranked = list(s.sort_values().index)
                    keep = [c for c in prev_w.index if buf and c in ranked[:buf]] if len(prev_w) else []
                    picks = keep + [c for c in ranked if c not in keep][: kn - len(keep)]
            else:
                picks = list(s.nsmallest(n).index)
        if not picks:
            continue
        picks = pd.Index(picks)
        o_in, c_prev = opn.loc[e, picks], close.loc[d, picks]
        lim = limit_pct(picks, e)
        buyable = o_in.notna() & ((o_in < c_prev * (1 + lim - 0.005)) | picks.isin(prev_w.index))
        held = picks[buyable.values]
        slots = len(picks) if s is not None else len(held)
        # 卖出：e_next 开盘；开盘跌停顺延（最多 10 日），停牌用最近收盘
        exit_px = {}
        for c in held:
            i0 = pos[e_next]
            px = np.nan
            for j in range(i0, min(i0 + 10, len(dates))):
                o, pc = opn.iat[j, opn.columns.get_loc(c)], close_ff.iat[j - 1, close.columns.get_loc(c)]
                if pd.notna(o) and o > pc * (1 - limit_pct(pd.Index([c]), dates[j]).iat[0] + 0.005):
                    px = o
                    break
            exit_px[c] = px if pd.notna(px) else close_ff.at[dates[i0], c]
        r = pd.Series({c: exit_px[c] / o_in[c] - 1 for c in held})
        new_w = pd.Series(1.0 / slots, index=held)
        turnover = new_w.sub(prev_w, fill_value=0).abs().sum()
        gross = r.sum() / slots
        net = gross - (turnover * COST_ONE_WAY if cost else 0)
        drift = new_w * (1 + r)
        prev_w = drift / (drift.sum() + (slots - len(held)) / slots) if len(held) else pd.Series(dtype=float)
        rows.append({"date": e, "exit": e_next, "ret": net, "gross": gross, "n": len(held), "turnover": turnover})
    return pd.DataFrame(rows).set_index("date")


def run_event(universe, st_mode="name", cost=True, hold=40, thr=2.0):
    """第四轮 F2：每个交易日等权持有「近 hold 个交易日内实际披露、SUE ≥ thr」的股票，每日开盘调回等权。

    信号日 = 实际披露日之后的首个交易日（收盘可见、且当日在可投资格内）；次一交易日开盘买入。
    买入日开盘接近涨停则放弃该事件；到期日开盘跌停则顺延持有。按自然月汇总成与 run() 相同的表。
    """
    w = load(st_mode)
    cols = [c for c in w["close"].columns if c in universe]
    w = {k: v[cols] for k, v in w.items()}
    elig, _ = features(w)
    opn, close = w["open"], w["close"]
    dates = close.index
    close_ff = close.ffill()
    px = opn.where(opn.notna(), close_ff.shift(1)).fillna(close_ff)  # 停牌按最近收盘估值
    sue = pd.read_pickle(PANEL.parent / "sue_pit.pkl")
    sue = sue[sue.code.isin(cols) & (sue.sue >= thr)]
    di = {d: i for i, d in enumerate(dates)}
    ci = {c: j for j, c in enumerate(cols)}
    O, PC, PX = opn.to_numpy(), close_ff.shift(1).to_numpy(), px.to_numpy()
    E = elig.to_numpy()
    active = np.zeros(O.shape, dtype=bool)
    for r in sue.itertuples():
        k = dates.searchsorted(r.avail)  # 首个 >= 可用日的交易日 = 信号日
        if k + 1 >= len(dates) or dates[k] < "2020-03-31":
            continue
        j = ci[r.code]
        if not E[k, j]:
            continue
        b = k + 1
        lim = limit_pct(pd.Index([r.code]), dates[b]).iat[0]
        if np.isnan(O[b, j]) or O[b, j] >= PC[b, j] * (1 + lim - 0.005):
            continue
        end = min(b + hold, len(dates) - 1)
        while end < len(dates) - 1:  # 到期日开盘跌停或停牌：顺延
            limd = limit_pct(pd.Index([r.code]), dates[end]).iat[0]
            if not np.isnan(O[end, j]) and O[end, j] > PC[end, j] * (1 - limd + 0.005):
                break
            end += 1
        active[b:end, j] = True
    rows, prev = [], np.zeros(len(cols))
    for t in range(len(dates) - 1):
        a = active[t]
        tgt = a / a.sum() if a.any() else np.zeros(len(cols))
        turn = np.abs(tgt - prev).sum()
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(a & (PX[t] > 0), PX[t + 1] / PX[t] - 1, 0.0)
        r = np.where(np.isfinite(r), r, 0.0)
        gross = float((tgt * r).sum())
        net = gross - (turn * COST_ONE_WAY if cost else 0)
        drift = tgt * (1 + r)
        prev = drift / (drift.sum() + (1 - tgt.sum())) if tgt.sum() else np.zeros(len(cols))
        rows.append({"date": dates[t], "exit": dates[t + 1], "ret": net, "gross": gross, "n": int(a.sum()), "turnover": turn})
    d = pd.DataFrame(rows)
    d = d[d.date >= "2020-04-01"]
    d["m"] = d.date.str[:7]
    g = d.groupby("m")
    out = pd.DataFrame({"date": g.date.first(), "exit": g.exit.last(), "ret": g.ret.apply(lambda x: (1 + x).prod() - 1),
                        "gross": g.gross.apply(lambda x: (1 + x).prod() - 1), "n": g.n.mean(), "turnover": g.turnover.sum(),
                        "empty": g.n.apply(lambda x: int((x == 0).sum()))})
    return out.set_index("date")


def stats(df, bench=None):
    r = df["ret"]
    nav = (1 + r).cumprod()
    yrs = len(r) / 12
    out = {
        "CAGR": nav.iat[-1] ** (1 / yrs) - 1,
        "vol": r.std() * np.sqrt(12),
        "maxDD": (nav / nav.cummax() - 1).min(),
        "win_m": (r > 0).mean(),
        "turn": df["turnover"].mean(),
        "n": df["n"].mean(),
        "worst_m": r.min(),
        "q05": r.quantile(0.05),
    }
    # 剔除与 2024-01~02 小盘踩踏、2024-09-24~10-08 行情重叠的持有期
    shock = ((df.index <= "2024-02-29") & (df["exit"] >= "2024-01-01")) | ((df.index <= "2024-10-08") & (df["exit"] >= "2024-09-24"))
    rs = r[~shock]
    out["CAGR_x"] = (1 + rs).prod() ** (12 / len(rs)) - 1
    for name, a, b in SEGMENTS:
        seg = r[(r.index.str[:4] >= a) & (r.index.str[:4] <= b)]
        out[name] = (1 + seg).prod() ** (12 / len(seg)) - 1 if len(seg) else np.nan
        if bench is not None:
            bs = bench["ret"].reindex(seg.index)
            out[name + "_ex"] = out[name] - ((1 + bs).prod() ** (12 / len(bs)) - 1)
    if bench is not None:  # 月度超额的 t 值（按自然月对齐，兼容 F2 的月度汇总）
        ex = (r.groupby(r.index.str[:7]).sum() - bench["ret"].groupby(bench.index.str[:7]).sum()).dropna()
        out["ex_m"] = ex.mean()
        out["t_ex"] = ex.mean() / ex.std() * np.sqrt(len(ex))
    return out


def splits(codes, rules, st_mode, k=20, prefix="r3-", n=20, cmp=None):
    """第三轮起：20 次随机半样本切分，报 CAGR 中位数、超额为正的次数与平均超额；cmp 给出时另报相对 cmp 规则的超额。"""
    rows = []
    for i in range(k):
        uni = {c for c in codes if int(hashlib.md5(f"{prefix}{i}:{c}".encode()).hexdigest(), 16) % 2 == 0}
        ew = stats(run("EW", uni, cost=False, st_mode=st_mode))
        base = stats(run(cmp, uni, n, st_mode=st_mode)) if cmp else None
        for rule in rules:
            r = stats(run(rule, uni, n, st_mode=st_mode))
            rows.append({"rule": rule, "split": i, "CAGR": r["CAGR"], "ex": r["CAGR"] - ew["CAGR"],
                         "ex_cmp": r["CAGR"] - base["CAGR"] if cmp else np.nan,
                         "dd_cmp": r["maxDD"] - base["maxDD"] if cmp else np.nan,
                         "maxDD": r["maxDD"], "turn": r["turn"], "n": r["n"]})
        print(f"split {i} done", flush=True)
    d = pd.DataFrame(rows)
    g = d.groupby("rule").agg(CAGR_med=("CAGR", "median"), CAGR_min=("CAGR", "min"), ex_mean=("ex", "mean"),
                              ex_pos=("ex", lambda x: int((x > 0).sum())), ex_min=("ex", "min"),
                              cmp_mean=("ex_cmp", "mean"), cmp_pos=("ex_cmp", lambda x: int((x > 0).sum())),
                              dd_cmp_mean=("dd_cmp", "mean"), dd_cmp_pos=("dd_cmp", lambda x: int((x > 0).sum())),
                              DD_med=("maxDD", "median"), turn=("turn", "mean"), n=("n", "mean"))
    for c in ("CAGR_med", "CAGR_min", "ex_mean", "ex_min", "cmp_mean", "dd_cmp_mean", "DD_med"):
        g[c] = (g[c] * 100).round(1)
    print(f"splits={k} st={st_mode}")
    print(g.round(2).to_string())
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--set", default="A", choices=["A", "B", "C", "D", "ALL", "SPLITS"])
    ap.add_argument("--rules", default="H1,H2,H3,H4,H5")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--st", default="name", choices=["name", "pit", "r5"])
    ap.add_argument("--prefix", default="r3-")
    ap.add_argument("--cmp", default=None, help="切分时的对照规则（第四轮 F4 用 K3）")
    ap.add_argument("--out", default=None, help="切分明细另存 CSV")
    a = ap.parse_args()
    codes = load(a.st)["close"].columns
    if a.set == "SPLITS":
        d = splits(codes, a.rules.split(","), a.st, prefix=a.prefix, n=a.n, cmp=a.cmp)
        if a.out:
            d.to_csv(a.out, index=False)
        return d
    salt = "r2:" if a.set in ("C", "D") else ""
    uni = set(codes) if a.set == "ALL" else {c for c in codes if half(c, salt) == a.set}
    bench = run("EW", uni, cost=False, st_mode=a.st)
    res = {"EW(基准,无费)": stats(bench)}
    for rule in a.rules.split(","):
        res[rule] = stats(run(rule, uni, a.n, st_mode=a.st), bench)
    t = pd.DataFrame(res).T
    pct = [c for c in t.columns if c not in ("turn", "n", "t_ex")]
    print(f"set={a.set} stocks={len(uni)} N={a.n} st={a.st}")
    print(t.assign(**{c: (t[c] * 100).round(1) for c in pct}).assign(turn=t.turn.round(2), n=t.n.round(1), **({"t_ex": t.t_ex.round(2)} if "t_ex" in t else {})).to_string())


if __name__ == "__main__":
    main()
