"""隔夜实验单元测试（不联网）：.venv/bin/python -m pytest tests/test_overnight.py -q"""
import datetime as dt
import json

import pandas as pd
import pytest

from sim import calendar as cal, config, orders, overnight as on
from sim.ledger import Account

D0, D1, D2, D3 = "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_HOME", str(tmp_path))
    days, d = [], dt.date(2026, 1, 1)
    while d.year == 2026:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    monkeypatch.setattr(cal, "trade_dates", lambda: tuple(days))
    on.init("2026-11-02", lambda m: None)


class FakeMarket:
    """bars: {日期: {代码: (open, close)}}；30 天历史日线保证上市天数与成交额检查通过。"""

    def __init__(self, bars, amount=5e8, spot=None):
        self.bars, self.amount, self._spot = bars, amount, spot

    def bar(self, code, date):
        x = self.bars.get(date, {}).get(code)
        return None if x is None else pd.Series({"open": x[0], "close": x[1]})

    def prev_close(self, code, date):
        d = cal.prev_day(date)
        x = self.bars.get(d, {}).get(code)
        return x[1] if x else 10.0

    def daily(self, code, end=None):
        if code == "sh000300":
            dates = sorted(d for d in self.bars if end is None or d <= end)
            return pd.DataFrame({"close": [1.0] * len(dates)}, index=dates)
        idx = [f"h{i:02d}" for i in range(30)]
        return pd.DataFrame({"amount": [self.amount] * 30}, index=idx)

    def spot(self):
        return self._spot

    def _minutes(self, code):
        return None


def at(date, hm="14:52"):
    return dt.datetime.fromisoformat(f"{date}T{hm}:00")


def write_draft(date, picks, vetoed=(), name="overnight_agent.draft.json", decided="14:50"):
    d = config.orders_dir() / date
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"decided_at": f"{date}T{decided}:00", "market_view": "测试",
                                      "picks": list(picks), "vetoed": list(vetoed)}, ensure_ascii=False))


def pick(code, w=0.3, p=0.6):
    return {"code": code, "name": "测试股", "weight": w, "p_win": p, "exp_gap": 0.01,
            "thesis_type": "题材发酵", "thesis": "x", "kill": ["y"]}


def scores():
    return pd.read_csv(config.home() / "data" / "overnight" / "scores.csv", dtype={"code": str})


def test_buy_at_close_sell_at_next_open_with_costs():
    m = FakeMarket({D0: {"sh600000": (10.0, 10.0)}, D1: {"sh600000": (10.5, 10.2)}})
    write_draft(D0, [pick("sh600000", 0.3)])
    assert on.freeze(D0, m, print, now=at(D0)) == 0
    on.run_settle(D0, m, print)
    a = Account("overnight/agent")
    assert a.positions["sh600000"]["shares"] == 3000 and a.positions["sh600000"]["buy_price"] == 10.0
    on.run_settle(D1, m, print)
    a = Account("overnight/agent")
    assert not a.positions
    gross = 3000 * 10.5
    expected = 100000 - 30000 - 7.5 - 0.3 + gross - gross * 0.00025 - gross * 0.0005 - gross * 0.00001
    assert a.cash == pytest.approx(expected, abs=0.05)
    s = scores()
    row = s[s["account"] == "agent"].iloc[0]
    assert row["status"] == "filled" and row["win"] == 1 and row["ret_net"] == pytest.approx(0.0489, abs=1e-3)


def test_limit_up_close_is_unfilled_and_limit_down_open_carries():
    m = FakeMarket({D0: {"sh600000": (10.0, 11.0), "sh600001": (10.0, 10.0)},
                    D1: {"sh600001": (9.0, 9.5)}, D2: {"sh600001": (9.6, 9.7)}})
    write_draft(D0, [pick("sh600000"), pick("sh600001")])
    on.freeze(D0, m, print, now=at(D0))
    on.run_settle(D0, m, print)
    a = Account("overnight/agent")
    assert list(a.positions) == ["sh600001"]
    on.run_settle(D1, m, print)  # 开盘 9.0 = 跌停价，卖不出
    assert "sh600001" in Account("overnight/agent").positions
    on.run_settle(D2, m, print)
    assert not Account("overnight/agent").positions
    s = scores()
    assert set(s[s["account"] == "agent"]["status"]) == {"unfilled", "carried"}


def test_freeze_deadline_and_stale_draft_rejected():
    m = FakeMarket({D0: {"sh600000": (10.0, 10.0)}})
    write_draft(D0, [pick("sh600000")])
    assert on.freeze(D0, m, print, now=at(D0, "14:58")) == 1
    assert not orders.exists(D0, "overnight_agent")
    write_draft(D0, [pick("sh600000")], decided="14:55")  # 决策时间晚于冻结时间
    on.freeze(D0, m, print, now=at(D0, "14:53"))
    doc = orders.load_verified(D0, "overnight_agent")
    assert doc["source"] == "missed" and doc["orders"] == []


def test_provisional_used_when_close_draft_missing():
    m = FakeMarket({D0: {"sh600000": (10.0, 10.0)}})
    write_draft(D0, [pick("sh600000")], name="overnight_agent.provisional.json", decided="11:40")
    on.freeze(D0, m, print, now=at(D0))
    doc = orders.load_verified(D0, "overnight_agent")
    assert doc["source"] == "provisional" and doc["orders"][0]["status"] == "approved"


def test_review_rules_caps_and_required_fields():
    m = FakeMarket({}, amount=1e7)
    r = on.rules()
    low = on.review([pick("sh600000")], m, None, D0, r, require=True)
    assert low[0]["status"] == "rejected" and "日均成交额" in low[0]["notes"][0]
    m = FakeMarket({})
    picks = [pick(f"sh60000{i}", 0.5) for i in range(7)] + [{"code": "sz000001", "weight": 0.1}]
    out = on.review(picks, m, None, D0, r, require=True)
    ok = [o for o in out if o["status"] == "approved"]
    assert len(ok) == r["max_picks"]
    assert sum(o["weight"] for o in ok) == pytest.approx(r["max_total"], abs=1e-3)
    assert all(o["weight"] <= r["max_single"] for o in ok)
    assert out[-1]["status"] == "rejected" and "缺少字段" in out[-1]["notes"][-1]
    st = on.review([{"code": "sh600002", "name": "*ST某某", "weight": 0.1}], m, None, D0, r, require=False)
    assert st[0]["status"] == "rejected"


def test_quant_random_from_scan_and_veto_counterfactual():
    spot = pd.DataFrame({"代码": ["sh600000", "sh600001", "sz000001", "sh600002"],
                         "名称": ["甲", "乙", "丙", "*ST丁"], "最新价": [10.5, 10.3, 10.6, 10.5],
                         "涨跌幅": [5.0, 3.0, 6.0, 5.0], "昨收": [10.0] * 4, "今开": [10.0] * 4,
                         "最高": [10.55, 10.4, 10.6, 10.5], "最低": [9.9, 9.9, 9.95, 9.9],
                         "成交量": [1e7] * 4, "成交额": [5e8] * 4, "时间戳": ["14:40:00"] * 4})
    bars = {D0: {c: (10.0, 10.0) for c in ("sh600000", "sh600001", "sz000001")},
            D1: {c: (10.2, 10.2) for c in ("sh600000", "sh600001", "sz000001")}}
    m = FakeMarket(bars, spot=spot)
    cand = on.scan(D0, m, print, now=at(D0, "14:40"))
    assert "sh600002" not in set(cand["code"]) and len(cand) == 3
    write_draft(D0, [], vetoed=[{"code": "sz000001", "name": "丙", "p_win": 0.3, "reason": "今晚有减持公告"}])
    on.freeze(D0, m, print, now=at(D0))
    assert len([o for o in orders.load_verified(D0, "overnight_quant")["orders"] if o["status"] == "approved"]) == 3
    assert orders.load_verified(D0, "overnight_agent")["orders"] == []
    on.run_settle(D0, m, print)
    on.run_settle(D1, m, print)
    s = scores()
    veto = s[s["account"] == "agent_veto"].iloc[0]
    assert veto["status"] == "filled" and veto["ret_gross"] == pytest.approx(0.02)
    assert len(s[s["account"] == "quant"]) == 3
    text = open(on.report(D1), encoding="utf-8").read()
    assert "三组对照" in text and "主动空仓 1" in text


def test_settle_is_idempotent_and_catches_up():
    m = FakeMarket({D0: {"sh600000": (10.0, 10.0)}, D1: {"sh600000": (10.1, 10.1)},
                    D2: {"sh600000": (10.2, 10.2)}})
    write_draft(D0, [pick("sh600000")])
    on.freeze(D0, m, print, now=at(D0))
    on.run_settle(D2, m, print)  # 漏跑两天：一次补齐 D0、D1、D2
    a = Account("overnight/agent")
    assert a.state["last_settled"] == D2 and len(a.nav()) == 3 and not a.positions
    on.run_settle(D2, m, print)
    assert len(Account("overnight/agent").nav()) == 3


def test_missing_stock_bar_waits_before_20_then_treated_as_suspended():
    m = FakeMarket({D0: {"sh600000": (10.0, 10.0)}, D1: {}})
    write_draft(D0, [pick("sh600000")])
    on.freeze(D0, m, print, now=at(D0))
    on.run_settle(D0, m, print, now=at(D0, "15:30"))
    assert on.run_settle(D1, m, print, now=at(D1, "15:30")) == 1  # 个股日线未更新：不结算
    assert Account("overnight/agent").state["last_settled"] == D0
    on.run_settle(D1, m, print, now=at(D1, "20:30"))  # 20:00 后仍缺 → 停牌，顺延
    a = Account("overnight/agent")
    assert a.state["last_settled"] == D1 and "sh600000" in a.positions
