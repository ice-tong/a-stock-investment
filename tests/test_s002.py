"""S-002 挑战者单元测试（不联网）：.venv/bin/python -m pytest tests/test_s002.py -q"""
import datetime as dt
import json

import pandas as pd
import pytest

from sim import calendar as cal, config
from sim.ledger import Account, all_accounts
from tools import s002

D0, D1, D2 = "2026-11-02", "2026-11-03", "2026-11-04"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_HOME", str(tmp_path))
    days, d = [], dt.date(2026, 1, 1)
    while d.year == 2026:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    monkeypatch.setattr(cal, "trade_dates", lambda: tuple(days))
    monkeypatch.setattr(s002, "N", 2)  # 两只等权，便于手算
    Account.create(s002.ACCOUNT, s002.STRATEGY, s002.INIT_CASH, D0)


class FakeMarket:
    def __init__(self, bars, hfq=None):
        self.bars, self.hfq = bars, hfq or {}

    def bar(self, code, date):
        x = self.bars.get(date, {}).get(code)
        return None if x is None else pd.Series({"open": x[0], "close": x[1]})

    def prev_close(self, code, date):
        x = self.bars.get(cal.prev_day(date), {}).get(code)
        return x[1] if x else 10.0

    def close(self, code, date):
        x = self.bars.get(date, {}).get(code)
        return x[1] if x else None

    def last_close(self, code, date):
        return self.close(code, date) or 10.0

    def hfq_factor(self, code, date):
        return self.hfq.get(date, {}).get(code, 1.0)


def intent(exec_date, codes):
    doc = {"signal_date": cal.prev_day(exec_date), "exec_date": exec_date,
           "picks": [{"code": c, "name": c} for c in codes], "k3": codes, "ew": codes}
    s002._write_json(s002.orders_dir() / f"{exec_date}.json", doc)


def test_month_end_detection():
    assert s002.is_month_end("2026-10-30")
    assert not s002.is_month_end("2026-10-29")
    assert s002.signal("2026-10-29")["status"] == "skipped"


def test_isolated_from_main_accounts():
    assert all(a.name != s002.ACCOUNT for a in all_accounts())


def test_rebalance_equal_weight_skips_limit_up():
    intent(D0, ["sh600001", "sh600002"])
    m = FakeMarket({D0: {"sh600001": (10.0, 10.2), "sh600002": (10.99, 11.0)}})  # 600002 开盘 ≈ 涨停（前收 10）
    s002.settle(D0, m, print)
    a = Account(s002.ACCOUNT)
    assert set(a.positions) == {"sh600001"}
    # 目标 = 10 万 / 2；按 10.0×1.001 取整手
    assert a.positions["sh600001"]["shares"] == 4900
    assert a.cash > 4.9e4  # 买不进的那一半留现金


def test_exit_deferred_on_limit_down_then_sold():
    intent(D0, ["sh600001", "sh600002"])
    m = FakeMarket({D0: {"sh600001": (10.0, 10.0), "sh600002": (10.0, 10.0)},
                    D1: {"sh600001": (9.0, 9.0), "sh600002": (10.0, 10.0), "sh600003": (10.0, 10.0)},
                    D2: {"sh600001": (9.5, 9.6), "sh600002": (10.0, 10.0), "sh600003": (10.0, 10.0)}})
    s002.settle(D0, m, print)
    intent(D1, ["sh600002", "sh600003"])  # 600001 被换出，但 D1 开盘跌停
    s002.settle(D1, m, print)
    a = Account(s002.ACCOUNT)
    assert "sh600001" in a.positions and a.state["pending_exits"] == {"sh600001": 0}
    s002.settle(D2, m, print)
    a = Account(s002.ACCOUNT)
    assert "sh600001" not in a.positions and not a.state["pending_exits"]
    t = pd.read_csv(config.home() / "accounts" / s002.ACCOUNT / "trades.csv")
    last = t[(t.code == "sh600001") & (t.side == "sell")].iloc[-1]
    assert last.price == pytest.approx(9.5 * (1 - s002.SLIP), abs=1e-3)
    assert last.reason_type == "s002_deferred_exit"


def test_retained_within_tolerance_not_traded_and_corp_action():
    intent(D0, ["sh600001", "sh600002"])
    m = FakeMarket({D0: {"sh600001": (10.0, 10.0), "sh600002": (10.0, 10.0)},
                    D1: {"sh600001": (5.2, 5.2), "sh600002": (10.2, 10.2)}},
                   hfq={D1: {"sh600001": 2.0}})  # 600001 10 送 10
    s002.settle(D0, m, print)
    before = Account(s002.ACCOUNT).positions["sh600001"]["shares"]
    intent(D1, ["sh600001", "sh600002"])
    s002.settle(D1, m, print)
    a = Account(s002.ACCOUNT)
    assert a.positions["sh600001"]["shares"] == pytest.approx(before * 2)
    t = pd.read_csv(config.home() / "accounts" / s002.ACCOUNT / "trades.csv")
    assert not ((t.date == D1) & t.side.isin(["buy", "sell"])).any()  # 偏离 <25%，不调


def test_s003_profile_isolated(monkeypatch):
    monkeypatch.setattr(s002, "PROFILE", s002.PROFILE)
    for k in ("ACCOUNT", "STRATEGY", "RULE", "N"):
        monkeypatch.setattr(s002, k, getattr(s002, k))
    s002.use("s003")
    assert (s002.ACCOUNT, s002.RULE, s002.N) == ("challenger/s003", "R5", 10)
    assert s002.data_dir().name == "s003" and s002.cache_dir().parent.name == "s002"  # 日线缓存共用
    Account.create(s002.ACCOUNT, s002.STRATEGY, s002.INIT_CASH, D0)
    intent(D0, [f"sh6000{i:02d}" for i in range(10)])
    m = FakeMarket({D0: {f"sh6000{i:02d}": (10.0, 10.0) for i in range(10)}})
    s002.settle(D0, m, print)
    a = Account("challenger/s003")
    assert len(a.positions) == 10 and all(p["shares"] == 900 for p in a.positions.values())  # 1 万 / 10.01 → 900 股
    assert not Account("challenger/s002").positions
