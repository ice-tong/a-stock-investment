"""回测独立验证：复算、敏感性、基准与偏差审计（verifier 用，不复用引擎组合逻辑）。

设计原则
--------
- 候选池构造按口径规格第 3-4 节：逐日重算 eligible + 复用 ``sim.pool`` 的 ``channels()``/``triage()``
  口径（规格明确允许 import 复用这些纯函数），不复制引擎代码。
- 组合模拟（第 5 节）在本文件**独立实现**，与 ``tools/bt_agent_engine.py`` 无代码共享：
  先按"最简口径"（ready 前 6 只等权 + T+1 收盘 + -8% 止损 + 20 日时间止损 + 含成本）实现并与
  引擎 nav_P3 对比；差异>1pp 时用可开关的规则口径做归因（不是调数字凑）。
- 数据只读：``data/cache/backtest/``；本脚本只写 ``data/backtest_agent/verify/``。

用法
----
    .venv/bin/python tools/bt_agent_verify.py cache       # 特征面板 + 候选池 ready 名单缓存
    .venv/bin/python tools/bt_agent_verify.py sim         # 独立组合模拟 + 敏感性 + 子区间
    .venv/bin/python tools/bt_agent_verify.py metrics     # 从引擎 nav 复算指标并对比 metrics.json
    .venv/bin/python tools/bt_agent_verify.py bench       # 基准
    .venv/bin/python tools/bt_agent_verify.py audit       # 偏差审计
    .venv/bin/python tools/bt_agent_verify.py all         # 依次全部跑
"""
import argparse
import json
import math
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from sim import pool as sim_pool  # 只读复用 channels/triage 口径（规格第 4 节允许）

DATA = ROOT / "data" / "cache" / "backtest"
OUT = ROOT / "data" / "backtest_agent"
VER = OUT / "verify"
CACHE = VER / "cache"

START_SIGNAL = "2024-10-24"
END_DATE = "2026-10-23"
BASE_DATE = "2026-10-23"

INIT_CASH = 100_000.0
LOT = 100
TRADING_DAYS = 250

POOL_CFG = {"per_channel": 25, "defensive_max_pe": 25, "breakout_amt_ratio": 1.5,
            "sector_top": 5, "sector_per": 4, "sector_min_size": 8}
FEE = {"commission": 0.00025, "comm_min": 5.0, "stamp": 0.0005, "transfer": 0.00001}
SLIP_TIERS = [(1e9, 0.001), (1e8, 0.002), (0.0, 0.005)]
ETF_SLIP = 0.0005

FEATURE_FIELDS = ["close", "listed_days", "chg_1d", "chg_5d", "ret_20", "ret_60", "ret_120",
                  "vol_20", "vol_60", "ma20", "ma60", "ma120", "hi60", "dd_60",
                  "near_high_60", "avg_amount", "amt_ratio"]


def _log(*a):
    print(*a, flush=True)


def _num(x, d=2):
    try:
        v = float(x)
        return f"{v:.{d}f}" if math.isfinite(v) else "-"
    except (TypeError, ValueError):
        return "-"


# ============================ 1. 数据 ============================
def load_raw():
    panel = pd.read_pickle(DATA / "panel.pkl")
    meta = pd.read_pickle(DATA / "meta.pkl")
    panel["date"] = panel["date"].astype(str)
    panel["code"] = panel["code"].astype(str)
    meta["code"] = meta["code"].astype(str)
    idx = pd.read_csv(DATA / "index_sh000300.csv")
    idx["date"] = idx["date"].astype(str)
    etf = pd.read_csv(DATA / "etf_sh510300.csv")
    etf["date"] = etf["date"].astype(str)
    return panel, meta, idx, etf


def panel_integrity(panel, meta):
    cnt = panel.groupby("code")["date"].count()
    return {
        "rows": int(len(panel)), "codes": int(panel["code"].nunique()),
        "dates": int(panel["date"].nunique()),
        "date_min": panel["date"].min(), "date_max": panel["date"].max(),
        "dup_date_code": int(panel.duplicated(["date", "code"]).sum()),
        "close_nan": int(panel["close"].isna().sum()),
        "amount_nan": int(panel["amount"].isna().sum()),
        "meta_codes": int(meta["code"].nunique()),
        "meta_industry_na": int(meta["industry"].isna().sum()),
        "meta_pe_na": int(meta["pe_snapshot"].isna().sum()),
        "meta_pe_le0": int((meta["pe_snapshot"] <= 0).sum()),
        "codes_obs_lt_678": int((cnt < panel["date"].nunique()).sum()),
        "obs_min": int(cnt.min()), "obs_median": float(cnt.median()),
    }


# ============================ 2. 特征面板 ============================
def compute_features(panel, dates, codes, force=False):
    """逐股按自身交易日序列重算 ``sim.pool.features()`` 同款字段（与直播口径逐值对齐已验证）。"""
    p = CACHE / "features.pkl"
    if p.exists() and not force:
        with open(p, "rb") as f:
            obj = pickle.load(f)
        if obj.get("codes") == codes and obj.get("dates") == dates:
            _log(f"[features] 读缓存 {p}")
            return obj["fields"]
    _log(f"[features] 逐股重算 {len(codes)} 只 / {len(dates)} 个信号日 ...")
    di = {d: i for i, d in enumerate(dates)}
    ci = {c: j for j, c in enumerate(codes)}
    nD, nC = len(dates), len(codes)
    fields = {k: np.full((nD, nC), np.nan, dtype=np.float32) for k in FEATURE_FIELDS}
    panel = panel.sort_values(["code", "date"])
    for code, g in panel.groupby("code", sort=False):
        j = ci.get(code)
        if j is None:
            continue
        g = g.set_index("date")
        c = g["close"].astype(float)
        h = g["high"].astype(float)
        a = g["amount"].astype(float)
        ret = c.pct_change()
        f = pd.DataFrame(index=g.index)
        f["close"] = c
        f["listed_days"] = np.arange(1, len(c) + 1, dtype=float)
        f["chg_1d"] = c / c.shift(1) - 1
        f["chg_5d"] = c / c.shift(5) - 1
        f["ret_20"] = c / c.shift(20) - 1
        f["ret_60"] = c / c.shift(60) - 1
        f["ret_120"] = c / c.shift(120) - 1
        f["vol_20"] = ret.rolling(20).std() * math.sqrt(TRADING_DAYS)
        f["vol_60"] = ret.rolling(60).std() * math.sqrt(TRADING_DAYS)
        f["ma20"] = c.rolling(20).mean()
        f["ma60"] = c.rolling(60).mean()
        f["ma120"] = c.rolling(120).mean()
        f["hi60"] = h.rolling(60).max()
        f["dd_60"] = c / f["hi60"] - 1
        f["near_high_60"] = (c >= 0.97 * f["hi60"]).astype(float)
        f["avg_amount"] = a.rolling(20).mean()
        f["amt_ratio"] = a.rolling(5).mean() / a.rolling(20).mean()
        sub = f.reindex(dates)
        keep = [d for d in sub.index if d in di]
        sub = sub.loc[keep]
        rows = np.array([di[d] for d in keep], dtype=int)
        for k in FEATURE_FIELDS:
            fields[k][rows, j] = sub[k].to_numpy(dtype=np.float32)
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        pickle.dump({"dates": dates, "codes": codes, "fields": fields}, f)
    _log(f"[features] 完成，写缓存 {p} ({p.stat().st_size / 1e6:.0f}MB)")
    return fields


# ============================ 3. regime ============================
def compute_regime(idx, dates):
    s = idx.set_index("date")["close"].sort_index().astype(float)
    ma20, ma60, ma120 = (s.rolling(n).mean() for n in (20, 60, 120))
    offense = (s > ma120) & (ma20 > ma20.shift(5))
    defense = (s < ma120) & (s < ma60)
    reg = pd.Series(np.where(offense, "offense", np.where(defense, "defense", "neutral")), index=s.index)
    return reg.reindex(dates).fillna("defense").to_dict()


# ============================ 4. 候选池（逐日重算） ============================
def _static_ok(codes, meta):
    names = meta.set_index("code")["name"].reindex(codes).fillna("").to_numpy()
    bad = np.array([c.startswith("bj") or c[2:].startswith(("688", "689", "8", "4", "92")) for c in codes])
    st = np.array([("ST" in str(n).upper() or "退" in str(n)) for n in names])
    return ~bad & ~st


def _eligibility(fields, i, static_ok):
    return (static_ok
            & (fields["listed_days"][i] >= 130.0)
            & (fields["avg_amount"][i] >= 1e8)
            & (fields["chg_1d"][i] <= 0.07)
            & (fields["chg_5d"][i] <= 0.25)
            & (fields["close"][i] > 0))


def build_pools(dates, fields, codes, meta, regime, mode="lowvol", triage_pe="none", force=False):
    """逐日：eligible → channels → assemble → triage；返回 {date: {ready, n_*, regime}}。

    mode: 'lowvol'（主口径 defensive=lowvol）| 'pe'（敏感性，defensive 用 eps_implied 反推历史 PE）
    triage_pe: 'snapshot'（2026-10-23 PE 快照进 triage avoid 过滤，前视）| 'none'（PE 全空，无前视）
    引擎实测口径 = lowvol + none（见 verify.md 归因）。
    """
    tag = f"pools_{mode}_{triage_pe}"
    p = CACHE / f"{tag}.pkl"
    if p.exists() and not force:
        with open(p, "rb") as f:
            _log(f"[pools:{tag}] 读缓存")
            return pickle.load(f)
    _log(f"[pools:{tag}] 逐日构造候选池 {len(dates)} 天 ...")
    static_ok = _static_ok(codes, meta)
    m = meta.set_index("code").reindex(codes)
    names = m["name"].fillna("").to_numpy()
    ind = m["industry"].to_numpy()
    pe_snap = m["pe_snapshot"].to_numpy(dtype=float)
    eps = m["eps_implied"].to_numpy(dtype=float)
    out = {}
    for i, d in enumerate(dates):
        feat = pd.DataFrame({k: fields[k][i] for k in FEATURE_FIELDS})
        feat.insert(0, "code", codes)
        feat["name"] = names
        feat["industry"] = ind
        feat["eligible"] = _eligibility(fields, i, static_ok)
        if mode == "pe":
            with np.errstate(divide="ignore", invalid="ignore"):
                feat["pe"] = np.where(eps > 0, feat["close"] / eps, np.nan)
        else:
            feat["pe"] = pe_snap
        ch = sim_pool.channels(feat, POOL_CFG)
        if mode == "lowvol":
            el = feat[feat["eligible"]]
            low = el[(el["close"] > el["ma120"]) & np.isfinite(el["vol_60"])]
            ch["defensive"] = low.sort_values("vol_60").head(POOL_CFG["per_channel"])["code"].tolist()
        feat["pe"] = pe_snap if triage_pe == "snapshot" else np.nan
        pool = sim_pool.assemble(feat, ch, [], [])
        n_elig = int(feat["eligible"].sum())
        if len(pool) == 0:
            out[d] = {"ready": [], "n_eligible": n_elig, "n_pool": 0, "n_ready": 0,
                      "regime": regime.get(d, "neutral")}
            continue
        pool["stop_prob"] = pool["vol_60"].map(sim_pool.stop_prob)
        pool["one_lot_value"] = pool["close"] * 100
        pool = sim_pool.triage(pool, regime.get(d, "neutral"))
        ready = pool[pool["action"] == "ready"]["code"].tolist()
        out[d] = {"ready": ready, "n_eligible": n_elig, "n_pool": int(len(pool)),
                  "n_ready": int(len(ready)), "regime": regime.get(d, "neutral")}
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        pickle.dump(out, f)
    _log(f"[pools:{tag}] 完成")
    return out


# ============================ 5. 独立组合模拟 ============================
DEFAULT_SIM = dict(
    name="base",
    exec_price="close",          # T+1 执行价：close / open
    slippage_mult=1.0,           # 滑点倍数
    stop_pct=0.08,               # 止损
    time_stop_days=20,           # 时间止损
    n_positions=6,               # 目标持股数
    max_new_per_week=None,       # 当周新开上限
    max_industry=None,           # 单行业上限
    max_single=None,             # 单只上限
    min_hold_days=3,
    breaker=True,                # 12% 回撤熔断（暂停新开 10 日）
    breaker_action="pause",      # pause（规格）/ liquidate（sim 代码行为）
    halt=True,                   # 18% 回撤暂停
    halt_action="pause",         # pause（规格）/ liquidate（sim 代码行为）
    halt_resume=False,           # 暂停后是否"净创新高自动恢复"（引擎实现，规格未定义）
    use_regime_sell=False,       # P3 忽略 band → 默认关
    fill_retry=3,                # 顺延天数
    limit_guard="chg",           # chg（按涨跌幅近似）| oneprice（引擎口径：一字板）| none
    size_equity="decision",      # 目标仓位基于决策日净值（最简）/ exec 执行日净值（引擎）
    industry_unknown=False,      # 行业缺失是否计入"未分类"并受 35% 约束（引擎=True）
    week_mode="decision",        # 周新开计数按决策日/执行日所在自然周
    fill_round=4,                # 成交价取整小数位（引擎=3）
    lot_precheck=False,          # 决策时检查"一手金额超目标"（引擎=True）
)


def _limit_pct(code):
    return 0.20 if code[2:].startswith(("300", "301")) else 0.10


def _slip_for(avg_amount):
    if not np.isfinite(avg_amount):
        return SLIP_TIERS[-1][1]
    for lo, rate in SLIP_TIERS:
        if avg_amount >= lo:
            return rate
    return SLIP_TIERS[-1][1]


def simulate(pools, dates, close, open_, avg_amount, codes, meta, cfg=None, high=None, low=None):
    """独立 P3 式组合模拟：T 收盘决策 → T+1 收盘(或开盘)成交，整手，含成本。"""
    cf = dict(DEFAULT_SIM)
    if cfg:
        cf.update(cfg)
    nD, nC = close.shape
    ci = {c: j for j, c in enumerate(codes)}
    meta_i = meta.set_index("code")
    name_map = meta_i["name"].to_dict()
    industry_map = meta_i["industry"].to_dict()
    one_price = (np.abs(high - low) < 1e-9) if (high is not None and low is not None) else None
    chg_mat = np.zeros_like(close)
    chg_mat[1:] = close[1:] / close[:-1] - 1

    cash = INIT_CASH
    positions = {}
    pending_sells = {}
    pending_buys = []
    last_close = {}
    peak = INIT_CASH
    halted = False
    breaker_until = None
    breaker_peak = None
    week_new = {}
    nav_rows, trades = [], []

    def px_of(i, j):
        mat = open_ if cf["exec_price"] == "open" else close
        v = mat[i, j]
        return float(v) if np.isfinite(v) else None

    def not_tradable(i, j, side):
        if cf["limit_guard"] == "none":
            return False
        if cf["limit_guard"] == "oneprice":
            return bool(one_price[i, j]) and ((chg_mat[i, j] > 0) if side == "buy" else (chg_mat[i, j] < 0))
        prev = close[i - 1, j] if i > 0 else np.nan
        if not np.isfinite(prev) and codes[j] in last_close:
            prev = last_close[codes[j]]
        if not np.isfinite(prev) or prev <= 0:
            return False
        p = px_of(i, j)
        if p is None:
            return True
        chg = p / prev - 1
        lp = _limit_pct(codes[j])
        return chg >= lp - 0.005 if side == "buy" else chg <= -lp + 0.005

    def equity_mark():
        sv = sum(p["shares"] * last_close.get(c, p["cost_ps"]) for c, p in positions.items())
        return cash + sv

    for i in range(nD):
        d = dates[i]
        for j in range(nC):
            v = close[i, j]
            if np.isfinite(v):
                last_close[codes[j]] = float(v)
        eq_exec = equity_mark() if cf["size_equity"] == "exec" else None

        # --- 1) 卖出（先卖后买） ---
        for code in list(pending_sells):
            j = ci.get(code)
            if j is None or code not in positions:
                pending_sells.pop(code, None)
                continue
            info = pending_sells[code]
            raw = px_of(i, j)
            if raw is None or not_tradable(i, j, "sell"):
                info["retries"] = info.get("retries", 0) + 1
                if info["retries"] > cf["fill_retry"]:
                    trades.append({"date": d, "code": code, "side": "sell", "shares": 0, "price": np.nan,
                                   "amount": 0.0, "commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0,
                                   "slippage_cost": 0.0, "realized_pnl": np.nan, "reason_type": "no_fill",
                                   "note": "顺延超限未成交：" + info["reason"]})
                    pending_sells.pop(code, None)
                continue
            pos = positions.pop(code)
            slip = info["slip"] * cf["slippage_mult"]
            fill = round(raw * (1 - slip), cf["fill_round"])
            amt = pos["shares"] * fill
            comm = max(FEE["comm_min"], FEE["commission"] * amt)
            stamp = FEE["stamp"] * amt
            transfer = FEE["transfer"] * amt
            proceeds = amt - comm - stamp - transfer
            cash += proceeds
            trades.append({"date": d, "code": code, "side": "sell", "shares": int(pos["shares"]),
                           "price": round(fill, 4), "amount": round(amt, 2),
                           "commission": round(comm, 2), "stamp_tax": round(stamp, 2),
                           "transfer_fee": round(transfer, 4),
                           "slippage_cost": round(pos["shares"] * raw * slip, 2),
                           "realized_pnl": round(proceeds - pos["cost_total"], 2),
                           "reason_type": info["reason"], "note": ""})
            pending_sells.pop(code, None)

        # --- 2) 买入 ---
        for order in list(pending_buys):
            code = order["code"]
            j = ci.get(code)
            if j is None or code in positions:
                pending_buys.remove(order)
                continue
            raw = px_of(i, j)
            if raw is None or not_tradable(i, j, "buy"):
                order["retries"] += 1
                if order["retries"] > cf["fill_retry"]:
                    trades.append({"date": d, "code": code, "side": "buy", "shares": 0, "price": np.nan,
                                   "amount": 0.0, "commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0,
                                   "slippage_cost": 0.0, "realized_pnl": np.nan, "reason_type": "no_fill",
                                   "note": "顺延超限未成交"})
                    pending_buys.remove(order)
                    wk = order.get("wk")
                    if wk is not None:
                        week_new[wk] = max(0, week_new.get(wk, 1) - 1)
                continue
            slip = order["slip"] * cf["slippage_mult"]
            fill = round(raw * (1 + slip), cf["fill_round"])
            if cf["size_equity"] == "exec":
                target_value = order["target_w"] * eq_exec
                shares = int(target_value / raw // LOT) * LOT
            else:
                target_value = order["target"]
                shares = int(min(cash, target_value) // (fill * LOT)) * LOT
            while shares > 0:
                amt = shares * fill
                comm = max(FEE["comm_min"], FEE["commission"] * amt)
                total = amt + comm + FEE["transfer"] * amt
                if total <= cash + 1e-9:
                    break
                shares -= LOT
            if shares <= 0:
                pending_buys.remove(order)
                wk = order.get("wk")
                if wk is not None:
                    week_new[wk] = max(0, week_new.get(wk, 1) - 1)
                continue
            amt = shares * fill
            comm = max(FEE["comm_min"], FEE["commission"] * amt)
            transfer = FEE["transfer"] * amt
            total = amt + comm + transfer
            cash -= total
            cps = total / shares
            positions[code] = {"code": code, "shares": shares, "cost_total": total, "cost_ps": cps,
                               "buy_i": i, "stop_price": round(cps * (1 - cf["stop_pct"]), 3),
                               "industry": industry_map.get(code)}
            trades.append({"date": d, "code": code, "side": "buy", "shares": int(shares),
                           "price": round(fill, 4), "amount": round(amt, 2),
                           "commission": round(comm, 2), "stamp_tax": 0.0,
                           "transfer_fee": round(transfer, 4),
                           "slippage_cost": round(shares * raw * slip, 2),
                           "realized_pnl": np.nan, "reason_type": order["reason"], "note": ""})
            pending_buys.remove(order)

        # --- 3) 估值 & 风控状态 ---
        stock_value = sum(p["shares"] * last_close.get(c, p["cost_ps"]) for c, p in positions.items())
        equity = cash + stock_value
        peak = max(peak, equity)
        dd = 1 - equity / peak if peak > 0 else 0.0
        regime = pools[d]["regime"]
        nav_rows.append({"date": d, "cash": round(cash, 2), "stock_value": round(stock_value, 2),
                         "equity": round(equity, 2), "drawdown": round(dd, 6), "regime": regime,
                         "n_positions": len(positions), "halted": halted})
        if cf["halt_resume"] and halted and equity >= peak - 1e-9:
            halted = False
        if cf["halt"] and dd >= 0.18 and not halted:
            halted = True
            if cf["halt_action"] == "liquidate":
                for code in positions:
                    pending_sells.setdefault(code, {"reason": "halt", "slip": 0.002, "retries": 0})
        elif cf["breaker"] and dd >= 0.12 and breaker_peak != peak:
            breaker_until = i + 10
            breaker_peak = peak
            if cf["breaker_action"] == "liquidate":
                for code in positions:
                    pending_sells.setdefault(code, {"reason": "breaker", "slip": 0.002, "retries": 0})

        # --- 4) 次日决策（基于 d 收盘） ---
        if i >= nD - 1:
            break
        in_breaker = breaker_until is not None and i + 1 <= breaker_until
        for code, pos in positions.items():
            if code in pending_sells:
                continue
            j = ci.get(code)
            cpx = close[i, j] if j is not None else np.nan
            if not np.isfinite(cpx):
                cpx = last_close.get(code, np.nan)
            if not np.isfinite(cpx):
                continue
            if cpx <= pos["stop_price"] + 1e-12:
                pending_sells[code] = {"reason": "stop_loss",
                                       "slip": _slip_for(avg_amount[i, j] if j is not None else np.nan),
                                       "retries": 0}
            elif (i - pos["buy_i"]) >= cf["time_stop_days"] and pos["shares"] * cpx < pos["cost_total"] - 1e-9:
                pending_sells[code] = {"reason": "time_stop",
                                       "slip": _slip_for(avg_amount[i, j] if j is not None else np.nan),
                                       "retries": 0}
        if cf["use_regime_sell"]:
            smax = {"offense": 0.80, "neutral": 0.50, "defense": 0.20}.get(regime)
            if smax is not None:
                held = [(c, p) for c, p in positions.items() if c not in pending_sells]
                tot = sum(p["shares"] * last_close.get(c, p["cost_ps"]) for c, p in held)
                excess = tot - smax * equity
                if excess > 0:
                    mom = []
                    for c, p in held:
                        j = ci.get(c)
                        mom.append((_mom(close, j, i) if j is not None else -9.0, c))
                    mom.sort()
                    for _, c in mom:
                        if excess <= 0:
                            break
                        j = ci.get(c)
                        pending_sells[c] = {"reason": "regime",
                                            "slip": _slip_for(avg_amount[i, j]), "retries": 0}
                        excess -= positions[c]["shares"] * last_close.get(c, positions[c]["cost_ps"])

        can_buy = not halted and not in_breaker
        if can_buy:
            wk_date = dates[i + 1] if cf["week_mode"] == "exec" else d
            wk = pd.Timestamp(wk_date).isocalendar()
            wk_key = (int(wk.year), int(wk.week))
            slots = cf["n_positions"] - (len(positions) - len(pending_sells)) - len(pending_buys)
            target_w = 1.0 / cf["n_positions"]
            if cf["max_single"] is not None:
                target_w = min(target_w, cf["max_single"])
            target = target_w * equity
            ind_w = {}
            if cf["max_industry"] is not None:
                for c, p in positions.items():
                    if c in pending_sells:
                        continue
                    ind = p.get("industry")
                    ind = ind if isinstance(ind, str) and ind else ("未分类" if cf["industry_unknown"] else None)
                    if ind is None:
                        continue
                    ind_w[ind] = ind_w.get(ind, 0.0) + p["shares"] * last_close.get(c, p["cost_ps"]) / equity
            for code in pools[d]["ready"]:
                if slots <= 0:
                    break
                if cf["max_new_per_week"] is not None and week_new.get(wk_key, 0) >= cf["max_new_per_week"]:
                    break
                if code in positions or code in pending_sells or any(o["code"] == code for o in pending_buys):
                    continue
                j = ci.get(code)
                if j is None:
                    continue
                if cf["lot_precheck"] and np.isfinite(close[i, j]) and close[i, j] * LOT > target_w * equity * 1.01:
                    continue
                if cf["max_industry"] is not None:
                    ind = industry_map.get(code)
                    ind = ind if isinstance(ind, str) and ind else ("未分类" if cf["industry_unknown"] else None)
                    if ind is not None and ind_w.get(ind, 0.0) + target_w > cf["max_industry"] + 1e-9:
                        continue
                slip = _slip_for(avg_amount[i, j])
                pending_buys.append({"code": code, "target": target, "target_w": target_w,
                                     "reason": "entry", "slip": slip, "retries": 0, "wk": wk_key})
                week_new[wk_key] = week_new.get(wk_key, 0) + 1
                slots -= 1

    return pd.DataFrame(nav_rows), pd.DataFrame(trades)


def _mom(close, j, i, n=20):
    if i - n < 0:
        return -9.0
    a, b = close[i, j], close[i - n, j]
    return float(a / b - 1) if np.isfinite(a) and np.isfinite(b) and b > 0 else -9.0


# ============================ 6. 指标 ============================
def nav_metrics(nav, col="equity"):
    eq = nav[col].to_numpy(dtype=float)
    if len(eq) < 2:
        return {}
    rets = np.diff(eq) / eq[:-1]
    total = eq[-1] / eq[0] - 1
    n = len(eq)
    years = (n - 1) / TRADING_DAYS
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and (1 + total) > 0 else np.nan
    peak = np.maximum.accumulate(eq)
    dd = 1 - eq / peak
    maxdd = float(dd.max())
    longest = cur = 0
    for v in dd:
        cur = cur + 1 if v > 0 else 0
        longest = max(longest, cur)
    std = rets.std(ddof=1)
    sharpe = float(rets.mean() / std * math.sqrt(TRADING_DAYS)) if std > 0 else np.nan
    vol = float(std * math.sqrt(TRADING_DAYS))
    calmar = float(cagr / abs(maxdd)) if maxdd > 0 and np.isfinite(cagr) else np.nan
    return {"total_return": float(total), "cagr": float(cagr), "max_drawdown": maxdd,
            "sharpe": sharpe, "annual_vol": vol, "calmar": calmar, "n_days": int(n),
            "years_250": float(years), "dd_longest_days": int(longest),
            "final_equity": float(eq[-1]),
            "start_date": str(nav["date"].iloc[0]), "end_date": str(nav["date"].iloc[-1])}


def slice_metrics(nav, start, end, col="equity"):
    sub = nav[(nav["date"] >= start) & (nav["date"] <= end)].reset_index(drop=True)
    return nav_metrics(sub, col) if len(sub) >= 2 else {}


def trade_stats(tr):
    if tr is None or len(tr) == 0:
        return {}
    sells = tr[(tr["side"] == "sell") & (tr["shares"] > 0)]
    buys = tr[(tr["side"] == "buy") & (tr["shares"] > 0)]
    n = len(sells)
    wins = int((sells["realized_pnl"] > 0).sum()) if n else 0
    hold, open_buy = [], {}
    for _, r in tr.sort_values("date").iterrows():
        if r["side"] == "buy" and r["shares"] > 0:
            open_buy[r["code"]] = r["date"]
        elif r["side"] == "sell" and r["shares"] > 0 and r["code"] in open_buy:
            hold.append((pd.Timestamp(r["date"]) - pd.Timestamp(open_buy.pop(r["code"]))).days)
    return {"n_closed": int(n), "win_rate": float(wins / n) if n else np.nan,
            "avg_hold_calendar_days": float(np.mean(hold)) if hold else np.nan,
            "n_buys": int(len(buys)), "n_no_fill": int((tr["reason_type"] == "no_fill").sum()),
            "total_fees": float((tr["commission"] + tr["stamp_tax"] + tr["transfer_fee"]).sum()),
            "total_slippage": float(tr["slippage_cost"].sum()),
            "buy_notional": float(buys["amount"].sum())}


# ============================ 7. 基准 ============================
def _series_metrics(s, label):
    eq = s.to_numpy(dtype=float)
    nav = pd.DataFrame({"date": [str(x) for x in s.index], "equity": eq})
    m = nav_metrics(nav)
    m["benchmark"] = label
    return m


def _equal_weight_nav(dates, close, elig, codes, cost=True):
    """等权组合：每月首个交易日调仓（收盘价），换手计双边成本。"""
    nD, nC = close.shape
    rets = np.zeros_like(close)
    rets[1:] = close[1:] / close[:-1] - 1
    rets = np.nan_to_num(rets, nan=0.0)
    rebal, seen = set(), set()
    for d in dates:
        if d[:7] not in seen:
            seen.add(d[:7])
            rebal.add(d)
    buy_rate = 0.001 + FEE["commission"] + FEE["transfer"]
    sell_rate = 0.001 + FEE["commission"] + FEE["transfer"] + FEE["stamp"]
    v = np.zeros(nC)
    navs = []
    for i, d in enumerate(dates):
        if i > 0:
            v = v * (1 + rets[i])
            V = v.sum()
        else:
            V = INIT_CASH
        if d in rebal:
            uni = np.where(elig[i] & np.isfinite(close[i]))[0]
            if len(uni) == 0:
                uni = np.where(np.isfinite(close[i]))[0]
            tgt = np.zeros(nC)
            tgt[uni] = V / len(uni)
            if i > 0:
                turnover = np.abs(v - tgt).sum() / 2
                cost = turnover * (buy_rate + sell_rate) if cost else 0.0
                V -= cost
                tgt[uni] = V / len(uni)
            v = tgt
        navs.append({"date": d, "equity": float(v.sum()) if i > 0 else INIT_CASH})
    return pd.DataFrame(navs)


def benchmarks(dates, codes, fields, meta, idx, etf, close):
    rows = []
    s = idx.set_index("date")["close"].astype(float)
    rows.append(_series_metrics(s[(s.index >= START_SIGNAL) & (s.index <= END_DATE)], "sh000300价格指数"))
    e = etf.set_index("date")["close"].astype(float)
    rows.append(_series_metrics(e[(e.index >= START_SIGNAL) & (e.index <= END_DATE)], "sh510300买入持有"))
    static_ok = _static_ok(codes, meta)
    elig = np.zeros((len(dates), len(codes)), dtype=bool)
    for i in range(len(dates)):
        elig[i] = _eligibility(fields, i, static_ok)
    nav_ew = _equal_weight_nav(dates, close, elig, codes, cost=True)
    m = nav_metrics(nav_ew)
    m["benchmark"] = "等权合格池(月调仓后持有-本验证口径)"
    rows.append(m)
    nav_ew0 = _equal_weight_nav(dates, close, elig, codes, cost=False)
    m0 = nav_metrics(nav_ew0)
    m0["benchmark"] = "等权合格池(月调仓后持有-无成本)"
    rows.append(m0)
    # 引擎口径：每月固定"前一交易日 eligible 名单"，月内日频等权（每日再平衡），每月扣 0.15%
    nav_eng = _equal_weight_engine_style(dates, close, elig)
    me = nav_metrics(nav_eng)
    me["benchmark"] = "等权合格池(引擎口径-月内日频再平衡,0.15%/月)"
    rows.append(me)
    nav_fw = _equal_weight_nav(dates, close, np.isfinite(close), codes, cost=True)
    m2 = nav_metrics(nav_fw)
    m2["benchmark"] = "等权2473固定池(月调仓含成本)"
    rows.append(m2)
    return pd.DataFrame(rows), {"eligible": nav_ew, "eligible_engine": nav_eng, "fixed": nav_fw}


def _equal_weight_engine_style(dates, close, elig):
    """复刻引擎 benchmark_metrics 的 ew_eligible：月内日频等权、每月首日扣 0.15%。"""
    month = pd.Series([d[:7] for d in dates])
    rebal_idx = [i for i, d in enumerate(dates) if i == 0 or month.iloc[i] != month.iloc[i - 1]]
    rets = np.zeros(len(dates))
    for k, i in enumerate(rebal_idx):
        start = i
        end = rebal_idx[k + 1] if k + 1 < len(rebal_idx) else len(dates)
        src = i - 1 if i > 0 else i
        uni = np.where(elig[src])[0]
        if len(uni) == 0:
            continue
        sub = close[max(0, start - 1):end][:, uni]
        with np.errstate(invalid="ignore", divide="ignore"):
            pr = np.nanmean(sub[1:] / sub[:-1] - 1, axis=1)
        seg = np.nan_to_num(pr, nan=0.0)
        if i > 0 and len(seg):
            seg[0] -= 0.0015
        rets[start:start + len(seg)] = seg
    eq = np.cumprod(1 + rets) * INIT_CASH
    return pd.DataFrame({"date": dates, "equity": eq})


# ============================ 8. 与引擎产物对比 ============================
def load_engine_nav(name):
    p = OUT / f"nav_{name}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, dtype={"date": str})
    col = next((c for c in ("equity", "nav", "net_value", "value") if c in df.columns), None)
    if col is None:
        return None
    out = df.copy()
    out["equity"] = out[col].astype(float)
    return out


def load_engine_metrics():
    p = OUT / "metrics.json"
    if not p.exists():
        return {}
    try:
        m = json.loads(p.read_text())
    except Exception:
        return {}
    return {k: v.get("metrics", v) for k, v in (m.get("calibers") or {}).items()} or m


def recalc_engine_metrics():
    rows, diffs = [], []
    eng = load_engine_metrics()
    for name in ("P1", "P2", "P3"):
        nav = load_engine_nav(name)
        if nav is None or len(nav) < 2:
            continue
        m = nav_metrics(nav)
        m["portfolio"] = name
        tp = OUT / f"trades_{name}.csv"
        if tp.exists():
            tr = pd.read_csv(tp, dtype={"code": str})
            tr = tr.rename(columns={"reason": "reason_type"})
            sells = tr[(tr.get("side") == "sell") & (tr.get("status") == "filled")]
            m["n_trades"] = int(len(tr))
            m["n_buys"] = int(((tr["side"] == "buy") & (tr["status"] == "filled")).sum())
            m["n_sells"] = int(len(sells))
            stock_sells = sells[sells.get("kind", "stock") == "stock"] if "kind" in sells.columns else sells
            m["win_rate"] = float((stock_sells["pnl"] > 0).mean()) if len(stock_sells) else np.nan
            fee_col = tr["fees"] if "fees" in tr.columns else pd.Series(0.0, index=tr.index)
            m["total_fees"] = float(pd.to_numeric(fee_col, errors="coerce").fillna(0).sum())
            if "holding_days" in tr.columns:
                hs = stock_sells if "kind" in tr.columns else sells
                m["avg_holding_days"] = float(pd.to_numeric(hs["holding_days"], errors="coerce").mean()) \
                    if len(hs) else np.nan
        rows.append(m)
    mine = pd.DataFrame(rows)
    if len(mine) == 0:
        return None, None
    alias = {"total_return": ("total_return", "cum_return", "return"),
             "cagr": ("cagr", "annual_return"), "max_drawdown": ("max_drawdown", "mdd", "maxdd"),
             "sharpe": ("sharpe",), "calmar": ("calmar",), "annual_vol": ("annual_vol", "vol"),
             "dd_longest_days": ("max_dd_duration_days",), "n_buys": ("n_buys",),
             "n_sells": ("n_sells",), "win_rate": ("win_rate",), "total_fees": ("total_fees",),
             "avg_holding_days": ("avg_holding_days",)}
    for _, r in mine.iterrows():
        p = r["portfolio"]
        e = eng.get(p, {})
        if not isinstance(e, dict):
            continue
        for k, al in alias.items():
            ev = next((e[a] for a in al if a in e), None)
            if ev is None:
                continue
            try:
                ev = float(ev)
            except (TypeError, ValueError):
                continue
            diffs.append({"portfolio": p, "metric": k, "engine": ev, "verifier": float(r[k]),
                          "diff": float(r[k]) - ev})
    return mine, pd.DataFrame(diffs)


# ============================ 9. 敏感性 ============================
def run_sensitivity(pools, pools_pe, pools_snapshot, dates, close, open_, high, low, amt20, codes, meta):
    variants = [
        ("base", {}),
        ("exec_open", {"exec_price": "open"}),
        ("slip_0x", {"slippage_mult": 0.0}),
        ("slip_2x", {"slippage_mult": 2.0}),
        ("stop_5", {"stop_pct": 0.05}),
        ("stop_10", {"stop_pct": 0.10}),
        ("n_3", {"n_positions": 3}),
        ("n_3_singlecap20", {"n_positions": 3, "max_single": 0.20}),
        ("n_9", {"n_positions": 9}),
        ("spec_full", {"max_new_per_week": 3, "max_industry": 0.35, "max_single": 0.20}),
        ("breaker_liquidate", {"breaker_action": "liquidate", "halt_action": "liquidate"}),
        ("no_breaker", {"breaker": False, "halt": False}),
        # 规则归因（base → engine 的差异来源，逐条单变量）
        ("rule_weekly3", {"max_new_per_week": 3}),
        ("rule_ind35", {"max_industry": 0.35}),
        ("rule_ind35_unclassified", {"max_industry": 0.35, "industry_unknown": True}),
        ("rule_single20", {"max_single": 0.20}),
        ("rule_size_exec", {"size_equity": "exec"}),
        ("rule_halt_resume", {"halt_resume": True}),
        ("rule_oneprice", {"limit_guard": "oneprice", "fill_round": 3}),
        ("engine_aligned", {"max_new_per_week": 3, "max_industry": 0.35, "max_single": 0.20,
                            "size_equity": "exec", "industry_unknown": True, "week_mode": "exec",
                            "halt_resume": True, "limit_guard": "oneprice", "fill_round": 3,
                            "lot_precheck": True}),
    ]
    pool_map = {v[0]: pools for v in variants}
    rows, navs, trades = [], {}, {}
    for vname, cfg in variants:
        pset = pool_map[vname]
        nav, tr = simulate(pset, dates, close, open_, amt20, codes, meta, dict(cfg, name=vname),
                           high=high, low=low)
        if len(nav) < 2:
            continue
        m = nav_metrics(nav)
        m.update({"variant": vname, **{k: cfg.get(k, DEFAULT_SIM[k]) for k in
                                       ("exec_price", "slippage_mult", "stop_pct", "n_positions",
                                        "max_new_per_week", "max_industry", "max_single",
                                        "size_equity", "industry_unknown")}})
        m["halted_days"] = int(nav["halted"].sum())
        rows.append(m)
        navs[vname] = nav
        trades[vname] = tr
    for vname, pset in (("def_pe", pools_pe), ("triage_pe_snapshot", pools_snapshot)):
        nav, tr = simulate(pset, dates, close, open_, amt20, codes, meta, {"name": vname},
                           high=high, low=low)
        m = nav_metrics(nav)
        m.update({"variant": vname, "halted_days": int(nav["halted"].sum())})
        rows.append(m)
        navs[vname] = nav
        trades[vname] = tr
    return pd.DataFrame(rows), navs, trades


# ============================ 10. 偏差审计 ============================
def run_audit(pools, navs, trades, dates, codes, fields, meta, close, open_, high, low, amt20, idx, etf):
    res = {}
    # 10.1 股票池覆盖（漏斗来自直播 pool 文件）
    md = ROOT / "data" / "pool" / f"{BASE_DATE}.md"
    funnel = None
    if md.exists():
        import re
        m = re.search(r"全市场 (\d+) 只 → 基础过滤 (\d+) 只 → 流动性预过滤 (\d+) 只 → "
                      r"有效特征 (\d+) 只 → 合规 (\d+) 只", md.read_text(encoding="utf-8"))
        if m:
            funnel = [int(x) for x in m.groups()]
    ne = np.array([v["n_eligible"] for v in pools.values()])
    nr = np.array([v["n_ready"] for v in pools.values()])
    reg = pd.Series([v["regime"] for v in pools.values()]).value_counts().to_dict()
    res["universe"] = {
        "funnel_market_base_pre_feat_elig": funnel,
        "backtest_universe": int(len(codes)),
        "coverage_of_market": (len(codes) / funnel[0]) if funnel else None,
        "industry_missing_pct": float(meta["industry"].isna().mean()),
        "pe_missing_pct": float(meta["pe_snapshot"].isna().mean()),
        "eligible_daily_median": float(np.median(ne)), "eligible_daily_min": int(ne.min()),
        "eligible_daily_max": int(ne.max()),
        "ready_daily_median": float(np.median(nr)), "ready_daily_max": int(nr.max()),
        "ready_zero_days": int((nr == 0).sum()),
        "regime_days": {k: int(v) for k, v in reg.items()},
    }
    # 10.2 组合可交易性：单路径统计（换手/费用/集中度）
    nav = navs["base"]
    tr = trades["base"]
    ts = trade_stats(tr)
    m = nav_metrics(nav)
    sw = (nav["stock_value"] / nav["equity"]).replace([np.inf, -np.inf], np.nan)
    ts.update({
        "total_return": m["total_return"], "cagr": m["cagr"], "max_drawdown": m["max_drawdown"],
        "annual_turnover_est": ts.get("buy_notional", 0) / (INIT_CASH * max(m["years_250"], 1e-9)),
        "fee_pct_of_initial": ts.get("total_fees", 0) / INIT_CASH,
        "slippage_pct_of_initial": ts.get("total_slippage", 0) / INIT_CASH,
        "avg_stock_weight": float(sw.mean()), "min_stock_weight": float(sw.min()),
        "avg_n_positions": float(nav["n_positions"].mean()),
        "halted_days": int(nav["halted"].sum()),
    })
    res["portfolio_path"] = ts
    # 10.3 规则口径敏感性（base vs engine_aligned）差异
    res["rule_attribution"] = {k: {"total_return": nav_metrics(v)["total_return"],
                                   "cagr": nav_metrics(v)["cagr"],
                                   "max_drawdown": nav_metrics(v)["max_drawdown"]}
                               for k, v in navs.items() if k in (
                                   "base", "rule_weekly3", "rule_ind35", "rule_ind35_unclassified",
                                   "rule_single20", "rule_size_exec", "rule_halt_resume",
                                   "rule_oneprice", "engine_aligned", "no_breaker", "breaker_liquidate")}
    # 10.4 前视字段：PE 快照参与 triage（look-ahead）vs 不参与
    res["lookahead_pool"] = {
        "triage_pe_none": {"pool_days": int(len(dates))},
    }
    if "triage_pe_snapshot" in navs:
        a, b = nav_metrics(navs["base"]), nav_metrics(navs["triage_pe_snapshot"])
        res["lookahead_pool"]["pe_snapshot_in_triage"] = {
            "cagr_with": b["cagr"], "cagr_without": a["cagr"],
            "cagr_diff_pp": (b["cagr"] - a["cagr"]) * 100}
    # 10.5 执行价假设：open vs close；T 收盘 vs T+1 收盘（多一日滞后）
    a, b = nav_metrics(navs["base"]), nav_metrics(navs["exec_open"])
    res["execution"] = {
        "exec_close": {"cagr": a["cagr"], "maxdd": a["max_drawdown"], "total": a["total_return"]},
        "exec_open": {"cagr": b["cagr"], "maxdd": b["max_drawdown"], "total": b["total_return"]},
        "cagr_diff_pp_close_minus_open": (a["cagr"] - b["cagr"]) * 100,
    }
    # 10.6 成本低估：零滑点/双倍滑点
    s0, s2 = nav_metrics(navs["slip_0x"]), nav_metrics(navs["slip_2x"])
    res["costs"] = {"slip_0x": {"cagr": s0["cagr"]}, "slip_1x": {"cagr": a["cagr"]},
                    "slip_2x": {"cagr": s2["cagr"]},
                    "cagr_gain_per_1x_slip_pp": (s2["cagr"] - s0["cagr"]) * 100 / 2}
    # 10.7 ETF 现金拖累：P1 的 ETF 平均权重 + 与 P3 的收益差
    p1, p3 = load_engine_nav("P1"), load_engine_nav("P3")
    if p1 is not None and p3 is not None:
        m1, m3 = nav_metrics(p1), nav_metrics(p3)
        etf_col = "etf_value" if "etf_value" in p1.columns else None
        res["p1_vs_p3"] = {"P1_cagr": m1["cagr"], "P3_cagr": m3["cagr"],
                           "P1_maxdd": m1["max_drawdown"], "P3_maxdd": m3["max_drawdown"],
                           "cagr_diff_pp": (m1["cagr"] - m3["cagr"]) * 100,
                           "P1_avg_etf_weight": float((p1[etf_col] / p1["equity"]).mean()) if etf_col else None}
    return res


# ============================ 10b. 深度归因 ============================
def concentration_stats(trades, close_df, nav, meta):
    """逐日持仓集中度（单只、单行业）。"""
    if "seq" not in trades.columns:
        trades = trades.assign(seq=range(len(trades)))
    tr = trades[trades["status"] == "filled"].sort_values(["date", "seq"])
    by_date = {d: g for d, g in tr.groupby("date")}
    ind_map = meta.set_index("code")["industry"].to_dict()
    pos = {}
    rows = []
    nav_idx = nav.set_index("date")
    for d in nav["date"]:
        if d in by_date:
            for _, r in by_date[d].iterrows():
                c = r["code"]
                if r["side"] == "buy":
                    pp = pos.setdefault(c, {"shares": 0.0, "cost": 0.0})
                    pp["shares"] += float(r["shares"])
                    pp["cost"] += float(r["amount"]) + float(r.get("fees", 0.0))
                else:
                    pp = pos.get(c)
                    if pp and pp["shares"] > 0:
                        frac = min(1.0, float(r["shares"]) / pp["shares"])
                        pp["cost"] *= (1 - frac)
                        pp["shares"] -= float(r["shares"])
                        if pp["shares"] < 1e-6:
                            pos.pop(c, None)
        eq = float(nav_idx.loc[d, "equity"])
        vals = {}
        for c, pp in pos.items():
            v = np.nan
            if c in close_df.columns:
                s = close_df[c]
                s = s[s.index <= d]
                if len(s) and np.isfinite(s.iloc[-1]):
                    v = pp["shares"] * float(s.iloc[-1])
            if np.isfinite(v):
                vals[c] = v
        if not vals:
            rows.append({"date": d, "n_pos": 0, "max_single_w": 0.0, "max_sector_w": 0.0})
            continue
        tot = sum(vals.values())
        ind_w = {}
        for c, v in vals.items():
            ind = ind_map.get(c)
            ind = ind if isinstance(ind, str) and ind else "未分类"
            ind_w[ind] = ind_w.get(ind, 0.0) + v
        rows.append({"date": d, "n_pos": len(vals),
                     "max_single_w": max(vals.values()) / eq if eq else 0.0,
                     "single_code": max(vals, key=vals.get),
                     "max_sector_w": max(ind_w.values()) / eq if eq else 0.0,
                     "sector": max(ind_w, key=ind_w.get),
                     "stock_exposure": tot / eq if eq else 0.0})
    return pd.DataFrame(rows)


def forward_alpha(pools, dates, fields, codes, close, index_close, static_ok, meta):
    """ready 名单前向收益（T+1 收盘入场）与等权合格池/沪深300 对比。"""
    nD = len(dates)
    idx = index_close.set_index("date")["close"].astype(float)
    rows = []
    for i, d in enumerate(dates):
        if i + 1 >= nD:
            break
        ready = pools[d]["ready"]
        if not ready:
            continue
        for n in (5, 20, 60):
            if i + 1 + n >= nD:
                continue
            entry = i + 1
            exit_ = entry + n
            uni = np.where(_eligibility(fields, i, static_ok) & np.isfinite(close[entry]) & np.isfinite(close[exit_]))[0]
            if len(uni) < 50:
                continue
            uni_ret = float(np.mean(close[exit_, uni] / close[entry, uni] - 1))
            j = {c: k for k, c in enumerate(codes)}
            rr = [close[exit_, j[c]] / close[entry, j[c]] - 1 for c in ready
                  if c in j and np.isfinite(close[entry, j[c]]) and np.isfinite(close[exit_, j[c]])]
            if not rr:
                continue
            ex_idx = float(idx.get(dates[exit_], np.nan) / idx.get(dates[entry], np.nan) - 1) \
                if np.isfinite(idx.get(dates[exit_], np.nan)) else np.nan
            rows.append({"date": d, "fwd": n, "n_ready": len(rr), "ready": float(np.mean(rr)),
                         "ew_universe": uni_ret, "csi300": ex_idx,
                         "excess_ew": float(np.mean(rr)) - uni_ret,
                         "excess_csi": float(np.mean(rr)) - ex_idx if np.isfinite(ex_idx) else np.nan})
    df = pd.DataFrame(rows)
    return df


def pnl_attribution(trades, nav, close_df, meta):
    tr = trades[trades["status"] == "filled"].sort_values(["date", "seq"]) if "seq" in trades.columns \
        else trades[trades["status"] == "filled"].sort_values("date")
    pos, realized = {}, {}
    for _, r in tr.iterrows():
        c = r["code"]
        if r["side"] == "buy":
            pp = pos.setdefault(c, {"shares": 0.0, "cost": 0.0})
            pp["shares"] += float(r["shares"])
            pp["cost"] += float(r["amount"]) + float(r.get("fees", 0.0))
        else:
            pp = pos.get(c)
            if pp and pp["shares"] > 0:
                frac = min(1.0, float(r["shares"]) / pp["shares"])
                realized[c] = realized.get(c, 0.0) + float(r.get("pnl", 0.0))
                pp["cost"] *= (1 - frac)
                pp["shares"] -= float(r["shares"])
                if pp["shares"] < 1e-6:
                    pos.pop(c, None)
    last = nav["date"].iloc[-1]
    equity = float(nav["equity"].iloc[-1])
    ind_map = meta.set_index("code")["industry"].to_dict()
    name_map = meta.set_index("code")["name"].to_dict()
    rows = []
    for c, pp in pos.items():
        s = close_df[c][close_df.index <= last] if c in close_df.columns else pd.Series(dtype=float)
        px = float(s.iloc[-1]) if len(s) and np.isfinite(s.iloc[-1]) else pp["cost"] / pp["shares"]
        val = pp["shares"] * px
        rows.append({"code": c, "name": name_map.get(c, ""),
                     "industry": ind_map.get(c) if isinstance(ind_map.get(c), str) else "未分类",
                     "shares": pp["shares"], "cost": round(pp["cost"], 2), "value": round(val, 2),
                     "unrealized_pnl": round(val - pp["cost"], 2),
                     "realized_pnl": round(realized.get(c, 0.0), 2),
                     "total_pnl": round(val - pp["cost"] + realized.get(c, 0.0), 2),
                     "weight": round(val / equity, 4)})
    df = pd.DataFrame(rows).sort_values("total_pnl", ascending=False)
    closed = tr[tr["side"] == "sell"]
    summary = {"realized_total": float(closed["pnl"].sum()) if "pnl" in closed else 0.0,
               "unrealized_total": float((df.value - df.cost).sum()) if len(df) else 0.0,
               "n_closed": int(len(closed)),
               "n_closed_win": int((closed["pnl"] > 0).sum()) if "pnl" in closed else 0,
               "bank_pnl": float(df[df["industry"] == "金融行业"].total_pnl.sum()) if len(df) else 0.0,
               "top3_pnl": float(df.total_pnl.head(3).sum()) if len(df) else 0.0,
               "final_equity": equity, "end_cash": float(nav["cash"].iloc[-1])}
    return df, summary


def deep_audit(dates, codes, fields, meta, pools, close_df, index_close, navs, trades):
    res = {}
    static_ok = _static_ok(codes, meta)
    close = close_df.to_numpy(np.float32)
    fa = forward_alpha(pools, dates, fields, codes, close, index_close, static_ok, meta)
    fa.to_csv(VER / "forward_alpha.csv", index=False)
    summ = {}
    for n in (5, 20, 60):
        sub = fa[fa["fwd"] == n]
        if len(sub):
            summ[f"fwd{n}"] = {"n_days": int(len(sub)), "n_names": int(sub["n_ready"].sum()),
                               "ready_mean": float(sub["ready"].mean()),
                               "ew_universe_mean": float(sub["ew_universe"].mean()),
                               "csi300_mean": float(sub["csi300"].mean()),
                               "excess_ew": float(sub["excess_ew"].mean()),
                               "excess_csi": float(sub["excess_csi"].mean()),
                               "t_excess_ew": float(sub["excess_ew"].mean() / sub["excess_ew"].std()
                                                    * math.sqrt(len(sub))) if sub["excess_ew"].std() > 0 else None,
                               "hit_rate_ew": float((sub["excess_ew"] > 0).mean())}
    res["forward_alpha"] = summ
    # P3 归因与集中度（引擎成交流水）
    tp = OUT / "trades_P3.csv"
    if tp.exists():
        et = pd.read_csv(tp, dtype={"code": str})
        nav = load_engine_nav("P3")
        if nav is not None:
            df, psum = pnl_attribution(et, nav, close_df, meta)
            df.to_csv(VER / "p3_attribution.csv", index=False)
            res["p3_attribution"] = psum
            conc = concentration_stats(et, close_df, nav, meta)
            conc.to_csv(VER / "p3_concentration.csv", index=False)
            res["p3_concentration"] = {
                "max_single_w": float(conc.max_single_w.max()),
                "max_single_date": str(conc.loc[conc.max_single_w.idxmax(), "date"]),
                "max_sector_w": float(conc.max_sector_w.max()),
                "max_sector": str(conc.loc[conc.max_sector_w.idxmax(), "sector"]),
                "max_sector_date": str(conc.loc[conc.max_sector_w.idxmax(), "date"]),
                "share_days_single_gt20pct": float((conc.max_single_w > 0.20).mean()),
                "avg_single_max_w": float(conc.max_single_w.mean()),
                "end_max_single_w": float(conc.max_single_w.iloc[-1]),
                "end_max_sector_w": float(conc.max_sector_w.iloc[-1]),
            }
    return res


# ============================ 11. 主流程 ============================
def _load_cache():
    with open(CACHE / "features.pkl", "rb") as f:
        obj = pickle.load(f)
    panel, meta, idx, etf = load_raw()
    regime = json.loads((CACHE / "meta_regime.json").read_text())
    return obj["dates"], obj["codes"], obj["fields"], meta, idx, etf, regime


def _price_matrices(dates, codes):
    panel = pd.read_pickle(DATA / "panel.pkl")
    panel = panel[panel["code"].isin(codes)]
    def piv(col, only_dates=True):
        p = panel.pivot(index="date", columns="code", values=col).reindex(columns=codes)
        return p.reindex(index=dates) if only_dates else p
    close = piv("close")
    open_ = piv("open")
    high = piv("high")
    low = piv("low")
    amt_full = piv("amount", only_dates=False)
    amt20 = amt_full.rolling(20, min_periods=20).mean().reindex(index=dates)
    return (close.to_numpy(np.float32), open_.to_numpy(np.float32),
            amt20.to_numpy(np.float32), high.to_numpy(np.float32), low.to_numpy(np.float32))


def cmd_cache(force=False):
    panel, meta, idx, etf = load_raw()
    integ = panel_integrity(panel, meta)
    _log("[panel]", json.dumps(integ, ensure_ascii=False))
    VER.mkdir(parents=True, exist_ok=True)
    (VER / "panel_integrity.json").write_text(json.dumps(integ, ensure_ascii=False, indent=1))
    dates = [d for d in sorted(panel["date"].unique()) if d >= START_SIGNAL]
    codes = sorted(panel["code"].unique())
    fields = compute_features(panel, dates, codes, force=force)
    regime = compute_regime(idx, dates)
    (CACHE / "meta_regime.json").write_text(json.dumps(regime, ensure_ascii=False, indent=0))
    for mode, tpe in (("lowvol", "none"), ("lowvol", "snapshot"), ("pe", "none"), ("pe", "snapshot")):
        build_pools(dates, fields, codes, meta, regime, mode=mode, triage_pe=tpe, force=force)
    _log("[cache] done")
    return dates, codes, fields, meta, idx, etf, regime


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["cache", "sim", "metrics", "bench", "audit", "all"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    VER.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    if a.cmd in ("cache", "all"):
        cmd_cache(force=a.force)
        if a.cmd == "cache":
            return
    dates, codes, fields, meta, idx, etf, regime = _load_cache()
    close, open_, amt20, high, low = _price_matrices(dates, codes)

    if a.cmd in ("sim", "all"):
        pools = build_pools(dates, fields, codes, meta, regime, mode="lowvol", triage_pe="none")
        pools_pe = build_pools(dates, fields, codes, meta, regime, mode="pe", triage_pe="none")
        pools_snap = build_pools(dates, fields, codes, meta, regime, mode="lowvol", triage_pe="snapshot")
        sens, navs, trades = run_sensitivity(pools, pools_pe, pools_snap, dates, close, open_,
                                             high, low, amt20, codes, meta)
        sens.to_csv(VER / "sensitivity.csv", index=False)
        for tag in ("base", "engine_aligned", "spec_full"):
            if tag in navs:
                navs[tag].to_csv(VER / f"nav_{tag}.csv", index=False)
                trades[tag].to_csv(VER / f"trades_{tag}.csv", index=False)
        # 子区间
        sub = []
        for tag, nav in navs.items():
            for lab, s0, s1 in (("seg1_2024-10-24~2025-09-30", START_SIGNAL, "2025-09-30"),
                                ("seg2_2025-10-08~2026-10-23", "2025-10-08", END_DATE),
                                ("full", START_SIGNAL, END_DATE)):
                mm = slice_metrics(nav, s0, s1)
                if mm:
                    sub.append({"variant": tag, "segment": lab, **mm})
        pd.DataFrame(sub).to_csv(VER / "subperiods.csv", index=False)
        summary = {
            "sensitivity": {r["variant"]: {k: r.get(k) for k in
                                           ("total_return", "cagr", "max_drawdown", "sharpe", "calmar", "halted_days")}
                            for r in sens.to_dict("records")},
            "counts": {"days": len(dates), "codes": len(codes)},
            "pool_stats": {"n_eligible_median": float(np.median([v["n_eligible"] for v in pools.values()])),
                           "n_ready_median": float(np.median([v["n_ready"] for v in pools.values()])),
                           "ready_zero_days": int(sum(1 for v in pools.values() if v["n_ready"] == 0))},
        }
        (VER / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float))
        cols = ["variant", "total_return", "cagr", "max_drawdown", "sharpe", "calmar", "halted_days"]
        _log("[sim]\n" + sens[cols].to_string(index=False))
        # 与引擎逐笔对比
        eng_nav = load_engine_nav("P3")
        if eng_nav is not None and "engine_aligned" in navs:
            em = nav_metrics(eng_nav)
            mm = nav_metrics(navs["engine_aligned"])
            cmp = pd.DataFrame([
                {"metric": "total_return", "engine": em["total_return"], "verifier_aligned": mm["total_return"],
                 "diff_pp": (mm["total_return"] - em["total_return"]) * 100},
                {"metric": "cagr", "engine": em["cagr"], "verifier_aligned": mm["cagr"],
                 "diff_pp": (mm["cagr"] - em["cagr"]) * 100},
                {"metric": "max_drawdown", "engine": em["max_drawdown"], "verifier_aligned": mm["max_drawdown"],
                 "diff_pp": (mm["max_drawdown"] - em["max_drawdown"]) * 100},
                {"metric": "sharpe", "engine": em["sharpe"], "verifier_aligned": mm["sharpe"],
                 "diff": mm["sharpe"] - em["sharpe"]},
                {"metric": "final_equity", "engine": em["final_equity"], "verifier_aligned": mm["final_equity"],
                 "diff": mm["final_equity"] - em["final_equity"]},
            ])
            cmp.to_csv(VER / "engine_vs_verifier.csv", index=False)
            _log("[sim] 引擎 P3 vs 独立复算(对齐规则口径)\n" + cmp.to_string(index=False))
            # 逐笔对比
            ep = OUT / "trades_P3.csv"
            if ep.exists() and "engine_aligned" in trades:
                et = pd.read_csv(ep, dtype={"code": str})
                et = et[(et["status"] == "filled") & (et["shares"] > 0)]
                mt = trades["engine_aligned"]
                mt = mt[mt["shares"] > 0]
                ek = set(zip(et["date"].astype(str), et["code"], et["side"]))
                mk = set(zip(mt["date"].astype(str), mt["code"], mt["side"]))
                tc = pd.DataFrame([
                    {"portfolio": "P3", "engine_trades": len(ek), "verifier_trades": len(mk),
                     "overlap": len(ek & mk), "engine_only": len(ek - mk), "verifier_only": len(mk - ek)}])
                tc.to_csv(VER / "trade_overlap.csv", index=False)
                _log("[sim] 逐笔重合\n" + tc.to_string(index=False))

    if a.cmd in ("metrics", "all"):
        mine, diffs = recalc_engine_metrics()
        if mine is not None:
            mine.to_csv(VER / "recalc_metrics.csv", index=False)
            _log("[metrics] 独立复算:\n", mine[["portfolio", "total_return", "cagr", "max_drawdown",
                                                "sharpe", "calmar", "dd_longest_days"]].to_string(index=False))
            if diffs is not None and len(diffs):
                diffs.to_csv(VER / "metrics_diff.csv", index=False)
                _log("[metrics] 与 metrics.json 差异:\n", diffs.to_string(index=False))
            else:
                _log("[metrics] metrics.json 未找到")
        else:
            _log("[metrics] 引擎 nav 尚未生成")

    if a.cmd in ("bench", "all"):
        bm, navmap = benchmarks(dates, codes, fields, meta, idx, etf, close)
        bm.to_csv(VER / "benchmarks.csv", index=False)
        for k, v in navmap.items():
            v.to_csv(VER / f"nav_bench_{k}.csv", index=False)
        _log("[bench]\n" + bm[["benchmark", "total_return", "cagr", "max_drawdown", "sharpe", "calmar"]]
             .to_string(index=False))
        # 子区间基准
        rows = []
        for k, v in navmap.items():
            for lab, s0, s1 in (("seg1", START_SIGNAL, "2025-09-30"), ("seg2", "2025-10-08", END_DATE)):
                mm = slice_metrics(v, s0, s1)
                if mm:
                    rows.append({"benchmark": k, "segment": lab, **mm})
        pd.DataFrame(rows).to_csv(VER / "benchmarks_subperiods.csv", index=False)

    if a.cmd in ("audit", "all"):
        pools = build_pools(dates, fields, codes, meta, regime, mode="lowvol", triage_pe="none")
        pools_pe = build_pools(dates, fields, codes, meta, regime, mode="pe", triage_pe="none")
        pools_snap = build_pools(dates, fields, codes, meta, regime, mode="lowvol", triage_pe="snapshot")
        sens, navs, trades = run_sensitivity(pools, pools_pe, pools_snap, dates, close, open_,
                                             high, low, amt20, codes, meta)
        res = run_audit(pools, navs, trades, dates, codes, fields, meta, close, open_, high, low,
                        amt20, idx, etf)
        close_df = pd.DataFrame(close, index=dates, columns=codes)
        res.update(deep_audit(dates, codes, fields, meta, pools, close_df, idx, navs, trades))
        (VER / "audit.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float))
        _log("[audit]\n" + json.dumps(res, ensure_ascii=False, indent=1, default=float)[:5000])


if __name__ == "__main__":
    main()
