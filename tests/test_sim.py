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

    def daily(self, code, end=None):
        """只用于 regime / 数据新鲜度检查：按价格表拼出日线，没价格返回 None。"""
        import pandas as pd
        dates = sorted(d for d, px in self.prices.items() if px.get(code) is not None and (end is None or d <= end))
        if not dates:
            return None
        return pd.DataFrame({"close": [self.prices[d][code] for d in dates]}, index=dates)


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


def test_hfq_fetch_failure_keeps_shares_and_factor():
    """因子取数失败必须返回 None：不能被当成 1.0 后乘错持股，恢复后也不能再乘一次。"""
    acct = new_account(cash=0)
    acct.state["peak"] = 35000
    acct.positions["sh600000"] = {"name": "浦发银行", "kind": "stock", "shares": 1000, "cost": 35000,
                                  "first_buy_date": D0, "last_buy_date": D0, "last_price": 35,
                                  "stop_price": 32.2, "hfq": 3.5}
    m = FakeMarket({D1: {"sh600000": 35.0}}, factors={("sh600000", D1): None})
    r = settle.settle(acct, D1, m, RCFG, "neutral")
    pos = acct.positions["sh600000"]
    assert pos["shares"] == 1000 and pos["hfq"] == 3.5
    assert not [t for t in acct.trades() if t["side"] == "corp_action"]
    assert any("复权因子取数失败" in n for n in r["notes"])

    r2 = settle.settle(acct, D2, FakeMarket({D2: {"sh600000": 35.0}}, factors={("sh600000", D2): 3.5}), RCFG, "neutral")
    assert acct.positions["sh600000"]["shares"] == 1000
    assert acct.positions["sh600000"]["hfq"] == 3.5
    assert not [t for t in acct.trades() if t["side"] == "corp_action"]
    assert r2["notes"] == []


def test_buy_without_factor_records_first_valid_factor_without_adjusting():
    acct = new_account()
    m = FakeMarket({D0: {"sh600000": 10.0}, D1: {"sh600000": 10.0}},
                   factors={("sh600000", D1): None, ("sh600000", D2): 1.05})
    broker.execute(acct, order_doc({"code": "sh600000", "name": "浦发银行", "target_weight": 0.1}),
                   D1, m, BCFG, 0.08)
    assert acct.positions["sh600000"]["hfq"] is None
    settle.settle(acct, D2, FakeMarket({D2: {"sh600000": 10.0}}, factors={("sh600000", D2): 1.05}), RCFG, "neutral")
    assert acct.positions["sh600000"]["shares"] == 1000  # 首个有效因子只记录，不折算
    assert acct.positions["sh600000"]["hfq"] == 1.05


def test_absurd_corp_ratio_is_not_applied():
    """单日因子比值超出 [0.5, 3.0] 时视为数据错误：不折算、不覆盖原因子。"""
    acct = new_account(cash=0)
    acct.state["peak"] = 35000
    acct.positions["sh600000"] = {"name": "浦发银行", "kind": "stock", "shares": 1000, "cost": 35000,
                                  "first_buy_date": D0, "last_buy_date": D0, "last_price": 35,
                                  "stop_price": 32.2, "hfq": 3.5}
    r = settle.settle(acct, D1, FakeMarket({D1: {"sh600000": 35.0}}, factors={("sh600000", D1): 1.0}), RCFG, "neutral")
    assert acct.positions["sh600000"]["shares"] == 1000
    assert acct.positions["sh600000"]["hfq"] == 3.5
    assert any("超出合理范围" in n for n in r["notes"])
    assert not [t for t in acct.trades() if t["side"] == "corp_action"]


def test_corp_action_adjusts_stop_price():
    """10 送 10：持股翻倍、股价减半，止损价必须同步减半，否则当天就被误判止损。"""
    acct = new_account(cash=10000)
    acct.state["peak"] = 30000
    acct.positions["sh600000"] = {"name": "浦发银行", "kind": "stock", "shares": 1000, "cost": 20000,
                                  "first_buy_date": D0, "last_buy_date": D0, "last_price": 20,
                                  "stop_price": 18.4, "hfq": 1.0}
    r = settle.settle(acct, D1, FakeMarket({D1: {"sh600000": 10.0}}, factors={("sh600000", D1): 2.0}), RCFG, "neutral")
    pos = acct.positions["sh600000"]
    assert pos["shares"] == pytest.approx(2000) and pos["stop_price"] == pytest.approx(9.2)
    assert r["pending_exits"] == {} and acct.state["last_settled"] == D1


def test_settle_aborts_when_most_positions_have_no_price():
    """取价大面积失败是数据源故障，不是停牌：不能照常写净值。"""
    acct = new_account(cash=0)
    for i in range(3):
        acct.positions[f"sh60000{i}"] = {"name": f"股{i}", "kind": "stock", "shares": 100, "cost": 1000,
                                         "first_buy_date": D0, "last_buy_date": D0, "last_price": 10,
                                         "stop_price": 9.2, "hfq": 1}
    m = FakeMarket({D1: {"sh600000": 10.0}})
    with pytest.raises(settle.StaleData):
        settle.settle(acct, D1, m, RCFG, "neutral")
    assert acct.nav() == [] and acct.state["last_settled"] is None


def test_regime_falls_back_to_defense_and_data_freshness_gate():
    m = FakeMarket({D1: {"sh000300": 4000.0}})
    assert settle.regime(m, "sh000300", D1) == "defense"          # 不足 125 行：最保守档
    assert settle.data_fresh(m, "sh000300", D1) is True
    assert settle.data_fresh(m, "sh000300", D2) is False          # 指数还没更新到结算日
    assert settle.data_fresh(FakeMarket({}), "sh000300", D1) is False


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


def test_risk_rejects_stop_not_below_price_and_too_tight_stop():
    """止损价 >= 现价时单笔风险上限会退化成单票上限；距离过近同样会让仓位被算得过大。"""
    acct = new_account()
    out, _ = risk.review(acct, [{"code": "sh600000", "name": "浦发银行", "target_weight": 0.1, "stop_price": 10.5},
                                {"code": "sz000002", "name": "万科A", "target_weight": 0.1, "stop_price": 10.0},
                                {"code": "sh600001", "name": "邯郸钢铁", "target_weight": 0.1, "stop_price": 9.95}],
                         ctx(prices={"sh600000": 10.0, "sz000002": 10.0, "sh600001": 10.0, ETF["code"]: 4.0},
                             info={c: {"avg_amount": 5e8, "chg_1d": 0.01, "chg_5d": 0.02, "listed_days": 200}
                                   for c in ("sh600000", "sz000002", "sh600001")}))
    o = by_code(out)
    assert o["sh600000"]["status"] == "rejected" and "不低于现价" in o["sh600000"]["risk_notes"][-1]
    assert o["sz000002"]["status"] == "rejected" and "不低于现价" in o["sz000002"]["risk_notes"][-1]
    assert o["sh600001"]["status"] == "rejected" and "止损距离" in o["sh600001"]["risk_notes"][-1]


def test_risk_cap_still_uses_one_percent_risk_for_valid_stop():
    acct = new_account()
    out, _ = risk.review(acct, [{"code": "sh600000", "name": "浦发银行", "target_weight": 0.3, "stop_price": 9.2}],
                         ctx())
    o = by_code(out)["sh600000"]
    assert o["status"] == "adjusted" and o["target_weight"] == pytest.approx(0.125)


# ---------------- 数据新鲜度与命令层 ----------------
def test_stale_index_data_aborts_settle():
    from sim import __main__ as cli
    acct = new_account()
    m = FakeMarket({D0: {"sh000300": 4000.0}})  # 指数只更新到 D0，结算日 D1 没有日线
    with pytest.raises(settle.StaleData):
        cli.do_settle(D1, m, RCFG, BCFG)
    with pytest.raises(settle.StaleData):
        cli.do_plan(D1, m, RCFG, BCFG, config.load("strategy"), "defense")
    assert acct.nav() == [] and acct.state["last_settled"] is None
    assert not (config.orders_dir() / D2).exists()


def test_validate_draft_keeps_good_orders_and_reports_each_problem():
    from sim.strategies import validate_draft
    good, meta, errs = validate_draft({"meta": {"model": "m", "decided_on": D1}, "orders": [
        {"code": "sh600000", "target_weight": 0.05},
        {"code": "600887", "target_weight": 0.05},
        {"code": "sz000002", "target_weight": "5%"},
        {"code": "sz000001", "target_weight": 0.03, "stop_price": -1},
        {"code": "sh600001", "target_weight": 1.2},
    ]})
    assert [o["code"] for o in good] == ["sh600000"] and meta["model"] == "m" and len(errs) == 4
    assert validate_draft("not a dict")[0] == []
    assert validate_draft({"foo": 1})[0] == [] and "orders" in validate_draft({"foo": 1})[2][0]


def test_bad_draft_does_not_block_other_accounts(monkeypatch):
    """main 草稿坏了，quant / random / etf_band 也必须照常冻结。"""
    import pandas as pd
    from sim import __main__ as cli, screen as screen_mod
    for name, strategy in (("main", "agent"), ("quant", "quant"), ("random", "random"),
                           ("etf_band", "etf_band"), ("etf", "etf")):
        acct = Account.create(name, strategy, 100000, D0)
        acct.state["last_settled"] = D1
        acct.save()
    d = config.orders_dir() / D2
    d.mkdir(parents=True)
    (d / "main.draft.json").write_text('{"orders": [{"code": "600887", "target_weight": 0.05}]}', "utf-8")
    monkeypatch.setattr(screen_mod, "run", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(screen_mod, "info_map", lambda sc: {})
    monkeypatch.setattr(cli.orders, "git_commit", lambda msg: True)
    rc = cli.do_plan(D1, FakeMarket({D1: {"sh510300": 4.0, "sh000300": 4000.0}}), RCFG, BCFG,
                     config.load("strategy"), "defense")
    assert rc == 0
    for name in ("main", "quant", "random", "etf_band", "etf"):
        assert (d / f"{name}.json").exists(), name
    doc = json.loads((d / "main.json").read_text(encoding="utf-8"))
    assert doc["draft_errors"] and doc["orders"][0]["code"] == ETF["code"]  # 只剩底仓停泊
    # etf 固定 98%；etf_band 跟当日仓位区间上沿（defense 为 50%）
    assert json.loads((d / "etf.json").read_text())["orders"][0]["target_weight"] == 0.98
    assert json.loads((d / "etf_band.json").read_text())["orders"][0]["target_weight"] == 0.5


def test_stale_draft_is_discarded(monkeypatch):
    """decided_on 早于结算日的草稿不能用来下单（周末草稿周一才冻结的情况）。"""
    import pandas as pd
    from sim import __main__ as cli, screen as screen_mod
    acct = Account.create("main", "agent", 100000, D0)
    acct.state["last_settled"] = D1
    acct.save()
    d = config.orders_dir() / D2
    d.mkdir(parents=True)
    (d / "main.draft.json").write_text(json.dumps({
        "meta": {"decided_on": "2026-10-20"},
        "orders": [{"code": "sh600000", "name": "浦发银行", "target_weight": 0.05}]}), "utf-8")
    monkeypatch.setattr(screen_mod, "run", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(screen_mod, "info_map", lambda sc: {})
    monkeypatch.setattr(cli.orders, "git_commit", lambda msg: True)
    cli.do_plan(D1, FakeMarket({D1: {"sh510300": 4.0, "sh600000": 10.0, "sh000300": 4000.0}}), RCFG, BCFG,
                config.load("strategy"), "defense")
    doc = json.loads((d / "main.json").read_text(encoding="utf-8"))
    assert all(o["code"] == ETF["code"] for o in doc["orders"])
    assert any("早于结算日" in e for e in doc["draft_errors"])
    assert doc["meta"]["decided_on"] == "2026-10-20"


def test_catch_up_execute_before_settle():
    """成交漏跑时，evening 先补执行再结算，当天净值要反映持仓。"""
    from sim import __main__ as cli
    acct = Account.create("t", "agent", 100000, D0)
    d = config.orders_dir() / D1
    d.mkdir(parents=True)
    orders.freeze(D1, "t", {"orders": [{"code": "sh600000", "name": "浦发银行", "target_weight": 0.1,
                                        "status": "approved", "reason_type": "entry"}]})
    m = FakeMarket({D1: {"sh600000": 10.0}})
    assert cli.catch_up_execute(D1, m, BCFG, RCFG) == 0
    assert Account("t").positions["sh600000"]["shares"] == 1000
    assert D1 in Account("t").state["executed"]
    settle.settle(Account("t"), D1, m, RCFG, "neutral")
    assert float(Account("t").nav()[-1]["stock_value"]) > 0


def test_execute_refuses_account_already_settled():
    from sim import __main__ as cli
    acct = Account.create("t", "agent", 100000, D0)
    acct.state["last_settled"] = D1
    acct.save()
    d = config.orders_dir() / D1
    d.mkdir(parents=True)
    orders.freeze(D1, "t", {"orders": [{"code": "sh600000", "name": "浦发银行", "target_weight": 0.1,
                                        "status": "approved", "reason_type": "entry"}]})
    cli.run_orders(D1, FakeMarket({D1: {"sh600000": 10.0}}), BCFG, RCFG)
    assert Account("t").positions == {} and D1 not in Account("t").state["executed"]


def test_git_commit_outside_repo_is_not_a_failure():
    assert orders.git_commit("noop") is True  # SIM_HOME 是临时目录，没有 .git


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


# ---------------- 策略归因 ----------------
def test_attrib_episodes_link_to_frozen_orders(tmp_path):
    from sim import attrib
    acct = Account.create("main", "agent", 100000, "2026-10-01")
    d = config.orders_dir() / "2026-10-05"
    d.mkdir(parents=True)
    (d / "main.json").write_text(json.dumps({"trade_date": "2026-10-05", "strategy_version": "strategy-v1.0", "orders": [
        {"id": "o1", "code": "sh600001", "reason": "动量领先，置信度高", "channels": "momentum|trend"},
        {"id": "o2", "code": "sh600002", "reason": "回调企稳", "confidence": "低", "channels": "pullback"}]}), "utf-8")
    for t in [dict(date="2026-10-05", order_id="o1", code="sh600001", name="甲", side="buy", amount=10000),
              dict(date="2026-10-05", order_id="o2", code="sh600002", name="乙", side="buy", amount=5000),
              dict(date="2026-10-05", order_id="e1", code="sh510300", name="ETF", side="buy", amount=40000),
              dict(date="2026-10-12", order_id="o3", code="sh600001", name="甲", side="sell", realized_pnl=800,
                   closed=True, reason_type="exit")]:
        acct.append_trade(t)
    acct.state["positions"]["sh600002"] = {"name": "乙", "kind": "stock", "shares": 500, "cost": 5000.0, "last_price": 9.0}
    acct.save()
    eps = {e["code"]: e for e in attrib.episodes(Account("main"), attrib.order_index("main"))}
    assert set(eps) == {"sh600001", "sh600002"}                                         # ETF 不算个股持仓
    a, b = eps["sh600001"], eps["sh600002"]
    assert a["closed"] and a["pnl_pct"] == pytest.approx(0.08) and a["confidence"] == "高"
    assert a["version"] == "strategy-v1.0" and a["exit_reason"] == "exit"
    assert not b["closed"] and b["pnl"] == pytest.approx(-500) and b["confidence"] == "低"
    by_ch = {g["group"]: g for g in attrib.group_stats(list(eps.values()), lambda e: e["channels"].split("|"))}
    assert set(by_ch) == {"momentum", "trend"} and by_ch["momentum"]["win_rate"] == 1.0  # 只统计已平仓
    md = attrib.to_markdown(attrib.build("2026-10-12"))
    assert "未达到" in md and "momentum" in md
