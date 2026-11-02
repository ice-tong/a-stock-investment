"""S-003 挑战者：小市值 + 学术因子合成的前向纸面验证（文件名沿用 S-002，引擎是第四轮 F4）。

  - s003：第五轮 R5（观察级，未过存档门槛 t ≥ 2.5）：F4 的池子与合成分，剔除 X1 退市风险、X2 解禁/减持，
          X3 增持/回购 Z+0.10，取前 10 只；已持有且仍在前 30 名的继续持有（缓冲）。
          预注册 docs/research/选股策略_预注册_第五轮_2026-11-01.md，报告 reports/20261101_选股策略_第五轮.md。
  - S-002（F4，50 只）2026-11-02 按用户要求删除：10 万分 50 只每只约 2000 元，买不起一手的多、与宽基持仓区别不明显。
    账本与信号只在 git 历史里（提交 7650d58 及之前）。

F4 基础规则（预注册见 docs/research/选股策略_预注册_第四轮_2026-11-01.md，报告 reports/20261101_选股策略_第四轮.md，不可事后改；
R5 在此基础上改为 10 只 + 排雷 + 缓冲）：
  - 信号：每月最后一个交易日收盘后，在可投资格（上市满 250 日、20 日日均成交额 ≥5000 万、成交均价 ≥3 元、非 ST）内，
          取流通市值最小 20%，按合成分 Z（SUE / EP / 低异常换手 / 低应计的截面百分位等权）取最高 50 只。
  - 执行：下一个交易日开盘，等权（每只 = 开盘时净值 / 50）；开盘接近涨停的新票不买，资金留现金；
          开盘接近跌停或停牌卖不出的，顺延到之后的交易日开盘再卖。
  - 成本：config/broker.yaml 费率 + 单边 0.10% 滑点（与回测 0.35%/来回一致）。
  - 前向偏离（实施约定，写于首次运行前）：已持有且仍入选的票，只有偏离目标权重 >25% 才调回等权，避免碎单的 5 元最低佣金。

隔离：账户 accounts/challenger/s003/（主流程 all_accounts 只扫 accounts/* 第一层）；数据 data/s003/（日线与财报缓存 data/s003/cache/，gitignored）；不写 orders/、config/。

评估（报告里给出，不自动停）：满 12 次调仓后，年化 ≥ 11%（F4 回测切分中位数 22.2% 的一半）且累计跑赢 K3 纯小市值 → 可提交主策略提案；否则停止。

用法：
  .venv/bin/python tools/s002.py init --date 2026-11-02
  .venv/bin/python tools/s002.py signal [--date 2026-10-30] [--force]   # 每个交易日 21:15 跑；非月末自动跳过
  .venv/bin/python tools/s002.py settle [--date 2026-11-02]             # 每个交易日 20:50 跑：除权、换仓、净值
  .venv/bin/python tools/s002.py report | status
  （--profile s003 可写可不写，只剩这一个 profile）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sim import broker, calendar as cal, config
from sim.ledger import Account
from sim.market import limit_prices
from tools import select_bt as bt
from tools import select_fin as sf

PROFILES = {
    "s003": {"account": "challenger/s003", "strategy": "s003-r5-v1", "rule": "R5", "n": 10,
             "desc": "在流通市值最小 20% 的池子里按合成分（SUE / EP / 低异常换手 / 低应计）排序，剔除退市风险、解禁/减持，增持/回购加分，取前 10 只（前 30 名缓冲），次日开盘等权换仓。"
                     "观察级：回测切分中位数 22.0%、10 万口径 23.7%，月度超额 t = 2.19 未过 2.5。"},
}
PROFILE = "s003"
ACCOUNT = "challenger/s003"
STRATEGY = "s003-r5-v1"
RULE = "R5"
INIT_CASH = 100_000.0  # 用户 2026-11-01 指定 10 万：每只目标约 1 万元
N = 10
SLIP = 0.001
REBAL_TOL = 0.25
WINDOW_DAYS = 450  # 日历日，约 300 个交易日，够算 250 日换手与上市满 250 日
EVAL_REBALS, EVAL_CAGR = 12, 0.11
CORP_RATIO_RANGE = (0.5, 3.0)
BASE = Path(__file__).resolve().parents[1] / "data" / "cache"


def use(profile: str) -> None:
    global PROFILE, ACCOUNT, STRATEGY, RULE, N
    pr = PROFILES[profile]
    PROFILE, ACCOUNT, STRATEGY, RULE, N = profile, pr["account"], pr["strategy"], pr["rule"], pr["n"]


def log(msg: str) -> None:
    print(f"[{PROFILE}] {msg}", flush=True)


def data_dir() -> Path:
    return config.home() / "data" / PROFILE


def cache_dir() -> Path:
    return config.home() / "data" / "s003" / "cache"


def orders_dir() -> Path:
    return data_dir() / "orders"


def acct() -> Account:
    return Account(ACCOUNT)


def is_month_end(d: str) -> bool:
    return cal.is_trading_day(d) and cal.next_day(d)[:7] != d[:7]


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- 数据刷新
def _fetch_daily(args):
    code, start, end, out = args
    p = Path(out) / f"{code}.csv"
    if p.exists():
        try:
            last = pd.read_csv(p, usecols=["date"]).date.max()
            if last >= f"{end[:4]}-{end[4:6]}-{end[6:]}":
                return code, "cached"
        except Exception:
            pass
    import akshare as ak
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_daily(symbol=code, start_date=start, end_date=end, adjust="qfq")
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


def refresh(date: str, workers: int = 8) -> pd.DataFrame:
    """拉取当前在市沪深 A 股名单（含名称，用于 ST 剔除）、近 450 日前复权日线、最近几期业绩报表与披露日。"""
    import akshare as ak
    upath0 = cache_dir() / f"universe_{date}.csv"
    if upath0.exists():  # 同一信号日已取过名单（另一个账户先跑），直接复用，保证两个账户口径一致
        uni = pd.read_csv(upath0)
        log(f"复用 {upath0.name}")
        return _refresh_rest(date, uni, workers)
    cache_dir().mkdir(parents=True, exist_ok=True)
    upath = cache_dir() / f"universe_{date}.csv"
    try:
        spot = None
        for k in range(3):
            try:
                spot = ak.stock_zh_a_spot()
                break
            except Exception as e:
                log(f"全市场名单第 {k + 1} 次失败：{type(e).__name__}")
                time.sleep(10 * (k + 1))
        if spot is None:
            raise RuntimeError("全市场名单取数失败")
        uni = spot[spot["代码"].str[:2].isin(["sh", "sz"])][["代码", "名称"]].rename(columns={"代码": "code", "名称": "name"})
        uni.to_csv(upath, index=False)
    except RuntimeError:
        old = sorted(cache_dir().glob("universe_*.csv"))
        if upath.exists() or not old:
            raise
        uni = pd.read_csv(old[-1])  # 名单取不到时用最近一次（名称只用于 ST 剔除）
        log(f"改用 {old[-1].name} 的名单")
    return _refresh_rest(date, uni, workers)


def _refresh_rest(date: str, uni: pd.DataFrame, workers: int) -> pd.DataFrame:
    daily = cache_dir() / "daily"
    daily.mkdir(exist_ok=True)
    start = (dt.date.fromisoformat(date) - dt.timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    end = date.replace("-", "")
    stats = {"ok": 0, "cached": 0, "failed": 0}
    t0 = time.time()
    with ProcessPoolExecutor(workers) as ex:
        for i, (_, s) in enumerate(ex.map(_fetch_daily, [(c, start, end, str(daily)) for c in uni.code], chunksize=8), 1):
            stats[s] += 1
            if i % 1000 == 0:
                log(f"日线 {i}/{len(uni)} {stats} {time.time() - t0:.0f}s")
    log(f"日线完成 {stats}")
    if stats["failed"] > len(uni) * 0.05:
        raise RuntimeError(f"日线失败 {stats['failed']} 只，超过 5%，不出信号")
    _refresh_fin(date)
    return uni


def _recent_periods(date: str) -> list[str]:
    y = int(date[:4])
    ps = [f"{yy}{q}" for yy in (y - 1, y) for q in ("0331", "0630", "0930", "1231")]
    return [p for p in ps if f"{p[:4]}-{p[4:6]}-{p[6:]}" < date]


def _refresh_fin(date: str) -> None:
    """业绩报表（eps 等）与实际披露日：以研究缓存为底，刷新最近两年的报告期。"""
    import akshare as ak
    ypath, spath = cache_dir() / "yjbb.pkl", cache_dir() / "sched.pkl"
    y = pd.read_pickle(ypath) if ypath.exists() else pd.read_pickle(BASE / "full" / "yjbb.pkl")
    if "rev" not in y.columns:  # 第四轮时的缓存没有营收：换成已含营收的研究缓存为底（R5 的 X1 需要）
        y = pd.read_pickle(BASE / "full" / "yjbb.pkl")
    s = pd.read_pickle(spath) if spath.exists() else pd.read_pickle(BASE / "earnings_sched_2019_2026.pkl")
    qname = {v: k for k, v in sf.QLABEL.items()}
    for p in _recent_periods(date):
        for a in range(3):
            try:
                d = ak.stock_yjbb_em(date=p)
                d = d[["股票代码", "每股收益", "净利润-净利润", "每股经营现金流量", "每股净资产", "营业总收入-营业总收入"]].copy()
                d.columns = ["code6", "eps", "np", "cfps", "bps", "rev"]
                d["period"] = p
                y = pd.concat([y[y.period != p], d], ignore_index=True)
                label = p[:4] + qname[p[4:]]
                r = ak.stock_report_disclosure(market="沪深京", period=label)
                r = r[["股票代码", "股票简称", "首次预约", "初次变更", "二次变更", "三次变更", "实际披露"]].copy()
                for c in ("首次预约", "初次变更", "二次变更", "三次变更", "实际披露"):
                    r[c] = pd.to_datetime(r[c], errors="coerce")
                r["period"] = label
                s = pd.concat([s[s.period != label], r], ignore_index=True)
                log(f"财报 {p}: 业绩 {len(d)}，披露日 {len(r)}")
                break
            except Exception as e:
                log(f"财报 {p} 第 {a + 1} 次失败：{type(e).__name__}")
                time.sleep(3 * (a + 1))
    y.to_pickle(ypath)
    s.to_pickle(spath)
    if RULE in bt.RRULES:
        _refresh_events(date)


def _refresh_events(date: str) -> None:
    """R5 的排雷数据（第五轮）：以研究缓存为底，刷新近几年分红、未来 3 个月解禁、全部增减持与回购；写成 select_screens 读取的文件名。"""
    import akshare as ak
    from tools import select_events as se
    c = cache_dir()
    for name in ("fhps", "unlock", "ggcg", "repurchase"):
        if not (c / f"{name}.pkl").exists():
            pd.read_pickle(BASE / "full" / f"{name}.pkl").to_pickle(c / f"{name}.pkl")
    fh = pd.read_pickle(c / "fhps.pkl")
    y = int(date[:4])
    for yy in range(y - 3, y):
        for q in ("0630", "1231"):
            try:
                d = se._retry(lambda: ak.stock_fhps_em(date=f"{yy}{q}"))
                d = d[["代码", "现金分红-现金分红比例", "总股本", "预案公告日"]].copy()
                d.columns = ["code6", "cash_per10", "shares", "announce"]
                d["period"] = f"{yy}{q}"
                fh = pd.concat([fh[fh.period != f"{yy}{q}"], d], ignore_index=True)
            except Exception as e:
                log(f"分红 {yy}{q} 失败：{type(e).__name__}")
    fh.to_pickle(c / "fhps.pkl")
    un = pd.read_pickle(c / "unlock.pkl")
    start, end = dt.date.fromisoformat(date), dt.date.fromisoformat(date) + dt.timedelta(days=90)
    d = se._retry(lambda: ak.stock_restricted_release_detail_em(start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")))
    d = d[["股票代码", "解禁时间", "解禁数量"]].rename(columns={"股票代码": "code6", "解禁时间": "date", "解禁数量": "qty"})
    un["date_s"] = pd.to_datetime(un.date).dt.strftime("%Y-%m-%d")
    un = un[(un.date_s < start.isoformat()) | (un.date_s > end.isoformat())].drop(columns="date_s")
    pd.concat([un, d], ignore_index=True).to_pickle(c / "unlock.pkl")
    g = se._retry(lambda: ak.stock_ggcg_em(symbol="全部"))
    g = g[["代码", "持股变动信息-增减", "持股变动信息-占流通股比例", "公告日"]].copy()
    g.columns = ["code6", "side", "pct_float", "announce"]
    g.to_pickle(c / "ggcg.pkl")
    r = se._retry(lambda: ak.stock_repurchase_em())
    r = r[["股票代码", "计划回购金额区间-下限", "回购起始时间"]].copy()
    r.columns = ["code6", "amt_low", "start"]
    r.to_pickle(c / "repurchase.pkl")
    log(f"排雷数据：分红 {len(fh)}、解禁（未来 90 天）{len(d)}、增减持 {len(g)}、回购 {len(r)}")


def load_wide(date: str, universe: pd.DataFrame) -> dict:
    """前向数据 → select_bt.features 需要的宽表（只取 ≤ date 的行）。"""
    st = universe["name"].str.contains("ST|退", na=False)
    codes = set(universe.loc[~st, "code"])
    frames = []
    for p in (cache_dir() / "daily").glob("*.csv"):
        if p.stem in codes:
            df = pd.read_csv(p)
            df["code"] = p.stem
            frames.append(df[df.date <= date])
    panel = pd.concat(frames, ignore_index=True)
    return {c: panel.pivot(index="date", columns="code", values=c).sort_index()
            for c in ("open", "close", "amount", "volume", "turnover")}


def compute_signal(date: str, w: dict, yjbb: pd.DataFrame, sched: pd.DataFrame, held=(), screen_dir: Path | None = None) -> dict:
    """在 date 收盘后按 RULE 选股；同时给出 K3（最小 20% 全体）与等权全体名单，用于前向对照。
    R 规则的排雷数据从 screen_dir 读取（需含 fin_pit/yjbb/fhps/unlock/ggcg/repurchase.pkl）。"""
    fin = sf.fin_pit_table(yjbb)
    if RULE in bt.RRULES:
        from tools import select_screens as scr
        sd = screen_dir or cache_dir()
        fin.to_pickle(sd / "fin_pit.pkl")
        scr.CACHE = sd
        scr.tables.cache_clear()
    sue = sf.sue_table(yjbb, sched)
    elig, f = bt.features(w, fin_pit=fin, sue_pit=sue)
    if date not in elig.index:
        raise RuntimeError(f"{date} 不在日线数据里（数据未更新？）")
    ok = elig.loc[date]
    cand = ok[ok].index
    m = f["mcap"].loc[date].reindex(cand).dropna()
    picks = bt.fpick(RULE, f, date, cand, N, held=held)
    z = bt.composite(*(f[k].loc[date].reindex(cand) for k in ("sue", "ep", "at", "acc")))
    return {"picks": picks, "z": {c: round(float(z[c]), 4) for c in picks},
            "mcap": {c: round(float(m[c]) / 1e8, 2) for c in picks},
            "k3": list(m[m <= m.quantile(0.2)].index), "ew": list(cand), "n_elig": int(len(cand))}


def signal(date: str, force: bool = False, workers: int = 8) -> dict:
    if not is_month_end(date):
        log(f"{date} 不是月末最后一个交易日，跳过")
        return {"status": "skipped"}
    exec_day = cal.next_day(date)
    path = orders_dir() / f"{exec_day}.json"
    if path.exists() and not force:
        log(f"{exec_day} 订单已冻结，跳过")
        return {"status": "exists"}
    uni = refresh(date, workers)
    w = load_wide(date, uni)
    held = list(acct().positions)
    sig = compute_signal(date, w, pd.read_pickle(cache_dir() / "yjbb.pkl"), pd.read_pickle(cache_dir() / "sched.pkl"), held=held)
    names = dict(zip(uni.code, uni.name))
    doc = {"signal_date": date, "exec_date": exec_day, "decided_at": dt.datetime.now().isoformat(timespec="seconds"),
           "rule": RULE, "held_at_signal": held, "n_elig": sig["n_elig"],
           "picks": [{"code": c, "name": names.get(c, ""), "z": sig["z"][c], "mcap_yi": sig["mcap"][c]} for c in sig["picks"]],
           "k3": sig["k3"], "ew": sig["ew"]}
    _write_json(path, doc)
    log(f"{date} 信号：可投资格 {sig['n_elig']} 只，K3 {len(sig['k3'])} 只，{RULE} 入选 {len(sig['picks'])} 只 → {path}")
    _record_period(date, w)
    return doc


def _bench_ret(codes: list, w: dict, e: str, end: str) -> float:
    """等权组合 open(e) → close(end)，开盘接近涨停的不买（与回测一致），前复权价。"""
    if not codes or e not in w["open"].index:
        return float("nan")
    cs = [c for c in codes if c in w["open"].columns]
    o, pc, c_end = w["open"].loc[e, cs], w["close"].shift(1).loc[e, cs], w["close"].ffill().loc[end, cs]
    lim = bt.limit_pct(pd.Index(cs), e)
    ok = o.notna() & (o < pc * (1 + lim - 0.005))
    r = (c_end[ok] / o[ok] - 1).dropna()
    return float(r.mean()) if len(r) else float("nan")


def _record_period(date: str, w: dict) -> None:
    """上一期订单 → 本信号日收盘：F4 理想收益、K3、等权，以及账户实际净值变化，追加到 periods.csv。"""
    prev = sorted(p for p in orders_dir().glob("*.json") if p.stem <= date)
    if not prev:
        return
    doc = json.loads(prev[-1].read_text(encoding="utf-8"))
    e = doc["exec_date"]
    path = data_dir() / "periods.csv"
    old = pd.read_csv(path) if path.exists() else pd.DataFrame()
    if len(old) and (old.exec_date == e).any():
        return
    row = {"exec_date": e, "end_date": date, "f4_ideal": _bench_ret([p["code"] for p in doc["picks"]], w, e, date),
           "k3": _bench_ret(doc["k3"], w, e, date), "ew": _bench_ret(doc["ew"], w, e, date)}
    try:
        nav = pd.DataFrame(acct().nav())
        nav["equity"] = nav["equity"].astype(float)
        start = nav[nav.date < e].equity.iloc[-1] if (nav.date < e).any() else INIT_CASH
        row["account"] = float(nav[nav.date <= date].equity.iloc[-1] / start - 1)
    except Exception:
        row["account"] = float("nan")
    pd.concat([old, pd.DataFrame([row])], ignore_index=True).to_csv(path, index=False)
    log(f"记录 {e}~{date}: {RULE} 理想 {row['f4_ideal']:+.2%}  K3 {row['k3']:+.2%}  等权 {row['ew']:+.2%}  账户 {row['account']:+.2%}")


# ---------------------------------------------------------------- 结算与换仓
def _sellable(o, prev, code, name) -> bool:
    if o is None or prev is None or np.isnan(o):
        return False
    _, down = limit_prices(code, name, prev)
    return o > down * 1.005


def _buyable(o, prev, code, name) -> bool:
    if o is None or prev is None or np.isnan(o):
        return False
    up, _ = limit_prices(code, name, prev)
    return o < up * 0.995


def _sell(a, code, qty, px, date, bcfg, reason):
    pos = a.positions[code]
    fill = round(px * (1 - SLIP), 3)
    gross = qty * fill
    cm, tax, tf = broker.fees(bcfg, "stock", "sell", gross)
    cost_part = pos["cost"] * qty / pos["shares"]
    pnl = gross - cm - tax - tf - cost_part
    a.state["cash"] = round(a.cash + gross - cm - tax - tf, 2)
    closed = qty >= pos["shares"] - 1e-6
    a.append_trade({"date": date, "order_id": f"{date.replace('-', '')}-s002-{code}", "code": code, "name": pos["name"],
                    "side": "sell", "shares": qty, "price": fill, "amount": round(gross, 2), "commission": cm,
                    "stamp_tax": tax, "transfer_fee": tf, "slippage_cost": round(qty * px * SLIP, 2),
                    "realized_pnl": round(pnl, 2), "closed": closed, "reason_type": reason, "fill_source": "open"})
    if closed:
        del a.positions[code]
    else:
        pos["shares"] -= qty
        pos["cost"] -= cost_part


def _buy(a, code, name, qty, px, date, bcfg, lot, market) -> int:
    fill = round(px * (1 + SLIP), 3)
    while qty > 0 and qty * fill + sum(broker.fees(bcfg, "stock", "buy", qty * fill)) > a.cash:
        qty -= lot
    if qty <= 0:
        return 0
    gross = qty * fill
    cm, tax, tf = broker.fees(bcfg, "stock", "buy", gross)
    a.state["cash"] = round(a.cash - gross - cm - tax - tf, 2)
    pos = a.positions.get(code)
    if pos is None:
        pos = a.positions[code] = {"name": name, "kind": "stock", "shares": 0.0, "cost": 0.0, "first_buy_date": date,
                                   "hfq": market.hfq_factor(code, date), "last_price": px}
    pos["shares"] += qty
    pos["cost"] += gross + cm + tax + tf
    a.append_trade({"date": date, "order_id": f"{date.replace('-', '')}-s002-{code}", "code": code, "name": name,
                    "side": "buy", "shares": qty, "price": fill, "amount": round(gross, 2), "commission": cm,
                    "stamp_tax": tax, "transfer_fee": tf, "slippage_cost": round(qty * px * SLIP, 2),
                    "new_position": pos["shares"] == qty, "reason_type": "s002_rebalance", "fill_source": "open"})
    return qty


def settle(date: str, market, log=log) -> dict:
    if not cal.is_trading_day(date):
        log(f"{date} 非交易日，跳过")
        return {"status": "skipped"}
    a = acct()
    if a.state.get("last_settled") == date:
        log(f"{date} 已结算，跳过")
        return {"status": "settled"}
    bcfg = config.load("broker")
    lot = bcfg["lot_size"]
    notes = []

    # 1) 除权除息：后复权因子变化 → 按比例折算持股（同 sim/settle.py）
    for code, pos in a.positions.items():
        f_now = market.hfq_factor(code, date)
        prev = pos.get("hfq")
        if f_now is None:
            notes.append(f"{pos['name']} 复权因子取数失败，今日未检查除权")
            continue
        if prev and abs(f_now / prev - 1) > 1e-6:
            ratio = f_now / prev
            if CORP_RATIO_RANGE[0] <= ratio <= CORP_RATIO_RANGE[1]:
                pos["shares"] *= ratio
                a.append_trade({"date": date, "code": code, "name": pos["name"], "side": "corp_action",
                                "shares": round(pos["shares"], 4), "note": f"除权除息，持股按 {ratio:.6f} 折算"})
                notes.append(f"{pos['name']} 除权除息 ×{ratio:.4f}")
            else:
                notes.append(f"{pos['name']} 复权因子跳变 ×{ratio:.3f} 超出合理范围，未折算，需人工复核")
                continue
        pos["hfq"] = f_now

    opens, prevs = {}, {}
    path = orders_dir() / f"{date}.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    want = {p["code"]: p["name"] for p in doc["picks"]} if doc else {}
    for code in set(a.positions) | set(want) | set(a.state.get("pending_exits", {})):
        b = market.bar(code, date)
        opens[code] = float(b["open"]) if b is not None else None
        prevs[code] = market.prev_close(code, date)

    # 2) 顺延的卖单：开盘可卖就卖到目标股数
    pend = a.state.setdefault("pending_exits", {})
    for code, keep in list(pend.items()):
        if code not in a.positions:
            pend.pop(code)
            continue
        pos = a.positions[code]
        if _sellable(opens.get(code), prevs.get(code), code, pos["name"]):
            _sell(a, code, pos["shares"] - keep, opens[code], date, bcfg, "s002_deferred_exit")
            pend.pop(code)
        else:
            notes.append(f"{pos['name']} 仍卖不出，继续顺延")

    # 3) 月度换仓
    sold = bought = 0
    if doc:
        px = {c: (opens.get(c) or a.positions[c].get("last_price")) for c in a.positions}
        equity = a.cash + sum(p["shares"] * px[c] for c, p in a.positions.items())
        target = equity / N
        for code in list(a.positions):
            pos = a.positions[code]
            if code in want:
                v = pos["shares"] * px[code]
                if v <= target * (1 + REBAL_TOL):
                    continue
                keep = math.floor(target / px[code] / lot) * lot
            else:
                keep = 0
            qty = pos["shares"] - keep
            if qty <= 0:
                continue
            if _sellable(opens.get(code), prevs.get(code), code, pos["name"]):
                _sell(a, code, qty, opens[code], date, bcfg, "s002_rebalance")
                sold += 1
            else:
                pend[code] = keep
                notes.append(f"{pos['name']} 开盘跌停/停牌，卖单顺延")
        for code, name in want.items():
            have = a.positions[code]["shares"] * opens[code] if code in a.positions and opens.get(code) else 0.0
            if code in a.positions and have >= target * (1 - REBAL_TOL):
                continue
            if not _buyable(opens.get(code), prevs.get(code), code, name):
                notes.append(f"{name}({code}) 开盘接近涨停或停牌，未买入，资金留现金")
                continue
            qty = math.floor((target - have) / (opens[code] * (1 + SLIP)) / lot) * lot
            if qty > 0 and _buy(a, code, name, qty, opens[code], date, bcfg, lot, market):
                bought += 1
        a.state.setdefault("executed", []).append(date)

    # 4) 估值与净值
    stale = 0
    for code, pos in a.positions.items():
        c = market.close(code, date) or market.last_close(code, date)
        if c is None:
            stale += 1
        else:
            pos["last_price"] = c
    if a.positions and stale * 2 > len(a.positions):
        raise RuntimeError(f"{stale}/{len(a.positions)} 只持仓取不到价格，判定为数据源故障，本次不结算")
    held = sum(p["shares"] * p["last_price"] for p in a.positions.values())
    equity = a.cash + held
    a.state["peak"] = max(a.state["peak"], equity)
    dd = 1 - equity / a.state["peak"] if a.state["peak"] else 0.0
    a.append_nav({"date": date, "cash": round(a.cash, 2), "stock_value": round(held, 2), "etf_value": 0,
                  "equity": round(equity, 2), "stock_weight": round(held / equity, 4) if equity else 0,
                  "etf_weight": 0, "peak": round(a.state["peak"], 2), "drawdown": round(dd, 4),
                  "regime": "", "risk_state": "normal"})
    a.state["last_settled"] = date
    a.save()
    log(f"{date} 结算：{'换仓日，' if doc else ''}卖 {sold} 只，买 {bought} 只，持仓 {len(a.positions)}，"
        f"现金 {a.cash:,.0f}，净值 {equity:,.0f}，回撤 {dd:.2%}")
    for n in notes:
        log(f"  {n}")
    return {"status": "ok", "sold": sold, "bought": bought, "equity": equity, "notes": notes}


# ---------------------------------------------------------------- 报告
def report() -> str:
    lines = [f"# {PROFILE.upper().replace('S', 'S-', 1)} 挑战者报告（{RULE}，前向纸面验证）", "",
             f"> 规则：{PROFILES[PROFILE]['desc']}独立 {INIT_CASH / 1e4:.0f} 万纸面账户。", ""]
    try:
        nav = pd.DataFrame(acct().nav())
    except Exception:
        nav = pd.DataFrame()
    if len(nav):
        eq = nav["equity"].astype(float)
        days = max(cal.count_between(nav.date.iloc[0], nav.date.iloc[-1]), 1)
        cagr = (eq.iat[-1] / INIT_CASH) ** (244 / days) - 1 if days >= 20 else float("nan")
        lines += [f"- 净值 {eq.iat[-1]:,.0f}（{eq.iat[-1] / INIT_CASH - 1:+.2%}），最大回撤 {nav['drawdown'].astype(float).max():.2%}，"
                  f"起始 {nav.date.iloc[0]}，最近 {nav.date.iloc[-1]}" + (f"，年化 {cagr:+.1%}" if cagr == cagr else "")]
    path = data_dir() / "periods.csv"
    per = pd.read_csv(path) if path.exists() else pd.DataFrame()
    n_rebal = len(list(orders_dir().glob("*.json"))) if orders_dir().exists() else 0
    lines.append(f"- 已发出 {n_rebal} 次换仓信号；已完成对照记录 {len(per)} 期")
    if len(per):
        lines += ["", f"| 换仓日 | 截至 | 账户 | {RULE} 理想 | K3 纯小市值 | 等权全体 |", "|---|---|---|---|---|---|"]
        for r in per.itertuples():
            lines.append(f"| {r.exec_date} | {r.end_date} | {r.account:+.2%} | {r.f4_ideal:+.2%} | {r.k3:+.2%} | {r.ew:+.2%} |")
        cum = (1 + per[["account", "f4_ideal", "k3", "ew"]]).prod() - 1
        lines.append(f"| **累计** | | {cum.account:+.2%} | {cum.f4_ideal:+.2%} | {cum.k3:+.2%} | {cum.ew:+.2%} |")
        if len(per) >= EVAL_REBALS:
            # 规则本身看「F4 理想」列；10 万账户（整手、现金闲置）是否可行看「账户」列
            for label, col in ((f"规则（{RULE} 理想）", "f4_ideal"), ("10 万账户", "account")):
                ann = (1 + cum[col]) ** (12 / len(per)) - 1
                ok = ann >= EVAL_CAGR and cum[col] > cum.k3
                lines += [f"- **评估·{label}**：满 {len(per)} 期，年化 {ann:+.1%}（门槛 {EVAL_CAGR:.0%}），"
                          f"相对 K3 {cum[col] - cum.k3:+.2%} → " + ("**达标**" if ok else "**未达标**")]
            lines.append("- 两项都达标才可提交主策略提案；只有规则达标，说明需要更大资金或改持股数（须重新预注册）")
        else:
            lines += ["", f"- 评估：{len(per)}/{EVAL_REBALS} 期，样本未满，只算观察"]
    lines += ["", "---", "本报告由程序生成，仅供研究参考，不构成投资建议。"]
    out = data_dir() / "report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)


def status() -> None:
    a = acct()
    held = sum(p["shares"] * p["last_price"] for p in a.positions.values())
    print(f"== {PROFILE} 挑战者（{ACCOUNT}）现金 {a.cash:,.0f}  持仓 {len(a.positions)} 只 {held:,.0f}  "
          f"最近结算 {a.state.get('last_settled')}  顺延卖单 {len(a.state.get('pending_exits', {}))}")
    nxt = sorted(orders_dir().glob("*.json"))[-1:] if orders_dir().exists() else []
    if nxt:
        d = json.loads(nxt[0].read_text(encoding="utf-8"))
        print(f"  最近信号 {d['signal_date']} → 换仓 {d['exec_date']}，入选 {len(d['picks'])} 只")


def main() -> int:
    from sim.market import Market
    ap = argparse.ArgumentParser(description="S-003 挑战者（独立账户）")
    ap.add_argument("--profile", default="s003", choices=list(PROFILES))  # 只剩 s003，保留参数兼容旧命令
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--date", required=True)
    p = sub.add_parser("signal"); p.add_argument("--date"); p.add_argument("--force", action="store_true")
    p.add_argument("--workers", type=int, default=8)
    p = sub.add_parser("settle"); p.add_argument("--date")
    sub.add_parser("report")
    sub.add_parser("status")
    a = ap.parse_args()
    use(a.profile)
    if a.cmd == "init":
        try:
            Account.create(ACCOUNT, STRATEGY, INIT_CASH, a.date)
            log(f"创建 {ACCOUNT}，初始 {INIT_CASH:,.0f}，起始 {a.date}")
        except FileExistsError:
            log(f"{ACCOUNT} 已存在")
        return 0
    if a.cmd == "signal":
        signal(a.date or cal.today(), a.force, a.workers)
        report()
        return 0
    if a.cmd == "settle":
        d = a.date or cal.today()
        if not cal.is_trading_day(d):
            log(f"{d} 非交易日，跳过")
            return 0
        settle(d, Market(), log)
        report()
        return 0
    if a.cmd == "report":
        print(report())
        return 0
    status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
