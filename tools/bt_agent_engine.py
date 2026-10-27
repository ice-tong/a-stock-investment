#!/usr/bin/env python
"""Agent 选股策略两年回测引擎（候选池确定性代理 + 机械组合模拟）。

口径规格：docs/research/2026-10-27_agent_backtest_spec.md

做三件事：
1) 用 data/cache/backtest/panel.pkl 逐日重算 sim/pool.py::features() 同款特征（向量化一次算完，再逐日切片）；
2) 每个交易日 T 收盘后：eligible 逐日重算 → sim_pool.channels() → assemble → sim_pool.triage()
   重建候选池与 action=="ready" 可行动名单（只 import，不改 sim/ 代码）；
3) 按 P1/P2/P3 三种仓位口径做逐日组合模拟（T 收盘决策、T+1 收盘成交、成本按 config/broker.yaml、
   风控按 config/risk.yaml），输出 data/backtest_agent/。

数据全部来自本地缓存，不联网；计算确定性（无随机数）。

用法：
    .venv/bin/python tools/bt_agent_engine.py                    # 完整三年回测（主口径）
    .venv/bin/python tools/bt_agent_engine.py --defensive-pe      # defensive 通道用 PE 近似（敏感性）
    .venv/bin/python tools/bt_agent_engine.py --start 2025-01-01 --end 2025-03-31 --out /tmp/bt
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import date as _date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim import broker as sim_broker      # noqa: E402  只 import 复用费率/滑点实现
from sim import config as sim_config      # noqa: E402
from sim import pool as sim_pool          # noqa: E402  只 import 复用 channels/triage/features 口径
from sim import risk as sim_risk          # noqa: E402  只 import 复用 band_for / TIERS

TRADING_DAYS = 250
LOT = 100
NAN = float("nan")
TRIM_TOL = 0.005          # 个股总仓位超 stock_max 的容忍带（避免每日微小漂移造成无谓换手）
HALT_JUMP = 0.18          # 回撤 ≥18% 机械暂停
BREAKER_JUMP = 0.12       # 回撤 ≥12% 熔断
WARN_JUMP = 0.08          # 回撤 ≥8% 预警（risk.yaml drawdown.warn）


def _nan(x) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else NAN
    except (TypeError, ValueError):
        return NAN


def _floor_lot(x: float) -> int:
    return int(math.floor(x / LOT + 1e-9)) * LOT


def _iso_week(d: str) -> tuple:
    y, w, _ = _date.fromisoformat(d).isocalendar()
    return (y, w)


def _r(x, n=6) -> float:
    v = _nan(x)
    return round(v, n) if math.isfinite(v) else NAN


def _jsonable(o):
    """JSON 序列化兜底：numpy 标量转 python，NaN/inf 转 null。"""
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.integer, int)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return v if math.isfinite(v) else None
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if o is None or isinstance(o, str):
        return o
    return str(o)


# ---------------------------------------------------------------- 配置与数据
def load_configs() -> dict:
    return {
        "strategy": sim_config.load("strategy"),
        "risk": sim_config.load("risk"),
        "broker": sim_config.load("broker"),
        "etf": sim_config.load("etf"),
    }


def load_data(data_dir: Path) -> dict:
    panel = pd.read_pickle(data_dir / "panel.pkl")
    meta = pd.read_pickle(data_dir / "meta.pkl")
    index = pd.read_csv(data_dir / "index_sh000300.csv", dtype={"date": str})
    etf = pd.read_csv(data_dir / "etf_sh510300.csv", dtype={"date": str})
    for df, col in ((panel, "date"), (index, "date"), (etf, "date")):
        df[col] = df[col].astype(str).str[:10]
    panel = panel[["date", "code", "open", "high", "low", "close", "volume", "amount"]].copy()
    panel["code"] = panel["code"].astype(str)
    meta = meta.copy()
    meta["code"] = meta["code"].astype(str)
    if "pe_snapshot" not in meta.columns and "pe" in meta.columns:
        meta = meta.rename(columns={"pe": "pe_snapshot"})
    for c in ("pe_snapshot", "eps_implied"):
        if c not in meta.columns:
            meta[c] = NAN
        meta[c] = pd.to_numeric(meta[c], errors="coerce")
    if "industry" not in meta.columns:
        meta["industry"] = None
    if "name" not in meta.columns:
        meta["name"] = meta["code"]
    meta = meta.drop_duplicates("code")
    index = index.drop_duplicates("date").sort_values("date")
    etf = etf.drop_duplicates("date").sort_values("date")
    return {"panel": panel, "meta": meta, "index": index, "etf": etf}


def regime_series(index: pd.DataFrame) -> pd.Series:
    """sim/settle.py::regime 同款：沪深 300 收盘 > MA120 且 MA20 > 5 日前 MA20 → offense；
    收盘 < MA120 且 < MA60 → defense；其余 neutral。数据不足 125 根时 defense。"""
    s = index.set_index("date")["close"].astype(float).sort_index()
    ma20 = s.rolling(20).mean()
    ma60 = s.rolling(60).mean()
    ma120 = s.rolling(120).mean()
    out = pd.Series("neutral", index=s.index, dtype=object)
    off = (s > ma120) & (ma20 > ma20.shift(5))
    dfn = (s < ma120) & (s < ma60)
    out[off] = "offense"
    out[dfn] = "defense"
    out.iloc[:125] = "defense"
    out[ma120.isna()] = "defense"
    return out


# ---------------------------------------------------------------- 特征（向量化）
FEATURE_COLS = [
    "close", "listed_days", "chg_1d", "chg_5d", "ret_20", "ret_60", "ret_120",
    "vol_20", "vol_60", "ma20", "ma60", "ma120", "hi60", "dd_60", "near_high_60",
    "avg_amount", "amt_ratio", "stop_prob", "one_lot_value", "dist_ma20", "dist_ma60", "dist_high60",
]


def build_features(panel: pd.DataFrame) -> pd.DataFrame:
    """逐股 groupby 向量化，语义对齐 sim/pool.py::features()（只用 T 及之前数据）。

    停牌日视为"该股序列里没有这一天"（与新浪日线返回一致）：先剔除无效收盘，再按 code 分组滚动。
    """
    p = panel.sort_values(["code", "date"], kind="mergesort").reset_index(drop=True)
    p = p[p["close"].notna() & (p["close"] > 0)].reset_index(drop=True)
    p["amount"] = p["amount"].fillna(0.0)
    g = p.groupby("code", sort=False)
    ret = g["close"].pct_change()

    def roll(series_by_code, n: int, how: str) -> pd.Series:
        r = getattr(series_by_code.rolling(n), how)()
        return r.reset_index(level=0, drop=True)

    c = p["close"]
    out = pd.DataFrame({"date": p["date"], "code": p["code"]})
    out["close"] = c
    out["listed_days"] = (g.cumcount() + 1).astype(float)
    out["chg_1d"] = ret
    out["chg_5d"] = c / g["close"].shift(5) - 1
    out["ret_20"] = c / g["close"].shift(20) - 1
    out["ret_60"] = c / g["close"].shift(60) - 1
    out["ret_120"] = c / g["close"].shift(120) - 1
    vol_20 = roll(ret.groupby(p["code"], sort=False), 20, "std") * TRADING_DAYS ** 0.5
    vol_60 = roll(ret.groupby(p["code"], sort=False), 60, "std") * TRADING_DAYS ** 0.5
    out["vol_20"] = vol_20
    out["vol_60"] = vol_60
    out["ma20"] = roll(g["close"], 20, "mean")
    out["ma60"] = roll(g["close"], 60, "mean")
    out["ma120"] = roll(g["close"], 120, "mean")
    hi60 = roll(g["high"], 60, "max")
    out["hi60"] = hi60
    amt20 = roll(g["amount"], 20, "mean")
    amt5 = roll(g["amount"], 5, "mean")
    out["avg_amount"] = amt20
    out["amt_ratio"] = np.where(amt20 > 0, amt5 / amt20.where(amt20 > 0, 1.0), 0.0)
    out["dd_60"] = np.where(hi60 > 0, c / hi60 - 1, NAN)
    out["near_high_60"] = (hi60 > 0) & (c >= 0.97 * hi60)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (-sim_pool.STOP_LOSS_PCT / (vol_60 * math.sqrt(sim_pool.STOP_HORIZON / TRADING_DAYS))) / math.sqrt(2.0)
    erf = np.frompyfunc(math.erf, 1, 1)
    sp = 1.0 + erf(z).astype(float)
    sp = np.where(np.isfinite(vol_60) & (vol_60 > 0), sp, NAN)
    out["stop_prob"] = sp
    out["one_lot_value"] = np.round(c * LOT, 2)
    out["dist_ma20"] = np.where(out["ma20"] > 0, c / out["ma20"] - 1, NAN)
    out["dist_ma60"] = np.where(out["ma60"] > 0, c / out["ma60"] - 1, NAN)
    out["dist_high60"] = np.where(hi60 > 0, c / hi60 - 1, NAN)
    out["valid"] = out["listed_days"] >= 130
    return out


# ---------------------------------------------------------------- 宽表
class Wide:
    """按 date × code 的宽表，供逐日切片/成交查询。"""

    def __init__(self, panel: pd.DataFrame, feats: pd.DataFrame, dates: list, etf: pd.DataFrame, index: pd.Series):
        self.dates = dates
        self.date_pos = {d: i for i, d in enumerate(dates)}
        sub = panel[panel["date"].isin(set(dates))]
        piv = sub.pivot(index="date", columns="code", values=["open", "high", "low", "close", "volume"])
        self.close = piv["close"].reindex(dates)
        self.high = piv["high"].reindex(dates)
        self.low = piv["low"].reindex(dates)
        self.open = piv["open"].reindex(dates)
        self.volume = piv["volume"].reindex(dates)
        self.prev_close = self.close.ffill().shift(1)
        with np.errstate(divide="ignore", invalid="ignore"):
            self.chg = self.close / self.prev_close - 1
        self.one_price = (self.high - self.low).abs() < 1e-9
        nums = pd.Series([c[2:] for c in self.close.columns], index=self.close.columns)
        thr = np.where(nums.str.startswith(("300", "301")), 0.195, 0.095)
        self.thr = pd.Series(thr, index=self.close.columns)
        self.limit_up = self.chg >= self.thr
        self.limit_down = self.chg <= -self.thr
        # 特征宽表（只保留逐日切片要用的）
        fs = feats[feats["date"].isin(set(dates))]
        pv = fs.pivot(index="date", columns="code", values=["ret_20", "avg_amount", "vol_60"])
        self.ret_20 = pv["ret_20"].reindex(dates)
        self.avg_amount = pv["avg_amount"].reindex(dates)
        self.vol_60 = pv["vol_60"].reindex(dates)
        # ETF
        e = etf.set_index("date").reindex(dates)
        self.etf_close = e["close"].astype(float)
        self.etf_prev = self.etf_close.ffill().shift(1)
        self.etf_chg = self.etf_close / self.etf_prev - 1
        self.etf_one_price = (e["high"] - e["low"]).abs() < 1e-9 if "high" in e else pd.Series(False, index=dates)
        self.etf_volume = e["volume"] if "volume" in e else pd.Series(NAN, index=dates)
        # 基准
        self.index_close = index.reindex(dates)
        self.index_chg = self.index_close.pct_change()

    def stock_close(self, d: str, code: str):
        try:
            v = self.close.at[d, code]
        except (KeyError, IndexError):
            return None
        return float(v) if pd.notna(v) else None

    def etf_px(self, d: str):
        try:
            v = self.etf_close.at[d]
        except KeyError:
            return None
        return float(v) if pd.notna(v) else None


# ---------------------------------------------------------------- 候选池重建
class PoolBuilder:
    """逐日：eligible 重算 → channels() → assemble() → triage()，与 sim/pool.py 同口径。"""

    def __init__(self, feats_sig: pd.DataFrame, meta: pd.DataFrame, w: Wide, pcfg: dict, rcfg: dict,
                 regime: pd.Series, defensive_pe: bool, defensive_max_pe: float = 25.0):
        self.pcfg = pcfg
        self.rcfg = rcfg
        self.w = w
        self.regime = regime
        self.defensive_pe = defensive_pe
        self.defensive_max_pe = defensive_max_pe
        self.name_map = dict(zip(meta["code"], meta["name"].astype(str)))
        self.industry_map = dict(zip(meta["code"], meta["industry"]))
        # pe 近似：pe_t = close_t / eps_implied（eps_implied = close_2026-10-23 / pe_2026-10-23）
        self.eps_map = dict(zip(meta["code"], meta["eps_implied"]))
        u = rcfg["universe"]
        self.exclude_prefixes = tuple(u["exclude_prefixes"])
        self.min_listed = u["min_listed_days"]
        self.min_amt = u["min_avg_amount"]
        self.max_1d = u["max_day_change"]
        self.max_5d = u["max_5d_change"]
        # 每日切片边界
        fs = feats_sig.sort_values(["date", "code"], kind="mergesort").reset_index(drop=True)
        self.feats = fs
        self.bounds = {}
        for d, idx in fs.groupby("date", sort=True).indices.items():
            self.bounds[d] = (int(idx.min()), int(idx.max()) + 1)
        # 静态代码级过滤（板块前缀 / ST）
        codes = pd.Index(fs["code"].unique())
        names = pd.Series(codes.map(self.name_map)).fillna("").astype(str)
        bad_prefix = pd.Series([c.startswith("bj") or c[2:].startswith(self.exclude_prefixes) for c in codes], index=codes)
        bad_st = names.str.contains("ST", case=False, regex=False) | names.str.contains("退", regex=False)
        self.code_ok = ~(bad_prefix | bad_st)
        self._pe_cache = {}

    def _pe_col(self, d: str, codes: pd.Series) -> pd.Series:
        close_row = self.w.close.loc[d]
        eps = pd.Series(codes.map(self.eps_map).values, index=codes.index, dtype=float)
        px = pd.Series(codes.map(close_row).values, index=codes.index, dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            pe = px / eps
        pe = pe.where(eps > 0, NAN)
        return pe

    def daily_pool(self, d: str) -> pd.DataFrame:
        """返回 T 日分诊后的候选池（含 action/priority/style）。"""
        if d not in self.bounds:
            return pd.DataFrame()
        s, e = self.bounds[d]
        f = self.feats.iloc[s:e]
        feat = f[f["valid"]].copy()
        if not len(feat):
            return pd.DataFrame()
        codes = feat["code"]
        ok = codes.map(self.code_ok).fillna(False).astype(bool)
        with np.errstate(invalid="ignore"):
            elig = (ok
                    & (feat["listed_days"] >= self.min_listed)
                    & (feat["avg_amount"] >= self.min_amt)
                    & (feat["chg_1d"] <= self.max_1d)
                    & (feat["chg_5d"] <= self.max_5d)
                    & (feat["close"] > 0))
        feat["eligible"] = elig.fillna(False).astype(bool)
        feat["name"] = codes.map(self.name_map).values
        feat["industry"] = codes.map(self.industry_map).values
        if self.defensive_pe:
            feat["pe"] = self._pe_col(d, codes)
        else:
            feat["pe"] = NAN
        chans = sim_pool.channels(feat, self.pcfg)
        if not self.defensive_pe:
            # defensive 主口径：lowvol 替代（close>MA120，vol_60 升序），PE 列置 NaN 后手动覆盖该通道
            base = feat[feat["eligible"] & (feat["close"] > feat["ma120"])]
            chans["defensive"] = (base.sort_values("vol_60", ascending=True)
                                  .head(self.pcfg["per_channel"])["code"].tolist())
        pool = sim_pool.assemble(feat, chans, [], [])
        if not len(pool):
            return pd.DataFrame()
        return sim_pool.triage(pool, self.regime.get(d, "neutral"))


# ---------------------------------------------------------------- 组合模拟
class Simulator:
    def __init__(self, ctx: dict, caliber: str):
        self.ctx = ctx
        self.cal = caliber
        self.cash = float(ctx["initial_cash"])
        self.positions: dict = {}
        self.trades: list = []
        self.nav_rows: list = []
        self.peak = float(ctx["initial_cash"])
        self.halted = False
        self.breaker_until = None
        self.breaker_peak = None
        self.dd_warn = False
        self.week_new: dict = {}
        self.last_equity = self.cash
        self.events: list = []
        self.skipped_lot = 0
        self.trade_seq = 0
        self._first_buy: dict = {}

    # ---------- 价格/估值 ----------
    def _amt(self, d: str, code: str) -> float:
        """T 日 avg_amount20（滑点分档用）；取不到返回 NaN（触发最高滑点档）。"""
        try:
            return _nan(self.ctx["w"].avg_amount.at[d, code])
        except KeyError:
            return NAN

    def _px(self, d: str, code: str, kind: str):
        if kind == "etf":
            return self.ctx["w"].etf_px(d)
        return self.ctx["w"].stock_close(d, code)

    def _value(self, d: str) -> tuple:
        sv = ev = 0.0
        for code, pos in self.positions.items():
            px = self._px(d, code, pos["kind"])
            if px is None:
                px = pos["last_price"]
            pos["last_price"] = px
            v = pos["shares"] * px
            if pos["kind"] == "stock":
                sv += v
            else:
                ev += v
        return sv, ev, self.cash + sv + ev

    def _band(self, regime: str, state: str) -> dict:
        """regime band；warn 时 stock_max 降一档（risk.yaml drawdown.warn）。

        熔断/暂停期间不用 breaker band 的 stock_max=0（规格只要求暂停新开，不强制清仓）。
        """
        rcfg = self.ctx["rcfg"]
        band = dict(rcfg["bands"][regime])
        band["min"] = band.get("min", 0.0)
        band["max"] = band.get("max", 1.0)
        if self.ctx["warn_downgrade"] and state == "warn":
            i = min(sim_risk.TIERS.index(regime) + 1, len(sim_risk.TIERS) - 1)
            band["stock_max"] = min(band["stock_max"], rcfg["bands"][sim_risk.TIERS[i]]["stock_max"])
        return band

    def _risk_state(self, d: str) -> str:
        if self.halted:
            return "halt"
        if self.breaker_until and d <= self.breaker_until:
            return "breaker"
        return "warn" if self.dd_warn else "normal"

    def mark(self, d: str, regime: str) -> dict:
        sv, ev, eq = self._value(d)
        self.last_equity = eq
        self.peak = max(self.peak, eq)
        dd = 1 - eq / self.peak if self.peak > 0 else 0.0
        rcfg = self.ctx["rcfg"]
        dd_cfg = rcfg["drawdown"]
        if dd >= dd_cfg["halt"] and not self.halted:
            self.halted = True
            self.events.append({"date": d, "event": "halt", "drawdown": round(dd, 4)})
        elif dd >= dd_cfg["breaker"] and self.breaker_peak != self.peak:
            i = self.ctx["w"].date_pos.get(d, 0)
            until = self.ctx["dates"][min(i + dd_cfg["breaker_days"], len(self.ctx["dates"]) - 1)]
            self.breaker_until = until
            self.breaker_peak = self.peak
            self.events.append({"date": d, "event": "breaker", "drawdown": round(dd, 4), "until": until})
        elif self.halted and eq >= self.peak - 1e-9:
            # 规格未定义 resume（实盘需人工 resume）；回测用"机械口径"：净创新高自动恢复
            self.halted = False
            self.events.append({"date": d, "event": "halt_resume", "drawdown": round(dd, 4)})
        self.dd_warn = dd >= WARN_JUMP
        state = self._risk_state(d)
        row = {"date": d, "equity": round(eq, 2), "cash": round(self.cash, 2),
               "stock_value": round(sv, 2), "etf_value": round(ev, 2),
               "stock_weight": round(sv / eq, 4) if eq > 0 else 0.0,
               "etf_weight": round(ev / eq, 4) if eq > 0 else 0.0,
               "regime": regime, "drawdown": round(dd, 4),
               "peak": round(self.peak, 2), "risk_state": state,
               "n_stocks": sum(1 for p in self.positions.values() if p["kind"] == "stock")}
        self.nav_rows.append(row)
        return row

    # ---------- 交易记录 ----------
    def _trade(self, **kw) -> None:
        self.trade_seq += 1
        row = {"seq": self.trade_seq, "date": "", "code": "", "name": "", "side": "", "shares": 0, "price": NAN,
               "amount": 0.0, "fees": 0.0, "reason": "", "holding_days": NAN, "pnl": NAN,
               "status": "filled", "kind": "stock", "commission": 0.0, "stamp_tax": 0.0,
               "transfer_fee": 0.0, "slippage_cost": 0.0, "weight_target": NAN, "decided_on": "",
               "note": "", "priority": NAN, "regime": ""}
        row.update(kw)
        self.trades.append(row)

    # ---------- 决策（T 收盘） ----------
    def decide(self, d: str, state: str, regime: str) -> list:
        ctx, rcfg = self.ctx, self.ctx["rcfg"]
        p = rcfg["position"]
        eq = self.last_equity
        sv, ev, _ = self._value(d)
        orders: list = []
        exiting: set = set()
        w = ctx["w"]

        def stop_px(code):
            pos = self.positions[code]
            return pos["cost"] / pos["shares"] if pos["shares"] > 0 else 0.0

        # 1) 止损：T 收盘 ≤ 成本 × (1-8%) → T+1 收盘卖，不受最短持有期限制
        for code, pos in self.positions.items():
            if pos["kind"] != "stock" or code in exiting:
                continue
            px = w.stock_close(d, code)
            if px is None:
                continue
            cp = stop_px(code)
            if cp > 0 and px <= cp * (1 - ctx["stop_loss"]) + 1e-12:
                orders.append({"code": code, "kind": "stock", "name": pos["name"], "action": "sell_all",
                               "target_weight": 0.0, "reason": "stop_loss", "decided_on": d,
                               "avg_amount": self._amt(d, code), "regime": regime, "priority": NAN})
                exiting.add(code)
        # 2) 时间止损：持有 ≥20 交易日且市值 < 成本
        for code, pos in self.positions.items():
            if pos["kind"] != "stock" or code in exiting:
                continue
            px = w.stock_close(d, code)
            if px is None:
                continue
            held = w.date_pos[d] - w.date_pos.get(pos["first_date"], w.date_pos[d])
            if held >= p["time_stop_days"] and px * pos["shares"] < pos["cost"] - 1e-9:
                orders.append({"code": code, "kind": "stock", "name": pos["name"], "action": "sell_all",
                               "target_weight": 0.0, "reason": "time_stop", "decided_on": d,
                               "avg_amount": self._amt(d, code), "regime": regime, "priority": NAN})
                exiting.add(code)

        band = self._band(regime, state)
        # 3) regime 降档导致个股总仓位 > stock_max → 按 ret_20 最差顺序减到不超限（P3 忽略 band）
        sv_after = 0.0
        for code, pos in self.positions.items():
            if pos["kind"] != "stock" or code in exiting:
                continue
            px = w.stock_close(d, code) or pos["last_price"]
            sv_after += pos["shares"] * px
        if self.cal != "P3" and state not in ("breaker", "halt"):
            sw = sv_after / eq if eq > 0 else 0.0
            if sw > band["stock_max"] + TRIM_TOL:
                cands = []
                for code, pos in self.positions.items():
                    if pos["kind"] != "stock" or code in exiting:
                        continue
                    r20 = _nan(w.ret_20.at[d, code]) if code in w.ret_20.columns else NAN
                    pxv = w.stock_close(d, code) or pos["last_price"]
                    cands.append((r20, code, pos["shares"] * pxv))
                cands.sort(key=lambda x: (not math.isfinite(x[0]), x[0] if math.isfinite(x[0]) else 0.0))
                for r20, code, val in cands:
                    if sw <= band["stock_max"] + 1e-9:
                        break
                    pos = self.positions[code]
                    orders.append({"code": code, "kind": "stock", "name": pos["name"], "action": "sell_all",
                                   "target_weight": 0.0, "reason": "regime_trim", "decided_on": d,
                                   "avg_amount": self._amt(d, code), "regime": regime,
                                   "ret_20": r20, "priority": NAN})
                    exiting.add(code)
                    sw -= val / eq if eq > 0 else 0.0
                    sv_after -= val

        # 4) 买入（熔断/暂停期间禁止新开）
        stock_w_after = sv_after / eq if eq > 0 else 0.0
        if state not in ("breaker", "halt") and eq > 0:
            slots = p["max_stocks"] - sum(
                1 for c, pos in self.positions.items() if pos["kind"] == "stock" and c not in exiting)
            next_date = ctx["dates"][min(w.date_pos[d] + 1, len(ctx["dates"]) - 1)]
            wk = _iso_week(next_date)
            n_new = self.week_new.get(wk, 0)
            if self.cal == "P1":
                target_w = ctx["p1_single"]
            elif self.cal == "P2":
                target_w = band["stock_max"] / p["max_stocks"]
            else:
                target_w = 1.0 / p["max_stocks"]
            target_w = min(target_w, p["max_single"])
            capacity = band["stock_max"] - stock_w_after if self.cal != "P3" else 1.0
            ind_w: dict = {}
            for code, pos in self.positions.items():
                if pos["kind"] != "stock" or code in exiting:
                    continue
                ind = pos.get("industry") or "未分类"
                pxv = w.stock_close(d, code) or pos["last_price"]
                ind_w[ind] = ind_w.get(ind, 0.0) + pos["shares"] * pxv / eq
            ready = ctx["pools"].get(d)
            if ready is not None and len(ready):
                for _, r in ready.iterrows():
                    if slots <= 0 or n_new >= p["max_new_per_week"]:
                        break
                    code = r["code"]
                    if code in self.positions or code in exiting:
                        continue
                    px_t = _nan(r.get("close"))
                    if not math.isfinite(px_t) or px_t <= 0:
                        continue
                    if px_t * LOT > target_w * eq * 1.01:   # 一手金额超过目标仓位（实盘风控直接拒绝）
                        self.skipped_lot += 1
                        continue
                    if self.cal != "P3" and target_w > capacity + 1e-9:
                        break
                    ind = r.get("industry") if pd.notna(r.get("industry")) else "未分类"
                    if ind_w.get(ind, 0.0) + target_w > p["max_industry"] + 1e-9:
                        continue
                    orders.append({"code": code, "kind": "stock", "name": r.get("name", ""), "action": "buy",
                                   "target_weight": target_w, "reason": "entry", "decided_on": d,
                                   "avg_amount": _nan(r.get("avg_amount")), "regime": regime,
                                   "priority": _nan(r.get("priority")), "industry": ind,
                                   "style": r.get("style", "")})
                    slots -= 1
                    n_new += 1
                    capacity -= target_w
                    ind_w[ind] = ind_w.get(ind, 0.0) + target_w
                    self.week_new[wk] = n_new

        # 5) P1 底仓 ETF：补到 band_max − 个股权重，偏离 >5pp 才调
        if self.cal == "P1" and eq > 0:
            planned = sum(o["target_weight"] for o in orders if o["kind"] == "stock" and o["action"] == "buy")
            etf_target = max(0.0, band["max"] - (stock_w_after + planned))
            etf_now = ev / eq
            has_buy = planned > 0
            if has_buy or abs(etf_target - etf_now) > p["etf_rebalance_tolerance"]:
                orders.append({"code": ctx["etf_code"], "kind": "etf", "name": ctx["etf_name"],
                               "action": "target", "target_weight": etf_target, "reason": "park_etf",
                               "decided_on": d, "avg_amount": NAN, "regime": regime, "priority": NAN})
        return orders

    # ---------- 执行（T+1 收盘） ----------
    def execute(self, d: str, orders: list) -> None:
        ctx, bcfg = self.ctx, self.ctx["broker"]
        w = ctx["w"]
        sv, ev, eq = self._value(d)
        plans = []
        for o in orders:
            code, kind = o["code"], o["kind"]
            px = self._px(d, code, kind)
            if px is None or px <= 0:
                self._no_fill(d, o, "停牌/无成交数据")
                if o["action"] == "buy":
                    self.week_new[_iso_week(d)] = max(0, self.week_new.get(_iso_week(d), 1) - 1)
                continue
            cur = self.positions.get(code, {}).get("shares", 0)
            if o["action"] == "sell_all":
                target = 0
            else:
                target = _floor_lot(o["target_weight"] * eq / px)
            plans.append((o, px, cur, target))

        sells = sorted([p for p in plans if p[3] < p[2]], key=lambda x: 0 if x[0]["kind"] == "stock" else 1)
        buys = sorted([p for p in plans if p[3] > p[2]], key=lambda x: 1 if x[0]["kind"] == "etf" else 0)
        for o, px, cur, target in sells:
            self._exec_sell(d, o, px, cur, target)
        for o, px, cur, target in buys:
            self._exec_buy(d, o, px, cur, target, eq)

    def _no_fill(self, d: str, o: dict, note: str) -> None:
        self._trade(date=d, code=o["code"], name=o.get("name", ""), side="buy" if o["action"] == "buy" else "sell",
                    status="no_fill", kind=o["kind"], reason=o["reason"], decided_on=o.get("decided_on", ""),
                    weight_target=_r(o.get("target_weight")), note=f"no_fill: {note}",
                    regime=o.get("regime", ""), priority=_r(o.get("priority"), 3))

    def _exec_sell(self, d: str, o: dict, px: float, cur: float, target: float) -> None:
        ctx, bcfg = self.ctx, self.ctx["broker"]
        code, kind = o["code"], o["kind"]
        pos = self.positions[code]
        w = ctx["w"]
        if kind == "stock" and bool(w.one_price.at[d, code]) and _nan(w.chg.at[d, code]) < 0:
            self._no_fill(d, o, "一字跌停无法卖出")
            return
        qty = min(cur, cur - target)
        qty = _floor_lot(qty)
        vol = _nan(w.volume.at[d, code]) if kind == "stock" else _nan(w.etf_volume.at[d])
        if math.isfinite(vol) and vol > 0:
            qty = min(qty, _floor_lot(vol * bcfg["volume_cap_ratio"]))
        if qty <= 0:
            self._no_fill(d, o, "成交量上限，未成交")
            return
        slip = sim_broker.slippage_rate(bcfg, kind, _nan(o.get("avg_amount")), "snapshot") * ctx["slippage_mult"]
        fill = round(px * (1 - slip), 3)
        gross = qty * fill
        c, s, t = sim_broker.fees(bcfg, kind, "sell", gross)
        net = gross - c - s - t
        cost_part = pos["cost"] * qty / cur
        pnl = net - cost_part
        self.cash += net
        pos["shares"] -= qty
        pos["cost"] -= cost_part
        held = w.date_pos[d] - w.date_pos.get(self._first_buy.get(code, d), w.date_pos[d])
        if pos["shares"] < 1e-6:
            del self.positions[code]
        self._trade(date=d, code=code, name=o.get("name", ""), side="sell", shares=qty, price=fill,
                    amount=round(gross, 2), fees=round(c + s + t, 2), reason=o["reason"],
                    holding_days=int(held) if math.isfinite(held) else NAN, pnl=round(pnl, 2),
                    kind=kind, commission=c, stamp_tax=s, transfer_fee=t,
                    slippage_cost=round((px - fill) * qty, 2), decided_on=o.get("decided_on", ""),
                    regime=o.get("regime", ""), priority=_r(o.get("priority"), 3), note="")

    def _exec_buy(self, d: str, o: dict, px: float, cur: float, target: float, eq: float) -> None:
        ctx, bcfg = self.ctx, self.ctx["broker"]
        code, kind = o["code"], o["kind"]
        w = ctx["w"]
        if kind == "stock" and bool(w.one_price.at[d, code]) and _nan(w.chg.at[d, code]) > 0:
            self._no_fill(d, o, "一字涨停无法买入")
            self.week_new[_iso_week(d)] = max(0, self.week_new.get(_iso_week(d), 1) - 1)
            return
        if kind == "etf" and bool(w.etf_one_price.at[d]) and _nan(w.etf_chg.at[d]) > 0:
            self._no_fill(d, o, "ETF 一字涨停无法买入")
            return
        slip = sim_broker.slippage_rate(bcfg, kind, _nan(o.get("avg_amount")), "snapshot") * ctx["slippage_mult"]
        fill = round(px * (1 + slip), 3)
        qty = _floor_lot(target - cur)
        vol = _nan(w.volume.at[d, code]) if kind == "stock" else _nan(w.etf_volume.at[d])
        if math.isfinite(vol) and vol > 0:
            qty = min(qty, _floor_lot(vol * bcfg["volume_cap_ratio"]))
        note = ""
        while qty > 0:
            gross = qty * fill
            c, s, t = sim_broker.fees(bcfg, kind, "buy", gross)
            if gross + c + s + t <= self.cash + 1e-9:
                break
            qty -= LOT
            note = "现金不足缩量"
        if qty <= 0:
            self._no_fill(d, o, note or "现金不足/不足一手")
            if o["action"] == "buy":
                self.week_new[_iso_week(d)] = max(0, self.week_new.get(_iso_week(d), 1) - 1)
            return
        gross = qty * fill
        c, s, t = sim_broker.fees(bcfg, kind, "buy", gross)
        self.cash -= gross + c + s + t
        pos = self.positions.get(code)
        new_pos = pos is None
        if new_pos:
            pos = self.positions[code] = {"name": o.get("name", ""), "kind": kind, "shares": 0.0, "cost": 0.0,
                                          "first_date": d, "last_price": px, "stop_price": NAN,
                                          "industry": o.get("industry"), "role": "park" if kind == "etf" else "stock"}
            self._first_buy[code] = d
        pos["shares"] += qty
        pos["cost"] += gross + c + s + t
        if kind == "stock":
            pos["stop_price"] = round(pos["cost"] / pos["shares"] * (1 - ctx["stop_loss"]), 3)
        self._trade(date=d, code=code, name=o.get("name", ""), side="buy", shares=qty, price=fill,
                    amount=round(gross, 2), fees=round(c + s + t, 2), reason=o["reason"], holding_days=0 if new_pos else NAN,
                    pnl=NAN, kind=kind, commission=c, stamp_tax=s, transfer_fee=t,
                    slippage_cost=round((fill - px) * qty, 2), decided_on=o.get("decided_on", ""),
                    weight_target=_r(o.get("target_weight")), note=note, regime=o.get("regime", ""),
                    priority=_r(o.get("priority"), 3), new_position=new_pos and kind == "stock")

    # ---------- 主循环 ----------
    def run(self) -> dict:
        ctx = self.ctx
        pending: list = []

        dates = ctx["dates"]
        for i, d in enumerate(dates):
            if pending:
                self.execute(d, pending)
                pending = []
            regime = ctx["regime"].get(d, "neutral")
            row = self.mark(d, regime)
            state = row["risk_state"]
            if i < len(dates) - 1:
                pending = self.decide(d, state, regime)
        eq_end = self.nav_rows[-1]["equity"] if self.nav_rows else self.cash
        positions = []
        for code, pos in self.positions.items():
            v = pos["shares"] * pos["last_price"]
            cps = pos["cost"] / pos["shares"] if pos["shares"] > 0 else NAN
            positions.append({"code": code, "name": pos["name"], "kind": pos["kind"],
                              "industry": pos.get("industry") or "未分类",
                              "shares": pos["shares"], "last_price": round(pos["last_price"], 3),
                              "value": round(v, 2), "weight": _r(v / eq_end, 4) if eq_end else NAN,
                              "cost_per_share": round(cps, 4) if math.isfinite(cps) else NAN,
                              "unrealized": _r(pos["last_price"] / cps - 1, 4) if math.isfinite(cps) and cps else NAN,
                              "first_date": pos["first_date"],
                              "hold_days": (len(self.ctx["dates"]) - 1) -
                                           self.ctx["w"].date_pos.get(pos["first_date"], 0)})
        positions.sort(key=lambda x: -x["value"])
        return {"nav": pd.DataFrame(self.nav_rows), "trades": pd.DataFrame(self.trades),
                "events": self.events, "skipped_lot": self.skipped_lot, "positions": positions}


# ---------------------------------------------------------------- 指标
def _drawdown_stats(equity: pd.Series) -> tuple:
    peak = equity.cummax()
    dd = 1 - equity / peak
    mdd = float(dd.max()) if len(dd) else 0.0
    cur = None
    longest = 0
    for i, v in enumerate(dd.to_numpy()):
        if v > 1e-12:
            if cur is None:
                cur = i
        else:
            if cur is not None:
                longest = max(longest, i - cur)
                cur = None
    if cur is not None:
        longest = max(longest, len(dd) - 1 - cur)
    return mdd, longest


def _series_metrics(dates: pd.Series, equity: pd.Series) -> dict:
    eq = equity.astype(float).reset_index(drop=True)
    n = len(eq) - 1
    if n <= 0:
        return {}
    rets = eq.pct_change().dropna()
    total = float(eq.iloc[-1] / eq.iloc[0] - 1)
    cagr = (1 + total) ** (TRADING_DAYS / n) - 1 if n > 0 else NAN
    mdd, dur = _drawdown_stats(eq)
    vol = float(rets.std() * TRADING_DAYS ** 0.5) if len(rets) > 1 else NAN
    sharpe = float(rets.mean() / rets.std() * TRADING_DAYS ** 0.5) if len(rets) > 1 and rets.std() > 0 else NAN
    return {"start": str(dates.iloc[0]), "end": str(dates.iloc[-1]), "trading_days": int(n),
            "total_return": _r(total, 6), "cagr": _r(cagr, 6), "max_drawdown": _r(mdd, 6),
            "max_dd_duration_days": int(dur), "annual_vol": _r(vol, 6), "sharpe": _r(sharpe, 4),
            "calmar": _r(cagr / abs(mdd), 4) if mdd > 0 and math.isfinite(cagr) else NAN}


def compute_metrics(nav: pd.DataFrame, trades: pd.DataFrame, label: str, extra: dict) -> dict:
    eq = nav["equity"].astype(float)
    base = _series_metrics(nav["date"], eq)
    base["label"] = label
    base["end_equity"] = round(float(eq.iloc[-1]), 2)
    base["end_cash"] = round(float(nav["cash"].iloc[-1]), 2)
    base["end_stock_value"] = round(float(nav["stock_value"].iloc[-1]), 2)
    base["end_etf_value"] = round(float(nav["etf_value"].iloc[-1]), 2)
    # 交易统计
    if len(trades) == 0:
        trades = pd.DataFrame(columns=["status", "side", "kind", "pnl", "amount", "fees", "holding_days"])
    filled = trades[trades["status"] == "filled"]
    sells = filled[(filled["side"] == "sell") & (filled["kind"] == "stock")] if len(filled) else filled
    closed_pnl = sells["pnl"].astype(float).dropna() if len(sells) else pd.Series(dtype=float)
    wins = int((closed_pnl > 0).sum())
    n_closed = int(len(closed_pnl))
    turnover_notional = float(filled["amount"].astype(float).sum()) if len(filled) else 0.0
    years = base["trading_days"] / TRADING_DAYS if base.get("trading_days") else NAN
    mean_eq = float(eq.mean())
    base.update({
        "n_trades_filled": int(len(filled)),
        "n_buys": int((filled["side"] == "buy").sum()) if len(filled) else 0,
        "n_sells": int((filled["side"] == "sell").sum()) if len(filled) else 0,
        "n_no_fill": int((trades["status"] == "no_fill").sum()) if len(trades) else 0,
        "n_closed_stock": n_closed,
        "win_rate": _r(wins / n_closed, 4) if n_closed else NAN,
        "avg_holding_days": _r(sells["holding_days"].astype(float).mean(), 2) if len(sells) else NAN,
        "avg_win_pnl": _r(closed_pnl[closed_pnl > 0].mean(), 2) if wins else NAN,
        "avg_loss_pnl": _r(closed_pnl[closed_pnl <= 0].mean(), 2) if n_closed - wins else NAN,
        "total_fees": _r(filled["fees"].astype(float).sum(), 2) if len(filled) else 0.0,
        "turnover_annual": _r(turnover_notional / mean_eq / years, 4) if years and mean_eq > 0 else NAN,
        "skipped_lot": int(extra.get("skipped_lot", 0)),
        "n_breaker_events": int(sum(1 for e in extra.get("events", []) if e["event"] == "breaker")),
        "n_halt_events": int(sum(1 for e in extra.get("events", []) if e["event"] == "halt")),
    })
    # 子区间
    subs = {"2024-10-24~2025-09-30": ("2024-10-24", "2025-09-30"),
            "2025-10-08~2026-10-23": ("2025-10-08", "2026-10-23")}
    base["subperiods"] = {}
    for name, (a, b) in subs.items():
        m = (nav["date"] >= a) & (nav["date"] <= b)
        if m.sum() < 3:
            continue
        sub = nav[m].reset_index(drop=True)
        base["subperiods"][name] = _series_metrics(sub["date"], sub["equity"])
        base["subperiods"][name]["avg_stock_weight"] = _r(sub["stock_weight"].mean(), 4)
        base["subperiods"][name]["avg_etf_weight"] = _r(sub["etf_weight"].mean(), 4)
    # 年度
    base["years"] = {}
    for y in sorted(set(d[:4] for d in nav["date"])):
        m = nav["date"].str[:4] == y
        if m.sum() < 3:
            continue
        sub = nav[m].reset_index(drop=True)
        base["years"][y] = _series_metrics(sub["date"], sub["equity"])
    # regime 分解
    base["by_regime"] = {}
    rets = eq.pct_change()
    for reg in ("offense", "neutral", "defense"):
        m = nav["regime"] == reg
        if not m.any():
            continue
        r = rets[m].dropna()
        cum = float(np.prod(1 + r.to_numpy()) - 1) if len(r) else 0.0
        base["by_regime"][reg] = {"days": int(m.sum()), "cum_return": _r(cum, 6),
                                  "annual_vol": _r(r.std() * TRADING_DAYS ** 0.5, 6) if len(r) > 1 else NAN,
                                  "avg_stock_weight": _r(nav.loc[m, "stock_weight"].mean(), 4),
                                  "avg_drawdown": _r(nav.loc[m, "drawdown"].mean(), 4)}
    return base


def benchmark_metrics(ctx: dict) -> dict:
    w = ctx["w"]
    dates = ctx["dates"]
    nav_dates = ctx["nav_dates"]
    out = {}
    for name, series in (("sh000300", w.index_close), ("sh510300_buyhold", w.etf_close)):
        s = series.reindex(nav_dates)
        if s.notna().sum() < 3:
            continue
        s = s.ffill()
        eq = s / s.iloc[0] * ctx["initial_cash"]
        m = _series_metrics(pd.Series(nav_dates), eq)
        out[name] = m
    # 等权合格池：每月首个交易日按上一交易日 eligible 名单等权，持有到下月（计换手成本 0.15%/次）
    elig = ctx["eligible_by_date"]
    close = w.close
    month_of = pd.Series([d[:7] for d in dates], index=dates)
    rebal_idx = [i for i, d in enumerate(dates) if i == 0 or month_of.iloc[i] != month_of.iloc[i - 1]]
    rets = pd.Series(0.0, index=dates)
    for k, i in enumerate(rebal_idx):
        start = i
        end = rebal_idx[k + 1] if k + 1 < len(rebal_idx) else len(dates)
        src = dates[i - 1] if i > 0 else dates[i]
        codes = elig.get(src, [])
        if not codes:
            continue
        lo = max(0, start - 1)
        sub = close.iloc[lo:end]
        sub = sub[[c for c in codes if c in sub.columns]]
        if sub.shape[1] == 0:
            continue
        pr = sub.pct_change().iloc[1:].mean(axis=1)
        r = pd.Series(pr.to_numpy(), index=dates[start:start + len(pr)]).reindex(dates[start:end])
        if i > 0 and len(r):
            r.iloc[0] = r.iloc[0] - 0.0015
        rets.iloc[start:end] = r.fillna(0.0).to_numpy()
    rets = rets.fillna(0.0)
    eq = (1 + rets).cumprod() * ctx["initial_cash"]
    out["ew_eligible"] = _series_metrics(pd.Series(dates), pd.Series(eq.to_numpy()))
    out["ew_eligible"]["note"] = "每月首个交易日按前一交易日合格池等权调仓，换手成本 0.15%/次"
    return out


# ---------------------------------------------------------------- 前向收益
def selection_forward(ctx: dict) -> tuple:
    w = ctx["w"]
    dates = ctx["dates"]
    rows = []
    n_ready_days = 0
    for d, ready in ctx["pools"].items():
        if ready is None or not len(ready):
            continue
        ready = ready[ready["action"] == "ready"]
        if not len(ready):
            continue
        n_ready_days += 1
        i = w.date_pos[d]
        base = w.close.iloc[i]
        idx_base = w.index_close.iloc[i]
        for _, r in ready.iterrows():
            code = r["code"]
            row = {"T": d, "code": code, "name": r.get("name", ""), "style": r.get("style", ""),
                   "priority": _r(r.get("priority"), 3), "action": r.get("action", ""),
                   "industry": r.get("industry") if pd.notna(r.get("industry")) else "",
                   "close": _r(r.get("close"), 3)}
            for n in (5, 20, 60):
                if i + n < len(dates):
                    c1 = w.close.iat[i + n, w.close.columns.get_loc(code)] if code in w.close.columns else NAN
                    if pd.notna(c1) and pd.notna(base.get(code)):
                        fwd = float(c1) / float(base[code]) - 1
                    else:
                        fwd = NAN
                    i1 = w.index_close.iloc[i + n]
                    idx_fwd = float(i1) / float(idx_base) - 1 if pd.notna(i1) and pd.notna(idx_base) else NAN
                else:
                    fwd = idx_fwd = NAN
                row[f"fwd{n}"] = _r(fwd)
                row[f"excess{n}"] = _r(fwd - idx_fwd) if math.isfinite(_nan(fwd)) and math.isfinite(_nan(idx_fwd)) else NAN
            rows.append(row)
    df = pd.DataFrame(rows)
    stats = {"n_ready_days": n_ready_days,
             "avg_ready_per_day": _r(len(df) / n_ready_days, 2) if n_ready_days else NAN}
    if len(df):
        for n in (5, 20, 60):
            col = df[f"fwd{n}"].astype(float).dropna()
            exc = df[f"excess{n}"].astype(float).dropna()
            stats[f"fwd{n}"] = {"n": int(len(col)), "mean": _r(col.mean(), 6), "median": _r(col.median(), 6),
                                "hit_rate": _r((col > 0).mean(), 4), "mean_excess": _r(exc.mean(), 6),
                                "excess_hit_rate": _r((exc > 0).mean(), 4)}
        stats["style_dist"] = df["style"].value_counts().to_dict()
        # 每日 priority 前三（LLM 从名单里挑 3-5 只的代理）
        for k in (1, 3):
            sub = df.sort_values(["T", "priority"], ascending=[True, False]).groupby("T").head(k)
            for n in (5, 20):
                col = sub[f"fwd{n}"].astype(float).dropna()
                stats[f"top{k}_fwd{n}"] = {"n": int(len(col)), "mean": _r(col.mean(), 6),
                                           "hit_rate": _r((col > 0).mean(), 4)}
    return df, stats


# ---------------------------------------------------------------- 自检
def recon_check(ctx: dict, recon_csv: Path) -> dict:
    """与 data/cache/pool/<日期>_features.csv 对账（重叠字段，相对误差 < 1e-6）。
    Lead 提示：个别票在 2026-10-24 之后除权，前复权因子把历史价整体下移，属正常口径差异；
    失败判据：某字段相对差 > 0.5% 的股票数 > 20。"""
    out = {"file": str(recon_csv), "passed": None, "fields": {}, "notes": []}
    if not recon_csv.exists():
        out["notes"].append("对账文件不存在，跳过")
        return out
    ref = pd.read_csv(recon_csv, dtype={"code": str})
    date = recon_csv.stem.split("_")[0]
    out["date"] = date
    mine = ctx["feats"].copy()
    mine = mine[mine["date"] == date]
    if not len(mine):
        out["notes"].append(f"重算特征中没有 {date}，跳过")
        return out
    fields = ["close", "chg_1d", "chg_5d", "ret_20", "ret_60", "ret_120", "vol_20", "vol_60",
              "ma20", "ma60", "ma120", "dd_60", "avg_amount", "amt_ratio"]
    m = ref.merge(mine[["code"] + fields + ["near_high_60"]], on="code", how="inner", suffixes=("_ref", "_new"))
    out["matched_codes"] = int(len(m))
    out["ref_codes"] = int(len(ref))
    bad_total = 0
    for f in fields:
        a = pd.to_numeric(m[f + "_ref"], errors="coerce")
        b = pd.to_numeric(m[f + "_new"], errors="coerce")
        ok = a.notna() & b.notna()
        if not ok.any():
            out["fields"][f] = {"n": 0}
            continue
        rel = ((a[ok] - b[ok]).abs() / b[ok].abs().where(b[ok].abs() > 1e-12, 1.0))
        n_bad = int((rel > 0.005).sum())
        bad_total = max(bad_total, n_bad)
        out["fields"][f] = {"n": int(ok.sum()), "max_rel_diff": _r(rel.max(), 10),
                            "n_rel_gt_1e-6": int((rel > 1e-6).sum()), "n_rel_gt_0.5pct": n_bad}
    a = m["near_high_60_ref"].astype(str).str.lower() == "true"
    b = m["near_high_60_new"].astype(str).str.lower() == "true"
    out["fields"]["near_high_60"] = {"n": int(len(m)), "n_mismatch": int((a != b).sum())}
    out["max_bad_codes"] = bad_total
    out["passed"] = bad_total <= 20
    out["notes"].append("判据（Lead 校准）：单字段相对差>0.5% 的股票数 ≤20 视为通过（除权导致的前复权因子差异）")
    # listed_days 口径不同（池子 features 用 400 自然日窗口，回测用 2024-01-02 起面板），只提示不判失败
    if "listed_days" in ref.columns:
        out["notes"].append("listed_days 不回对账：候选池 features 取 400 自然日窗口（约 270 交易日），"
                            "回测取面板起点 2024-01-02 起，两者观测数口径不同；两者均 ≥60 时资格判定一致")
    return out


def no_lookahead_check(ctx: dict, sample: int = 40, seed: int = 7) -> dict:
    """抽样：用「该股 ≤T 的全部日线」调 sim_pool.features() 重算，与向量化特征逐字段比对。
    既验证向量化实现与 sim/pool 一致，也验证特征只用到 T 及之前数据（不含未来函数）。"""
    rng = np.random.default_rng(seed)
    panel = ctx["panel"]
    by_code = {c: g for c, g in panel.groupby("code")}
    dates_all = sorted(panel["date"].unique())
    sig = [d for d in ctx["dates"] if d in set(dates_all)]
    picks = []
    for _ in range(sample * 3):
        if len(picks) >= sample:
            break
        d = sig[int(rng.integers(0, len(sig)))]
        code = str(rng.choice(np.array(sorted(by_code.keys()))))
        picks.append((d, code))
    fields = ["close", "chg_1d", "chg_5d", "ret_20", "ret_60", "ret_120", "vol_20", "vol_60",
              "ma20", "ma60", "ma120", "hi60", "dd_60", "avg_amount", "amt_ratio", "stop_prob",
              "one_lot_value", "dist_ma20", "dist_ma60", "dist_high60"]
    feats = ctx["feats"].set_index(["date", "code"], drop=False)
    rows, worst, n_checked = [], 0.0, 0
    for d, code in picks:
        g = by_code.get(code)
        if g is None:
            continue
        sub = g[g["date"] <= d].sort_values("date")
        ref = sim_pool.features(sub)
        if ref is None:
            continue
        key = (d, code)
        if key not in feats.index:
            continue
        mine = feats.loc[key]
        if isinstance(mine, pd.DataFrame):
            mine = mine.iloc[0]
        n_checked += 1
        for f in fields:
            if f not in mine or f not in ref:
                continue
            a, b = _nan(mine.get(f)), _nan(ref.get(f))
            if not (math.isfinite(a) or math.isfinite(b)):
                continue
            rel = abs(a - b) / max(1.0, abs(b)) if math.isfinite(b) else abs(a - b)
            worst = max(worst, rel)
            rows.append({"date": d, "code": code, "field": f, "mine": a, "ref": b, "rel": rel})
    return {"n_pairs_checked": n_checked, "max_rel_diff": _r(worst, 12),
            "passed": bool(n_checked >= 10 and worst < 1e-6), "fields": fields}


def cash_consistency(nav: pd.DataFrame, trades: pd.DataFrame, initial: float) -> dict:
    """P1 期末净值 = 现金 + 持仓市值；且现金可由成交流水递推。"""
    out = {}
    last = nav.iloc[-1]
    implied = float(last["cash"]) + float(last["stock_value"]) + float(last["etf_value"])
    out["nav_equity"] = round(float(last["equity"]), 2)
    out["cash_plus_positions"] = round(implied, 2)
    out["diff"] = round(float(last["equity"]) - implied, 6)
    f = trades[trades["status"] == "filled"]
    cash = initial
    for _, t in f.iterrows():
        if t["side"] == "buy":
            cash -= float(t["amount"]) + float(t["fees"])
        elif t["side"] == "sell":
            cash += float(t["amount"]) - float(t["fees"])
    out["cash_from_trades"] = round(cash, 2)
    out["cash_nav"] = round(float(last["cash"]), 2)
    out["cash_diff"] = round(cash - float(last["cash"]), 2)
    out["passed"] = abs(out["diff"]) < 1e-6 and abs(out["cash_diff"]) < 0.01
    return out


# ---------------------------------------------------------------- 输出
def write_summary(path: Path, ctx: dict, results: dict, sel_stats: dict, recon: dict, nolook: dict,
                  cash_chk: dict, bench: dict, notes: list) -> None:
    L = []
    L.append("# Agent 选股策略回测结果（确定性代理口径）")
    L.append("")
    L.append(f"- 区间：**{ctx['dates'][0]} ~ {ctx['dates'][-1]}**（{len(ctx['dates'])} 个交易日，"
             f"{len(ctx['dates']) / TRADING_DAYS:.2f} 年）")
    L.append(f"- 初始资金：{ctx['initial_cash']:,.0f} 元；T 收盘决策 → T+1 收盘成交（含滑点/佣金/印花税/过户费）")
    L.append(f"- 候选池：逐日重算 `sim/pool.py` 特征 → `channels()` → `assemble()` → `triage()`，"
             f"可行动名单 = `action=='ready'` 按 `priority` 降序")
    L.append(f"- regime：`sim/settle.py::regime` 同款（沪深 300 均线）；defensive 通道主口径 = lowvol 替代"
             f"{'（本次为 --defensive-pe 敏感性口径）' if ctx['defensive_pe'] else ''}")
    L.append(f"- 股票池：{len(ctx['meta'])} 只（2026-10-23 仍可交易者，存在幸存者偏差）")
    L.append("")
    L.append("## 一、三口径核心结果")
    L.append("")
    L.append("| 口径 | 累计收益 | CAGR | 最大回撤 | 回撤最长持续 | 年化波动 | Sharpe | Calmar | 期末净值 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    names = {"P1": "P1 实盘口径（5%×6 + 底仓 ETF）", "P2": "P2 风控上限口径（stock_max/6，无 ETF）",
             "P3": "P3 纯选股口径（6 只等权 100%）"}
    for k in ctx["calibers"]:
        m = results[k]["metrics"]
        L.append(f"| {names.get(k, k)} | {m['total_return']:+.2%} | {m['cagr']:+.2%} | {m['max_drawdown']:.2%} | "
                 f"{m['max_dd_duration_days']} 日 | {m['annual_vol']:.2%} | {m['sharpe']:.2f} | "
                 f"{m['calmar']:.2f} | {m['end_equity']:,.0f} |")
    L.append("")
    L.append("## 二、交易与持仓统计")
    L.append("")
    L.append("| 口径 | 成交笔数 | 买入 | 卖出 | 未成交(no_fill) | 已平仓胜率 | 平均持有(交易日) | 年换手 | 费用合计 | 熔断/暂停次数 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for k in ctx["calibers"]:
        m = results[k]["metrics"]
        L.append(f"| {k} | {m['n_trades_filled']} | {m['n_buys']} | {m['n_sells']} | {m['n_no_fill']} | "
                 f"{m['win_rate']:.1%} | {m['avg_holding_days']:.1f} | {m['turnover_annual']:.2f} | "
                 f"{m['total_fees']:,.0f} | {m['n_breaker_events']}/{m['n_halt_events']} |")
    L.append("")
    L.append("## 三、子区间分解")
    L.append("")
    for k in ctx["calibers"]:
        L.append(f"### {names.get(k, k)}")
        L.append("")
        L.append("| 区间 | 收益 | CAGR | 最大回撤 | Sharpe | 平均股票权重 | 平均 ETF 权重 |")
        L.append("|---|---|---|---|---|---|---|")
        for sp, sm in results[k]["metrics"]["subperiods"].items():
            L.append(f"| {sp} | {sm['total_return']:+.2%} | {sm['cagr']:+.2%} | {sm['max_drawdown']:.2%} | "
                     f"{sm['sharpe']:.2f} | {sm.get('avg_stock_weight', NAN):.1%} | {sm.get('avg_etf_weight', NAN):.1%} |")
        L.append("")
    L.append("## 四、regime 分解")
    L.append("")
    L.append("| 口径 | regime | 天数 | 区间累计收益 | 年化波动 | 平均股票权重 | 平均回撤 |")
    L.append("|---|---|---|---|---|---|---|")
    reg_cn = {"offense": "进攻", "neutral": "中性", "defense": "防守"}
    for k in ctx["calibers"]:
        for reg, rm in results[k]["metrics"]["by_regime"].items():
            L.append(f"| {k} | {reg_cn.get(reg, reg)} | {rm['days']} | {rm['cum_return']:+.2%} | "
                     f"{rm['annual_vol']:.2%} | {rm['avg_stock_weight']:.1%} | {rm['avg_drawdown']:.2%} |")
    L.append("")
    L.append("## 五、基准与选股 alpha")
    L.append("")
    L.append("| 基准 | 累计收益 | CAGR | 最大回撤 | Sharpe |")
    L.append("|---|---|---|---|---|")
    for name, bm in bench.items():
        L.append(f"| {name} | {bm['total_return']:+.2%} | {bm['cagr']:+.2%} | {bm['max_drawdown']:.2%} | {bm['sharpe']:.2f} |")
    L.append("")
    L.append("可行动名单（ready）前向收益（横截面均值，超额 = 相对沪深 300）：")
    L.append("")
    L.append("| 指标 | 样本数 | 均值 | 中位数 | 胜率 | 平均超额 |")
    L.append("|---|---|---|---|---|---|")
    for n in (5, 20, 60):
        s = sel_stats.get(f"fwd{n}")
        if s:
            L.append(f"| ready fwd{n} | {s['n']} | {s['mean']:+.2%} | {s['median']:+.2%} | {s['hit_rate']:.1%} | {s['mean_excess']:+.2%} |")
    for tag, label in (("top1", "priority 第 1 名"), ("top3", "priority 前 3 名")):
        for n in (5, 20):
            s = sel_stats.get(f"{tag}_fwd{n}")
            if s:
                L.append(f"| {label} fwd{n} | {s['n']} | {s['mean']:+.2%} | - | {s['hit_rate']:.1%} | - |")
    sd = sel_stats.get("style_dist") or {}
    if sd:
        L.append("ready 名单风格分布：" + "，".join(f"{k} {v}" for k, v in sd.items()))
        L.append("")
    L.append("## 六、期末持仓")
    L.append("")
    for k in ctx["calibers"]:
        pos = results[k].get("positions") or []
        L.append(f"### {names.get(k, k)}（{len(pos)} 只）")
        L.append("")
        if not pos:
            L.append("空仓。")
            L.append("")
            continue
        L.append("| 代码 | 名称 | 行业 | 股数 | 市值 | 权重 | 成本 | 浮动盈亏 | 建仓日 | 持有交易日 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for q in pos:
            L.append(f"| {q['code']} | {q['name']} | {q['industry']} | {q['shares']:.0f} | {q['value']:,.0f} | "
                     f"{q['weight']:.1%} | {q['cost_per_share']} | {q['unrealized']:+.1%} | {q['first_date']} | "
                     f"{q['hold_days']} |")
        L.append("")
    L.append("## 七、自检")
    L.append("")
    L.append(f"- 特征对账（{recon.get('date', '-')}，重叠 {recon.get('matched_codes', 0)} 只）："
             f"**{'通过' if recon.get('passed') else '未通过'}**（阈值：单字段相对差>0.5% 的股票数 ≤20）")
    for f, v in recon.get("fields", {}).items():
        if not v.get("n"):
            continue
        if "n_mismatch" in v:
            L.append(f"  - `{f}`（布尔字段）：{v['n_mismatch']} 只不一致")
        else:
            L.append(f"  - `{f}`：max 相对差 {v.get('max_rel_diff')}，>1e-6 的 {v.get('n_rel_gt_1e-6')} 只，"
                     f">0.5% 的 {v.get('n_rel_gt_0.5pct', 0)} 只")
    L.append(f"- 无未来函数抽样（{nolook.get('n_pairs_checked', 0)} 个 (T,代码) 组合，用 ≤T 数据调 "
             f"`sim.pool.features()` 重算）：max 相对差 {nolook.get('max_rel_diff')}，"
             f"**{'通过' if nolook.get('passed') else '未通过'}**（阈值 1e-6）")
    L.append(f"- P1 净值恒等式：期末 equity {cash_chk['nav_equity']:,.2f} = 现金+持仓 "
             f"{cash_chk['cash_plus_positions']:,.2f}（差 {cash_chk['diff']}）；"
             f"成交流水递推现金 {cash_chk['cash_from_trades']:,.2f} vs 净值现金 {cash_chk['cash_nav']:,.2f}"
             f"（差 {cash_chk['cash_diff']}）→ **{'通过' if cash_chk['passed'] else '未通过'}**")
    L.append("")
    L.append("## 八、口径说明与偏离")
    L.append("")
    for n in notes:
        L.append(f"- {n}")
    L.append("")
    L.append("## 九、输出文件")
    L.append("")
    for fn in sorted(p.name for p in Path(ctx["out"]).glob("*")):
        L.append(f"- `{fn}`")
    L.append("")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Agent 选股策略两年回测引擎")
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "cache" / "backtest"))
    ap.add_argument("--out", default=str(ROOT / "data" / "backtest_agent"))
    ap.add_argument("--start", default="2024-10-24")
    ap.add_argument("--end", default=None)
    ap.add_argument("--calibers", default="P1,P2,P3")
    ap.add_argument("--defensive-pe", action="store_true", help="defensive 通道用 PE 近似（敏感性口径）")
    ap.add_argument("--slippage-mult", type=float, default=1.0, help="滑点倍数（0=无滑点，2=两倍）")
    ap.add_argument("--stop-loss", type=float, default=None, help="止损比例（默认读 risk.yaml）")
    ap.add_argument("--max-stocks", type=int, default=None, help="最多持股数（默认读 risk.yaml）")
    ap.add_argument("--ignore-warn", action="store_true", help="不启用 drawdown.warn 的 stock_max 降档")
    ap.add_argument("--p1-single", type=float, default=0.05,
                    help="P1 单只个股目标权重（默认 5%%；v1.5 置信度分档可近似为 10%%）")
    ap.add_argument("--recon-csv", default=str(ROOT / "data" / "cache" / "pool" / "2026-10-23_features.csv"))
    ap.add_argument("--no-selfcheck", action="store_true")
    args = ap.parse_args(argv)

    t0 = time.time()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data_dir = Path(args.data_dir)

    def log(msg):
        print(f"[{time.time() - t0:6.1f}s] {msg}", flush=True)

    cfg = load_configs()
    rcfg, bcfg = cfg["risk"], cfg["broker"]
    if args.stop_loss is not None:
        rcfg["position"]["stop_loss_pct"] = args.stop_loss
    if args.max_stocks is not None:
        rcfg["position"]["max_stocks"] = args.max_stocks
    pcfg = cfg["strategy"]["pool"]
    initial_cash = float(bcfg["initial_cash"])
    log(f"配置载入：初始资金 {initial_cash:,.0f}，per_channel={pcfg['per_channel']}，"
        f"max_stocks={rcfg['position']['max_stocks']}，止损={rcfg['position']['stop_loss_pct']:.0%}")

    d = load_data(data_dir)
    panel, meta = d["panel"], d["meta"]
    log(f"数据载入：panel {len(panel):,} 行，{panel['code'].nunique()} 只，"
        f"{panel['date'].min()} ~ {panel['date'].max()}；meta {len(meta)} 只")

    panel_dates = sorted(panel["date"].unique())
    end = args.end or panel_dates[-1]
    dates = [x for x in panel_dates if args.start <= x <= end]
    if len(dates) < 10:
        print(f"交易日太少（{len(dates)}），检查 --start/--end")
        return 2
    signal_all = dates
    log(f"回测区间：{dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日）")

    regime_all = regime_series(d["index"])
    regime = regime_all.reindex(panel_dates).ffill()
    regime = regime.where(regime.notna(), "defense")
    log(f"regime 分布：" + "，".join(f"{k} {int((regime.reindex(dates) == k).sum())}"
                                    for k in ("offense", "neutral", "defense")))

    t = time.time()
    feats = build_features(panel)
    feats_sig = feats[(feats["date"] >= args.start) & (feats["date"] <= end)]
    log(f"特征计算完成：{len(feats):,} 行（信号期 {len(feats_sig):,} 行），{time.time() - t:.1f}s")

    t = time.time()
    w = Wide(panel, feats_sig, dates, d["etf"], d["index"].set_index("date")["close"].astype(float))
    log(f"宽表构建完成：{time.time() - t:.1f}s")

    t = time.time()
    builder = PoolBuilder(feats_sig, meta, w, pcfg, rcfg, regime, args.defensive_pe,
                          cfg["strategy"]["pool"]["defensive_max_pe"])
    pools = {}
    elig_by_date = {}
    action_counts = []
    for i, day in enumerate(dates):
        pool = builder.daily_pool(day)
        pools[day] = pool
        if builder.bounds.get(day):
            s, e = builder.bounds[day]
            f = builder.feats.iloc[s:e]
            # eligible 名单（等权基准用）
            codes = f["code"]
            ok = codes.map(builder.code_ok).fillna(False).astype(bool)
            elig = codes[(f["valid"]) & ok & (f["avg_amount"] >= rcfg["universe"]["min_avg_amount"])
                         & (f["chg_1d"] <= rcfg["universe"]["max_day_change"])
                         & (f["chg_5d"] <= rcfg["universe"]["max_5d_change"])
                         & (f["close"] > 0)]
            elig_by_date[day] = elig.tolist()
        if len(pool):
            vc = pool["action"].value_counts()
            action_counts.append({"date": day, "pool": int(len(pool)),
                                  "ready": int(vc.get("ready", 0)),
                                  "wait_pullback": int(vc.get("wait_pullback", 0)),
                                  "avoid": int(vc.get("avoid", 0))})
        if (i + 1) % 100 == 0:
            log(f"候选池 {i + 1}/{len(dates)} 天，最近 ready={action_counts[-1]['ready'] if action_counts else 0}")
    log(f"候选池重建完成：{time.time() - t:.1f}s，ready 日均 "
        f"{np.mean([a['ready'] for a in action_counts]) if action_counts else 0:.1f} 只")

    ctx = {"panel": panel, "meta": meta, "feats": feats, "dates": dates, "w": w, "regime": regime,
           "rcfg": rcfg, "broker": bcfg, "pcfg": pcfg, "pools": pools, "eligible_by_date": elig_by_date,
           "initial_cash": initial_cash, "out": out, "calibers": [c.strip() for c in args.calibers.split(",")],
           "etf_code": bcfg["base_etf"]["code"], "etf_name": bcfg["base_etf"]["name"],
           "defensive_pe": args.defensive_pe, "p1_single": args.p1_single,
           "stop_loss": rcfg["position"]["stop_loss_pct"],
           "slippage_mult": args.slippage_mult, "warn_downgrade": not args.ignore_warn}

    results = {}
    for cal in ctx["calibers"]:
        t = time.time()
        sim = Simulator(ctx, cal)
        res = sim.run()
        results[cal] = res
        log(f"{cal} 模拟完成：{time.time() - t:.1f}s，成交 {len(res['trades'])} 条，期末 "
            f"{res['nav']['equity'].iloc[-1]:,.0f}")

    ctx["nav_dates"] = [x for x in dates]
    bench = benchmark_metrics(ctx)
    sel_df, sel_stats = selection_forward(ctx)
    recon = recon_check(ctx, Path(args.recon_csv)) if not args.no_selfcheck else {"passed": None}
    nolook = no_lookahead_check(ctx) if not args.no_selfcheck else {"passed": None}
    cash_chk = cash_consistency(results["P1"]["nav"], results["P1"]["trades"], initial_cash) \
        if "P1" in results else {"passed": None, "nav_equity": NAN, "cash_plus_positions": NAN,
                                 "diff": NAN, "cash_from_trades": NAN, "cash_nav": NAN, "cash_diff": NAN}

    notes = [
        "**回测对象是策略的确定性内核代理**：候选池（六通道 + 分诊）→ ready 名单 → 机械执行 + risk.yaml 风控；"
        "不包含 LLM 深度研究判断、逻辑证伪卖出、事件驱动择时、加仓/减仓。核心代理假设：LLM 不会系统性偏离 ready 集合，"
        "它主要做的是从名单里再挑 3-5 只 + 决定买点，这里用 priority 排序/等权代替。",
        "**幸存者偏差**：股票池 = 2026-10-23 仍可交易且流动性达标的 2,473 只，不含已退市/长期停牌/当日流动性落榜者，"
        "收益偏乐观（历史 ST 不可得，用当前名称近似）。",
        "**前复权口径差异**：panel 于 2026-10-27 之后构建，个别个股在 2026-10-24 后除权会使历史前复权价整体下移，"
        "与 2026-10-23 当日候选池快照存在小幅绝对价差（不影响收益率）。",
        "**最短持有 3 交易日**：机械口径下没有非强制卖出（无主动止盈/换股），该条约束实际未触发。",
        "**熔断/暂停**：规格只要求暂停新开个股、不强制清仓（实盘 settle.py 会把持仓加入强制卖出清单，"
        "本回测按规格执行，仅停新开）。暂停后的恢复：规格未定义人工 resume，回测采用机械口径 = 净值创新高自动恢复。",
        "**drawdown.warn=8% 的 stock_max 降档**：规格未明列，但 risk.yaml 有此规则（回撤预警个股上限降一档），"
        "本回测启用（--ignore-warn 可关闭）。",
        "**max_risk_per_trade（单笔风险 ≤ 总资产 1%）**：risk.py 会据此把个股仓位上限压到 8%/1% = 12.5%；"
        "本回测按规格只约束 max_single=20% 与 band stock_max，未加该约束（仅影响 P3 的 1/6=16.7% 目标）。",
        "**P1 买入一手金额约束**：目标仓位 5%（5,000 元）买不起一手的股票（价格 >50 元）在决策阶段跳过，"
        "与实盘风控「一手金额超过目标仓位」拒绝一致，计入 skipped_lot。",
        "**regime 降档减仓**：按 ret_20 最差顺序整只卖出至不超 stock_max（含 0.5pp 容忍带，避免每日微漂移换手）。",
        "**执行价**：T+1 前复权收盘价（近似实盘 14:45 成交），滑点按 T 日 avg_amount20 分档 + 成交量上限 1%；"
        "停牌/一字涨停/跌停记 no_fill 并在次日顺延。",
        "**P1 底仓 ETF**：sh510300 补到 band_max − 个股权重，偏离 >5pp 或当日有股票买入才调仓；ETF 不计入个股数。",
        "**ETF 底仓交易成本**：佣金 0.025%（最低 5 元）+ 滑点 0.05%，免印花税/过户费。",
        "**胜率/持有天数**：仅统计已平仓个股（ETF 底仓单独在 trades 中标记 kind=etf）。",
        "**行业标签覆盖不足**：meta 的 industry 仅覆盖 1,442/2,473 只（58%，新浪行业板块缺失的票为 NaN），"
        "PE 快照 1,442 只、eps 1,117 只。缺失行业的个股记「未分类」→ 「单行业 ≤35%」约束与 sector 通道的覆盖不完整"
        "（约束偏松：同行业个股可能因标签缺失而未被计入），方向上使分散度指标偏乐观、sector 通道候选偏少。",
        "**ready 名单风格偏低波/回调**：分诊规则要求「已到/接近触发位（距离 ≤2%）」才算 ready，"
        "momentum/trend/breakout 命中时多数已远离 MA20/MA60 或突破位，被判为 wait_pullback / watch。"
        "这是分诊规则的直接结果，机械代理因此在进攻段也偏防守（报告需说明这是「选股代理」而非实盘 LLM 行为）。",
        "**机械口径无主动止盈**：卖出只有止损/时间止损/降档减仓三种，赚钱的持仓会一直持有到期末，"
        "已平仓胜率天然偏低（时间止损只在浮亏时触发）；LLM 实盘会做逻辑证伪卖出，实际换手率更高。",
    ]
    if args.defensive_pe:
        notes.append("本次为 **--defensive-pe 敏感性口径**：defensive 通道用 PE 近似（eps 由 2026-10-23 快照反推），"
                     "含盈利预期前视，只作敏感性检查，不是主口径。")
    if args.slippage_mult != 1.0:
        notes.append(f"本次滑点倍数 = {args.slippage_mult}。")
    if rcfg["position"]["max_stocks"] != 6:
        notes.append(f"本次 max_stocks = {rcfg['position']['max_stocks']}。")

    for k in results:
        results[k]["metrics"] = compute_metrics(results[k]["nav"], results[k]["trades"], k, results[k])

    # ---- 落盘
    for cal, res in results.items():
        nav = res["nav"]
        nav.to_csv(out / f"nav_{cal}.csv", index=False)
        tr = res["trades"]
        cols = ["date", "code", "name", "side", "shares", "price", "amount", "fees", "reason",
                "holding_days", "pnl", "status", "kind", "commission", "stamp_tax", "transfer_fee",
                "slippage_cost", "weight_target", "decided_on", "priority", "regime", "note", "seq"]
        for c in cols:
            if c not in tr.columns:
                tr[c] = NAN
        tr[cols].to_csv(out / f"trades_{cal}.csv", index=False)
    sel_df.to_csv(out / "selection_forward.csv", index=False)
    pd.DataFrame(action_counts).to_csv(out / "pool_daily.csv", index=False)

    metrics = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "period": {"start": dates[0], "end": dates[-1], "trading_days": len(dates),
                          "years": round(len(dates) / TRADING_DAYS, 3)},
               "params": {"initial_cash": initial_cash, "defensive_channel": "pe" if args.defensive_pe else "lowvol",
                          "slippage_mult": args.slippage_mult,
                          "stop_loss": rcfg["position"]["stop_loss_pct"],
                          "max_stocks": rcfg["position"]["max_stocks"],
                          "max_new_per_week": rcfg["position"]["max_new_per_week"],
                          "max_single": rcfg["position"]["max_single"],
                          "max_industry": rcfg["position"]["max_industry"],
                          "min_hold_days": rcfg["position"]["min_hold_days"],
                          "warn_downgrade": not args.ignore_warn,
                          "pool_cfg": pcfg},
               "calibers": {k: {"metrics": results[k]["metrics"], "events": results[k]["events"],
                                "final_positions": results[k]["positions"],
                                "nav_file": f"nav_{k}.csv", "trades_file": f"trades_{k}.csv"} for k in results},
               "benchmarks": bench, "selection": sel_stats,
               "selfcheck": {"recon": recon, "no_lookahead": nolook, "cash_consistency": cash_chk},
               "notes": notes}
    (out / "metrics.json").write_text(json.dumps(_jsonable(metrics), ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    write_summary(out / "summary.md", ctx, results, sel_stats, recon, nolook, cash_chk, bench, notes)
    log(f"输出已写入 {out}")
    # 控制台摘要
    for k in results:
        m = results[k]["metrics"]
        print(f"  {k}: 累计 {m['total_return']:+.2%}  CAGR {m['cagr']:+.2%}  MaxDD {m['max_drawdown']:.2%}  "
              f"Calmar {m['calmar']:.2f}  成交 {m['n_trades_filled']}  期末 {m['end_equity']:,.0f}")
    print(f"  自检：特征对账 {recon.get('passed')}，无未来函数 {nolook.get('passed')}，"
          f"净值恒等 {cash_chk.get('passed')}")
    log("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
