"""模拟交易内核的单元测试（不联网）：.venv/bin/python -m pytest tests -q"""
import datetime as dt
import json
import os

import pytest

from sim import broker, calendar as cal, config, orders, risk, settle
from sim.ledger import Account
from sim.market import Snapshot, limit_prices

BCFG = config.load("broker")
RCFG = config.load("risk")
ETF = BCFG["base_etf"]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_HOME", str(tmp_path))
    days, d = [], dt.date(2026, 1, 1)
    while d.year == 2026:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    monkeypatch.setattr(cal, "trade_dates", lambda: tuple(days))


class FakeMarket:
    """价格表：{日期: {代码: 价格}}；prev_close 取上一个交易日的价格。"""

    def __init__(self, prices, volume=1e9, avg_amount=5e8, factors=None, source="minute"):
        self.prices, self.volume, self._avg, self.factors, self.source = prices, volume, avg_amount, factors or {}, source

    def close(self, code, date):
        return self.prices.get(date, {}).get(code)

    last_close = close

    def prev_close(self, code, date):
        return self.close(code, cal.prev_day(date)) or self.close(code, date)

    def snapshot(self, code, date, name=""):
        px = self.close(code, date)
        if px is None:
            return None
        prev = self.prev_close(code, date)
        up, down = limit_prices(code, name, prev)
        return Snapshot(px, self.volume, prev, up, down, self.source)

    def avg_amount(self, code, date, n=20):
        return self._avg

    def hfq_factor(self, code, date):
        return self.factors.get((code, date), self.factors.get(code, 1.0))


def new_account(cash=100000, strategy="quant"):
    return Account.create("t", strategy, cash, "2026-10-01")


def order_doc(*orders_):
    return {"orders": [{"status": "approved", "reason_type": "entry"} | o for o in orders_]}


# ---------------- 费用 ----------------
def test_fees_minimum_and_stamp_tax():
    assert broker.fees(BCFG, "stock", "buy", 10000) == (5.0, 0.0, 0.1)
    assert broker.fees(BCFG, "stock", "sell", 100000) == (25.0, 50.0, 1.0)
    assert broker.fees(BCFG, "etf", "sell", 100000) == (25.0, 0.0, 0.0)


def test_limit_prices_by_board():
    assert limit_prices("sh600000", "浦发银行", 10.0) == (11.0, 9.0)
    assert limit_prices("sz300750", "宁德时代", 100.0) == (120.0, 80.0)
    assert limit_prices("sz000001", "*ST某某", 10.0) == (10.5, 9.5)
    assert limit_prices("sh510300", "沪深300ETF", 4.0) == (4.4, 3.6)


# ---------------- 撮合 ----------------
D0, D1, D2 = "2026-10-22", "2026-10-23", "2026-10-26"


def test_buy_rounds_to_lots_and_charges_costs():
    acct = new_account()
    m = FakeMarket({D0: {"sh600000": 10.0}, D1: {"sh600000": 10.0}})
    r = broker.execute(acct, order_doc({"code": "sh600000", "name": "浦发银行", "target_weight": 0.1}),
                       D1, m, BCFG, 0.08)
    assert r[0]["status"] == "filled" and r[0]["shares"] == 1000  # 按执行价 10 元换算目标股数，整手
    pos = acct.positions["sh600000"]
    assert pos["shares"] == 1000 and pos["stop_price"] == pytest.approx(pos["cost"] / 1000 * 0.92, abs=1e-3)
    assert acct.cash == pytest.approx(100000 - 1000 * 10.02 - 5.01 - 0.1, abs=0.01)


def test_limit_up_cannot_buy_and_limit_down_cannot_sell():
    acct = new_account()
    m = FakeMarket({D0: {"sh600000": 10.0}, D1: {"sh600000": 11.0}})
    r = broker.execute(acct, order_doc({"code": "sh600000", "name": "浦发银行", "target_weight": 0.1}),
                       D1, m, BCFG, 0.08)
    assert r[0]["status"] == "rejected" and "涨停" in r[0]["note"]

    acct.state["executed"] = []
    acct.positions["sh600000"] = {"name": "浦发银行", "kind": "stock", "shares": 1000, "cost": 10000,
                                  "first_buy_date": D0, "last_buy_date": D0, "last_price": 10, "stop_price": 9.2}
    m = FakeMarket({D0: {"sh600000": 10.0}, D1: {"sh600000": 9.0}})
    r = broker.execute(acct, order_doc({"code": "sh600000", "target_weight": 0.0, "reason_type": "stop_loss"}),
                       D1, m, BCFG, 0.08)
    assert r[0]["status"] == "rejected" and "跌停" in r[0]["note"]
    assert acct.positions["sh600000"]["shares"] == 1000


def test_t_plus_one_and_idempotent_execution():
    acct = new_account()
    m = FakeMarket({D0: {"sh600000": 10.0}, D1: {"sh600000": 10.0}})
    acct.positions["sh600000"] = {"name": "浦发银行", "kind": "stock", "shares": 1000, "cost": 10000,
                                  "first_buy_date": D1, "last_buy_date": D1, "last_price": 10, "stop_price": 9.2}
    r = broker.execute(acct, order_doc({"code": "sh600000", "target_weight": 0.0}), D1, m, BCFG, 0.08)
    assert "T+1" in r[0]["note"]
    again = broker.execute(acct, order_doc({"code": "sh600000", "target_weight": 0.0}), D1, m, BCFG, 0.08)
    assert again[0]["status"] == "skipped"


def test_sell_realizes_pnl_and_volume_cap_partial_fill():
    acct = new_account(cash=0)
    acct.positions["sh600000"] = {"name": "浦发银行", "kind": "stock", "shares": 1000, "cost": 10000,
                                  "first_buy_date": D0, "last_buy_date": D0, "last_price": 10, "stop_price": 9.2}
    m = FakeMarket({D0: {"sh600000": 10.0}, D1: {"sh600000": 11.0}}, volume=50000)  # 1% = 500 股
    r = broker.execute(acct, order_doc({"code": "sh600000", "target_weight": 0.0, "reason_type": "exit"}),
                       D1, m, BCFG, 0.08)
    assert r[0]["status"] == "partial" and r[0]["shares"] == 500
    t = acct.trades()[-1]
    fill = round(11 * 0.998, 3)
    gross = 500 * fill
    assert float(t["realized_pnl"]) == pytest.approx(gross - 5 - gross * 0.0005 - gross * 0.00001 - 5000, abs=0.02)


def test_sells_fund_buys_and_cash_never_negative():
    acct = new_account(cash=1000)
    acct.positions[ETF["code"]] = {"name": ETF["name"], "kind": "etf", "shares": 20000, "cost": 80000,
                                   "first_buy_date": D0, "last_buy_date": D0, "last_price": 4.0}
    m = FakeMarket({D0: {ETF["code"]: 4.0, "sh600000": 10.0}, D1: {ETF["code"]: 4.0, "sh600000": 10.0}})
    doc = order_doc({"code": "sh600000", "name": "浦发银行", "target_weight": 0.5},
                    {"code": ETF["code"], "name": ETF["name"], "target_weight": 0.45, "reason_type": "park"})
    broker.execute(acct, doc, D1, m, BCFG, 0.08)
    assert acct.cash >= 0
    assert acct.positions["sh600000"]["shares"] > 0
    assert acct.positions[ETF["code"]]["shares"] < 20000


# ---------------- 结算 ----------------
def test_settle_corp_action_drawdown_breaker_and_stops():
    acct = new_account(cash=0)
    acct.state["peak"] = 100000
    acct.positions["sh600000"] = {"name": "浦发银行", "kind": "stock", "shares": 10000, "cost": 100000,
                                  "first_buy_date": D0, "last_buy_date": D0, "last_price": 10, "stop_price": 9.2,
                                  "hfq": 1.0}
    m = FakeMarket({D1: {"sh600000": 8.0}}, factors={("sh600000", D1): 1.1})
    r = settle.settle(acct, D1, m, RCFG, "neutral")
    assert acct.positions["sh600000"]["shares"] == pytest.approx(11000)  # 送转/分红折算
    assert r["drawdown"] == pytest.approx(0.12)
    assert acct.state["breaker_until"] == cal.offset(D1, RCFG["drawdown"]["breaker_days"])
    assert r["risk_state"] == "breaker" and acct.state["pending_exits"] == {"sh600000": "breaker"}
    assert settle.settle(acct, D1, m, RCFG, "neutral")["skipped"]


def test_breaker_does_not_renew_forever_for_same_peak():
    acct = new_account(cash=88000)
    acct.state.update(peak=100000, breaker_peak=100000, breaker_until="2026-10-01")
    r = settle.settle(acct, D1, FakeMarket({}), RCFG, "neutral")
    assert r["risk_state"] == "warn"


def test_stop_loss_and_time_stop():
    acct = new_account(cash=0)
    acct.positions["sh600000"] = {"name": "A", "kind": "stock", "shares": 1000, "cost": 10000,
                                  "first_buy_date": D0, "last_buy_date": D0, "last_price": 10, "stop_price": 9.2, "hfq": 1}
    acct.positions["sh600001"] = {"name": "B", "kind": "stock", "shares": 1000, "cost": 10000,
                                  "first_buy_date": "2026-09-01", "last_buy_date": "2026-09-01", "last_price": 10,
                                  "stop_price": 9.2, "hfq": 1}
    acct.state["peak"] = 20000
    r = settle.settle(acct, D1, FakeMarket({D1: {"sh600000": 9.1, "sh600001": 9.9}}), RCFG, "neutral")
    assert r["pending_exits"] == {"sh600000": "stop_loss", "sh600001": "time_stop"}


# ---------------- 风控 ----------------
def ctx(**kw):
    base = {"trade_date": D2, "settle_date": D1, "regime": "offense", "risk_state": "normal", "rcfg": RCFG,
            "prices": {"sh600000": 10.0, "sh688001": 10.0, "sz000002": 10.0, ETF["code"]: 4.0},
            "info": {c: {"avg_amount": 5e8, "chg_1d": 0.01, "chg_5d": 0.02, "listed_days": 200}
                     for c in ("sh600000", "sh688001", "sz000002")},
            "equity": 100000, "base_etf": ETF, "fixed_etf_weight": None}
    return base | kw


def by_code(out):
    return {o["code"]: o for o in out}


def test_risk_caps_weight_by_single_trade_risk_and_excludes_boards():
    acct = new_account()
    out, band = risk.review(acct, [{"code": "sh600000", "name": "浦发银行", "target_weight": 0.3},
                                   {"code": "sh688001", "name": "华兴源创", "target_weight": 0.1},
                                   {"code": "sz000002", "name": "*ST某某", "target_weight": 0.1}], ctx())
    o = by_code(out)
    assert o["sh600000"]["status"] == "adjusted" and o["sh600000"]["target_weight"] == 0.125  # 1% / 8%
    assert o["sh688001"]["status"] == "rejected"
    assert o["sz000002"]["status"] == "rejected"
    assert o[ETF["code"]]["target_weight"] == pytest.approx(band["max"] - 0.125)


def test_risk_scales_total_stock_weight_in_defense():
    acct = new_account()
    drafts = [{"code": c, "name": c, "target_weight": 0.12} for c in ("sh600000", "sz000002")]
    out, band = risk.review(acct, drafts, ctx(regime="defense"))
    assert band["stock_max"] == 0.2
    assert sum(o["target_weight"] for o in out if o["code"] != ETF["code"]) == pytest.approx(0.2, abs=1e-3)


def test_risk_breaker_blocks_buys_and_forces_exits():
    acct = new_account()
    acct.positions["sz000002"] = {"name": "万科A", "kind": "stock", "shares": 1000, "cost": 10000,
                                  "first_buy_date": D1, "last_buy_date": D1, "last_price": 10}
    acct.state["pending_exits"] = {"sz000002": "breaker"}
    out, band = risk.review(acct, [{"code": "sh600000", "name": "浦发银行", "target_weight": 0.1}],
                            ctx(risk_state="breaker"))
    o = by_code(out)
    assert o["sh600000"]["status"] == "rejected"
    assert o["sz000002"]["status"] == "approved" and o["sz000002"]["target_weight"] == 0
    assert band["stock_max"] == 0


def test_risk_min_hold_days():
    acct = new_account()
    acct.positions["sz000002"] = {"name": "万科A", "kind": "stock", "shares": 1000, "cost": 10000,
                                  "first_buy_date": D1, "last_buy_date": D1, "last_price": 10}
    out, _ = risk.review(acct, [{"code": "sz000002", "name": "万科A", "target_weight": 0.0, "reason_type": "exit"}],
                         ctx())
    assert by_code(out)["sz000002"]["status"] == "rejected"


# ---------------- 订单冻结 ----------------
def test_frozen_orders_detect_tampering():
    orders.freeze(D2, "t", {"orders": []})
    assert orders.load_verified(D2, "t") == {"orders": []}
    with pytest.raises(FileExistsError):
        orders.freeze(D2, "t", {"orders": []})
    path = config.orders_dir() / D2 / "t.json"
    os.chmod(path, 0o644)
    path.write_text(json.dumps({"orders": [{"code": "x"}]}))
    with pytest.raises(RuntimeError):
        orders.load_verified(D2, "t")


# ---------------- 资金看板：阶段收益 ----------------
def test_period_and_monthly_returns():
    from sim.dashboard import monthly_returns, period_returns
    dates = ["2026-09-29", "2026-09-30", "2026-10-09", "2026-10-21", "2026-10-22", "2026-10-23"]
    values = [101, 102, 104, 106, 105, 110.25]
    p = period_returns(dates, values, 100)
    assert p["today"] == pytest.approx(110.25 / 105 - 1)
    assert p["week"] == pytest.approx(110.25 / 104 - 1)        # 本周从 10-19 起，期初取此前最后一个交易日 10-09
    assert p["month"] == pytest.approx(110.25 / 102 - 1)       # 期初为 9 月最后一个交易日
    assert p["year"] == pytest.approx(0.1025)                  # 今年之前没有数据，期初为初始资金
    assert p["all"] == pytest.approx(0.1025)
    assert p["d250"] is None                                   # 历史不足一年
    m = monthly_returns(dates, values, 100)
    assert m["2026"][9] == pytest.approx(0.02) and m["2026"][10] == pytest.approx(110.25 / 102 - 1)
    assert m["2026"]["year"] == pytest.approx(0.1025)


# ---------------- 主策略候选池 ----------------
def _bars(closes, amounts=None):
    import pandas as pd
    n = len(closes)
    idx = [f"d{i:03d}" for i in range(n)]
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"close": c, "high": c * 1.01, "amount": amounts or [2e8] * n}, index=idx)


def test_pool_features_and_channels():
    import pandas as pd
    from sim import pool
    up = _bars([10 * 1.004 ** i for i in range(200)])                                  # 稳步上涨
    dip = _bars([10 * 1.006 ** i for i in range(192)] + [31.6 * 0.98 ** i for i in range(1, 9)])  # 上升趋势中回调
    assert pool.features(_bars([10] * 100)) is None                                   # 数据不足
    fu, fd = pool.features(up), pool.features(dip)
    assert fu["ret_120"] > 0 and fu["near_high_60"] and fd["dd_60"] < -0.08 and fd["chg_5d"] < 0
    feat = pd.DataFrame([{"code": "sh600001", "name": "甲", "industry": "A", "pe": 12.0} | fu,
                         {"code": "sh600002", "name": "乙", "industry": "A", "pe": 80.0} | fd,
                         {"code": "sh600003", "name": "丙", "industry": "B", "pe": 10.0} | fu])
    feat["eligible"] = [True, True, False]
    cfg = {"per_channel": 5, "defensive_max_pe": 25, "breakout_amt_ratio": 1.5,
           "sector_top": 2, "sector_per": 2, "sector_min_size": 1}
    ch = pool.channels(feat, cfg)
    assert "sh600003" not in sum(ch.values(), [])                                     # 不合规的不召回
    assert ch["momentum"] == ["sh600001"] and ch["pullback"] == ["sh600002"]
    assert ch["defensive"] == ["sh600001"]                                             # 乙 PE 太高
    out = pool.assemble(feat, ch, holdings=["sh600003"], watch=["sh600002"])
    assert set(out["code"]) == {"sh600001", "sh600002", "sh600003"}                  # 持仓即使不合规也进池复核
    assert "watchlist" in out.set_index("code").loc["sh600002", "channels"]
    md = pool.to_markdown("2026-10-23", out, ch, {"spot": 3, "base": 3, "pre": 3, "feat": 3, "eligible": 2})
    assert "多通道共振" in md and "（不合规）" in md
