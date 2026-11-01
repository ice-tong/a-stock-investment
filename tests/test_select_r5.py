"""选股第五轮排雷筛选与 R5 缓冲的单元测试（不联网）：.venv/bin/python -m pytest tests/test_select_r5.py -q"""
import numpy as np
import pandas as pd

from tools import select_bt as bt
from tools import select_screens as scr

MAIN, GEM = "sh600001", "sz300001"


def fin_rows(rows):
    return pd.DataFrame(rows, columns=["code", "avail", "period", "np_ttm", "rev_ttm", "bps"])


def test_x1_loss_and_low_revenue_by_board_and_timing():
    fin = fin_rows([[MAIN, "2024-05-01", "20231231", -1e7, 2e8, 3.0],   # 主板：亏损 + 营收 < 3 亿
                    [GEM, "2024-05-01", "20231231", -1e7, 2e8, 3.0],    # 创业板：营收 2 亿 > 1 亿，不触发
                    [MAIN, "2024-09-01", "20240630", 1e7, 2e8, 3.0]])   # 半年报转盈，9 月起不再触发
    empty_np = pd.Series(dtype=float, index=pd.MultiIndex.from_tuples([], names=["code", "year"]))
    empty_fh = pd.DataFrame(columns=["code", "year", "cash", "announce"])
    a = scr.x1_bad("2024-06-28", [MAIN, GEM], fin, empty_np, empty_fh)
    assert a[MAIN] and not a[GEM]
    assert not scr.x1_bad("2024-04-30", [MAIN], fin, empty_np, empty_fh)[MAIN]  # 年报可用日之前看不到
    assert not scr.x1_bad("2024-09-30", [MAIN], fin, empty_np, empty_fh)[MAIN]


def test_x1_dividend_rule_only_from_2025_05():
    fin = fin_rows([[MAIN, "2025-05-01", "20241231", 1e8, 1e9, 3.0]])
    np_ = pd.Series([1e8, 1e8, 1e8], index=pd.MultiIndex.from_tuples([(MAIN, 2022), (MAIN, 2023), (MAIN, 2024)],
                                                                     names=["code", "year"]))
    fh = pd.DataFrame({"code": [MAIN], "year": [2024], "cash": [1e6], "announce": ["2025-04-20"]})
    assert scr.x1_bad("2025-06-30", [MAIN], fin, np_, fh)[MAIN]           # 三年分红 100 万 < 30%×1 亿 且 < 5000 万
    assert not scr.x1_bad("2025-04-30", [MAIN], fin, np_, fh)[MAIN]       # 新规生效前不检查
    fh2 = fh.assign(cash=[6e7])
    assert not scr.x1_bad("2025-06-30", [MAIN], fin, np_, fh2)[MAIN]      # 分红 6000 万 ≥ 5000 万


def test_x2_unlock_window_and_reduction():
    unlock = pd.DataFrame({"code": [MAIN, MAIN], "date": ["2024-07-10", "2024-09-30"], "qty": [1e7, 1e9]})
    gg = pd.DataFrame({"code": [GEM], "side": ["减持"], "pct_float": [1.2], "announce": ["2024-05-20"]})
    px, fm = pd.Series({MAIN: 10.0, GEM: 10.0}), pd.Series({MAIN: 1e9, GEM: 1e9})
    out = scr.x2_bad("2024-06-28", [MAIN, GEM], "2024-08-12", px, fm, unlock, gg)
    assert out[MAIN]   # 1000 万股 × 10 元 = 1 亿 = 流通市值 10%，落在窗口内（9 月那笔在窗口外）
    assert out[GEM]    # 60 日内减持 1.2% ≥ 1%
    assert not scr.x2_bad("2024-08-20", [GEM], "2024-10-01", px, fm, unlock.iloc[:0], gg)[GEM]  # 超过 60 日


def test_x3_repurchase_and_insider_buy():
    gg = pd.DataFrame({"code": [GEM], "side": ["增持"], "pct_float": [0.6], "announce": ["2024-06-01"]})
    repo = pd.DataFrame({"code": [MAIN], "start": ["2024-05-01"], "amt_low": [2e7]})
    fm = pd.Series({MAIN: 1e9, GEM: 1e9})
    out = scr.x3_bonus("2024-06-28", [MAIN, GEM], fm, gg, repo)
    assert out[MAIN] and out[GEM]
    assert not scr.x3_bonus("2024-04-30", [MAIN, GEM], fm, gg, repo).any()  # 公告之前看不到


def test_r5_buffer_keeps_held_within_top30(monkeypatch):
    codes = [f"sz0000{i:02d}" for i in range(60)]
    d = "2024-06-28"
    z = pd.Series(np.linspace(1, 0, 60), index=codes)
    f = {k: pd.DataFrame([z.values], index=[d], columns=codes) for k in ("sue", "ep", "acc")}
    f["at"] = -f["sue"]
    f["mcap"] = pd.DataFrame([np.ones(60)], index=[d], columns=codes)  # 全部在最小 20% 内（分位数取等）
    monkeypatch.setattr(bt, "screens", lambda rule, d, idx, f: {"bad": pd.Series(False, index=idx),
                                                                 "bonus": pd.Series(0.0, index=idx)})
    fresh = bt.fpick("R5", f, d, pd.Index(codes), 10)
    assert fresh == codes[:10]
    held = {codes[25], codes[40]}  # 第 26 名留下（前 30 内），第 41 名换掉
    picks = bt.fpick("R5", f, d, pd.Index(codes), 10, held=held)
    assert codes[25] in picks and codes[40] not in picks and len(picks) == 10
