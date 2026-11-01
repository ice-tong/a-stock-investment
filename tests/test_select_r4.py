"""选股第四轮（SUE / 合成分）单元测试（不联网）：.venv/bin/python -m pytest tests/test_select_r4.py -q"""
import numpy as np
import pandas as pd

from tools import select_bt, select_fin


def _yjbb(code6, eps_by_period):
    return pd.DataFrame([{"code6": code6, "period": p, "eps": v} for p, v in eps_by_period.items()])


def _quarters(years):
    return [f"{y}{q}" for y in years for q in ("0331", "0630", "0930", "1231")]


def test_single_quarter_split_and_sue():
    # 每季单季 EPS = 0.1，最后一个季度单季 0.3；累计值按年重置
    eps = {}
    for p in _quarters(range(2018, 2022)):
        k = ("0331", "0630", "0930", "1231").index(p[4:]) + 1
        eps[p] = 0.1 * k + (0.001 * int(p[:4]) * (k == 1))  # 一季度带年份扰动，让 ΔEPS 有波动
    eps["20211231"] = eps["20210930"] + 0.3
    d = select_fin.sue_table(_yjbb("600001", eps), pd.DataFrame(columns=["股票代码", "period", "实际披露"]))
    last = d[d.period == "20211231"].iloc[0]
    # 单季 Q4 = 0.3，去年同季 0.1 → ΔEPS = 0.2 > 0；SUE 正且很大
    assert last.sue > 2
    # 一季度单季 = 累计值本身
    q1 = d[d.period == "20210331"]
    assert len(q1) == 1


def test_sue_available_day_after_actual_disclosure():
    eps = {p: 0.1 + 0.01 * i for i, p in enumerate(_quarters(range(2018, 2022)))}
    sched = pd.DataFrame([{"股票代码": "000002", "period": "2021三季", "实际披露": pd.Timestamp("2021-10-20")}])
    d = select_fin.sue_table(_yjbb("000002", eps), sched).set_index("period")
    assert d.at["20210930", "avail"] == "2021-10-21"  # 披露日次日
    assert d.at["20210630", "avail"] == "2021-09-01"  # 无实际披露日 → 法定截止日次日
    assert d.at["20201231", "avail"] == "2021-05-01"
    assert set(d.code) == {"sz000002"}


def test_composite_missing_handling():
    idx = ["a", "b", "c", "d"]
    sue = pd.Series([3.0, 1.0, np.nan, np.nan], index=idx)
    ep = pd.Series([0.1, 0.05, 0.08, np.nan], index=idx)
    at = pd.Series([0.5, 2.0, 1.0, 1.0], index=idx)
    acc = pd.Series([-0.1, 0.2, 0.0, np.nan], index=idx)
    z = select_bt.composite(sue, ep, at, acc)
    assert z["a"] > z["b"]          # 四项都更好
    assert pd.notna(z["c"])          # 缺一项：用剩余三项
    assert pd.isna(z["d"])           # 缺两项以上：剔除
