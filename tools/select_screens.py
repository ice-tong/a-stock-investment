"""第五轮排雷筛选（预注册 docs/research/选股策略_预注册_第五轮_2026-11-01.md §2）：X1 退市风险、X2 解禁与减持、X3 增持与回购。

所有函数只用信号日 d 当时已知的数据；输入是原始事件表（tools/select_events.py、tools/select_fin.py 产出），
输出按 code（sh/sz 前缀）索引的 Series。纯函数，便于单测。
"""
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path(__file__).resolve().parents[1] / "data" / "cache" / "full"
DIV_RULE_FROM = "2025-05-01"
UNLOCK_DAYS, UNLOCK_PCT = 31, 0.05
SELL_DAYS, SELL_PCT = 60, 1.0      # 占流通股 %，接口单位为百分数
BUY_DAYS, BUY_PCT, REPO_PCT = 90, 0.5, 0.01
BONUS = 0.10


def _code(code6: pd.Series) -> pd.Series:
    c = code6.astype(str).str.zfill(6)
    return c.str[0].map({"6": "sh", "0": "sz", "3": "sz"}) + c


def growth_board(codes) -> pd.Series:
    """创业板 / 科创板（营收门槛 1 亿、分红门槛 3000 万）。"""
    idx = pd.Index(codes)
    return pd.Series(idx.str[2:].str.startswith(("300", "301", "688", "689")), index=idx)


@lru_cache(maxsize=1)
def tables():
    t = {}
    fin = pd.read_pickle(CACHE / "fin_pit.pkl")
    t["fin"] = fin.sort_values(["code", "avail", "period"])
    y = pd.read_pickle(CACHE / "yjbb.pkl").drop_duplicates(["code6", "period"])
    ann = y[y.period.str.endswith("1231")].copy()
    ann["code"], ann["year"] = _code(ann.code6), ann.period.str[:4].astype(int)
    ann["np"] = pd.to_numeric(ann["np"], errors="coerce")
    t["annual_np"] = ann.dropna(subset=["code"]).set_index(["code", "year"])["np"]
    fh = pd.read_pickle(CACHE / "fhps.pkl")
    fh["code"], fh["year"] = _code(fh.code6), fh.period.str[:4].astype(int)
    fh["cash"] = pd.to_numeric(fh.cash_per10, errors="coerce").fillna(0) / 10 * pd.to_numeric(fh.shares, errors="coerce")
    fh["announce"] = pd.to_datetime(fh.announce, errors="coerce").dt.strftime("%Y-%m-%d")
    t["fhps"] = fh.dropna(subset=["code", "announce"])
    un = pd.read_pickle(CACHE / "unlock.pkl")
    un["code"], un["date"] = _code(un.code6), pd.to_datetime(un.date).dt.strftime("%Y-%m-%d")
    un["qty"] = pd.to_numeric(un.qty, errors="coerce")
    t["unlock"] = un.dropna(subset=["code"])
    gg = pd.read_pickle(CACHE / "ggcg.pkl")
    gg["code"], gg["announce"] = _code(gg.code6), pd.to_datetime(gg.announce, errors="coerce").dt.strftime("%Y-%m-%d")
    gg["pct_float"] = pd.to_numeric(gg.pct_float, errors="coerce")
    t["ggcg"] = gg.dropna(subset=["code", "announce", "pct_float"])
    rp = pd.read_pickle(CACHE / "repurchase.pkl")
    rp["code"], rp["start"] = _code(rp.code6), pd.to_datetime(rp.start, errors="coerce").dt.strftime("%Y-%m-%d")
    rp["amt_low"] = pd.to_numeric(rp.amt_low, errors="coerce")
    t["repo"] = rp.dropna(subset=["code", "start", "amt_low"])
    return t


def _shift(d: str, days: int) -> str:
    return (pd.Timestamp(d) + pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def x1_bad(d: str, codes, fin: pd.DataFrame, annual_np: pd.Series, fhps: pd.DataFrame) -> pd.Series:
    """退市风险：亏损且营收低于门槛；净资产为负；（d ≥ 2025-05-01）三年分红不达标。"""
    idx = pd.Index(codes)
    g = growth_board(idx)
    last = fin[(fin.avail <= d) & fin.code.isin(idx)].groupby("code").last().reindex(idx)
    rev_thr = np.where(g, 1e8, 3e8)
    bad = ((last.np_ttm < 0) & (last.rev_ttm < rev_thr)).fillna(False) | (last.bps < 0).fillna(False)
    if d >= DIV_RULE_FROM:
        ly = int(d[:4]) - 1 if d[5:] >= "05-01" else int(d[:4]) - 2  # 最近一个已过年报截止日的会计年度
        years = [ly - 2, ly - 1, ly]
        npm = pd.DataFrame({y: annual_np.xs(y, level="year").reindex(idx) if y in annual_np.index.get_level_values("year") else np.nan
                            for y in years})
        fh = fhps[(fhps.announce <= d) & fhps.year.isin(years) & fhps.code.isin(idx)]
        div = fh.groupby("code").cash.sum().reindex(idx).fillna(0)
        lim = np.where(g, 3e7, 5e7)
        rule = (div < 0.3 * npm.mean(axis=1)) & (div < lim) & (npm[ly] > 0)
        bad |= rule.fillna(False)
    return pd.Series(bad.values, index=idx)


def x2_bad(d: str, codes, d31: str, px_raw: pd.Series, fmcap: pd.Series, unlock: pd.DataFrame, ggcg: pd.DataFrame) -> pd.Series:
    """解禁 (d, d31] 市值 ≥ 流通市值 5%；或 [d−60 日, d] 公告减持合计 ≥ 流通股 1%。"""
    idx = pd.Index(codes)
    u = unlock[(unlock.date > d) & (unlock.date <= d31) & unlock.code.isin(idx)]
    uq = u.groupby("code").qty.sum().reindex(idx).fillna(0)
    big_unlock = (uq * px_raw.reindex(idx) / fmcap.reindex(idx)) >= UNLOCK_PCT
    s = ggcg[(ggcg.side == "减持") & (ggcg.announce >= _shift(d, -SELL_DAYS)) & (ggcg.announce <= d) & ggcg.code.isin(idx)]
    sold = s.groupby("code").pct_float.sum().reindex(idx).fillna(0) >= SELL_PCT
    return (big_unlock.fillna(False) | sold)


def x3_bonus(d: str, codes, fmcap: pd.Series, ggcg: pd.DataFrame, repo: pd.DataFrame) -> pd.Series:
    """[d−90 日, d] 内计划回购下限 ≥ 流通市值 1%，或公告增持合计 ≥ 流通股 0.5%。"""
    idx = pd.Index(codes)
    r = repo[(repo.start >= _shift(d, -BUY_DAYS)) & (repo.start <= d) & repo.code.isin(idx)]
    big_repo = (r.groupby("code").amt_low.sum().reindex(idx).fillna(0) / fmcap.reindex(idx)) >= REPO_PCT
    b = ggcg[(ggcg.side == "增持") & (ggcg.announce >= _shift(d, -BUY_DAYS)) & (ggcg.announce <= d) & ggcg.code.isin(idx)]
    bought = b.groupby("code").pct_float.sum().reindex(idx).fillna(0) >= BUY_PCT
    return (big_repo.fillna(False) | bought)
