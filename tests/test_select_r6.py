"""选股第六轮信号的单元测试（不联网）：.venv/bin/python -m pytest tests/test_select_r6.py -q"""
import numpy as np
import pandas as pd

from tools import select_bt as bt
from tools import select_r6 as r6

A, B = "sh600001", "sz000002"


def test_y1_holders_point_in_time_and_late_records():
    g = pd.DataFrame({
        "code6": ["600001"] * 4 + ["000002"] * 2,
        "n_now": [1000, 900, 800, 700, 500, 400],
        "n_prev": [0] * 6,
        "end_now": ["2024-03-31", "2024-06-30", "2024-08-15", "2024-09-30", "2024-03-31", "2024-06-30"],
        "end_prev": [""] * 6,
        "announce": ["2024-04-20", "2024-07-20", "2024-08-20", "2024-10-20", "2024-04-20", "2025-01-30"],
    })
    t = r6.gdhs_table(g)
    assert set(t.end[t.code == A]) == {"2024-06-30", "2024-09-30"}       # 08-15 非季度末不用；首期无上期
    assert t.y1[(t.code == A) & (t.end == "2024-09-30")].iat[0] == -(700 / 900 - 1)  # 上期取上一个季度末（跳过 08-15）
    assert B not in set(t.code)                                            # 06-30 那期公告晚于截止日 120 日，作废
    idx = ["2024-07-19", "2024-07-22", "2024-12-31"]
    w = r6.gdhs_wide(idx, [A, B], t)
    assert np.isnan(w.at["2024-07-19", A])                                 # 公告前不可见
    assert w.at["2024-07-22", A] == -(900 / 1000 - 1)
    assert w.at["2024-12-31", A] == -(700 / 900 - 1)


def test_y2_site_visits_window_and_zero_fill():
    j = r6.jgdy_table(pd.DataFrame({"code6": ["600001", "600001"], "n_inst": [9, 20],
                                    "visit": ["2024-01-02", "2024-05-01"], "announce": ["2024-01-05", "2024-05-06"]}))
    w = r6.jgdy_wide(["2024-01-04", "2024-01-05", "2024-04-04", "2024-04-05", "2024-05-06"], [A, B], j)
    assert w.at["2024-01-04", A] == 0 and w.at["2024-01-05", A] == np.log1p(9)
    assert w.at["2024-04-04", A] == np.log1p(9) and w.at["2024-04-05", A] == 0   # [d−90, d]：01-05 到 04-04 正好 90 日
    assert w.at["2024-05-06", A] == np.log1p(20) and (w[B] == 0).all()           # 没有调研记 0，不是 NaN


def test_y3_forecast_visible_before_actual_then_replaced():
    rows = []
    for yr in range(2019, 2025):
        for q, v in (("0331", 1.0), ("0630", 2.0), ("0930", 3.0), ("1231", 4.0)):
            rows.append(["600001", f"{yr}{q}", v * (1 + 0.1 * (yr - 2019) + 0.01 * (hash(q) % 3))])
    y = pd.DataFrame(rows, columns=["code6", "period", "np"])
    sched = pd.DataFrame(columns=["股票代码", "实际披露", "period"])  # 无日程：正式数按法定截止日次日（年报次年 05-01）
    fc = pd.DataFrame({"code6": ["600001"], "metric": [r6.NP_METRIC], "value": [9.0], "prev_year": [0.0],
                       "announce": ["2025-01-20"], "period": ["20241231"]})
    base = r6.np_sue_table(y, sched)
    t = r6.np_sue_table(y, sched, fc)
    f = t[t.src == "forecast"]
    assert len(f) == 1 and f.avail.iat[0] == "2025-01-20"
    w = r6.sue_latest_wide(["2025-01-17", "2025-01-20", "2025-05-01"], [A], t)
    wb = r6.sue_latest_wide(["2025-01-17", "2025-01-20", "2025-05-01"], [A], base)
    assert w.at["2025-01-17", A] == wb.at["2025-01-17", A]               # 预告前与正式版一致（2024Q3）
    assert w.at["2025-01-20", A] == f.sue.iat[0] != wb.at["2025-01-20", A]  # 预告日起用预告数
    assert w.at["2025-05-01", A] == wb.at["2025-05-01", A]               # 正式数可用后覆盖预告


def test_y4_liquidity_threshold():
    idx = pd.Index([f"2024-01-{d:02d}" for d in range(1, 31)])
    one = lambda v: pd.DataFrame(v, index=idx, columns=[A, B])
    w = {"close": one(10.0), "amount": one([[3e7, 6e7]] * 30), "volume": one(3e6), "turnover": one(0.01)}
    w["volume"][B] = 6e6
    fin = pd.DataFrame({"code": [A], "avail": ["2023-05-01"], "eps_ttm": [1.0], "np_ttm": [1.0], "cfps_ttm": [1.0]})
    sue = pd.DataFrame({"code": [A], "avail": ["2023-05-01"], "sue": [1.0]})
    e5, _ = bt.features(w, fin, sue)
    e2, _ = bt.features(w, fin, sue, min_amt=bt.MIN_AMT20_Y4)
    assert not e5.iloc[-1][A] and e5.iloc[-1][B]   # 3000 万 < 5000 万
    assert e2.iloc[-1][A] and e2.iloc[-1][B]


def test_r6_assembled_from_passed_signals(monkeypatch):
    monkeypatch.setattr(bt, "R6_PARTS", {"y1", "y4"})
    assert bt.yparts("R6") == {"y1", "y4"}
    assert bt.feature_args("R6") == {"r6_signals": True, "min_amt": bt.MIN_AMT20_Y4}
    assert bt.feature_args("Y2") == {"r6_signals": True, "min_amt": bt.MIN_AMT20}
    assert bt.feature_args("R5") == {}
