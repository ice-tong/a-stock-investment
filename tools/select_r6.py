"""第六轮信号（预注册 docs/research/选股策略_预注册_第六轮_2026-11-02.md §3）：Y1 股东户数下降、Y2 机构调研、Y3 业绩预告版 SUE。

原始表由 tools/select_events.py（gdhs / jgdy / yjyg）与 tools/select_fin.py（yjbb、披露日程）产出，缓存在 data/cache/full/。
*_table 是纯函数（便于单测）；*_wide 把时点表展开成「交易日 × 股票」宽表，只用信号日 d 当时已知的数据。
"""
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path(__file__).resolve().parents[1] / "data" / "cache" / "full"
SCHED = CACHE.parent / "earnings_sched_2019_2026.pkl"
GDHS_STALE, GDHS_LATE = 150, 120   # 截止日须在 [d−150, d]；公告日晚于截止日 + 120 日的补登记录不用
JGDY_DAYS = 90
NP_METRIC = "归属于上市公司股东的净利润"
QLABEL = {"一季": "0331", "半年报": "0630", "三季": "0930", "年报": "1231"}
PREVQ = {"0331": "1231", "0630": "0331", "0930": "0630", "1231": "0930"}


def _code(code6: pd.Series) -> pd.Series:
    c = code6.astype(str).str.zfill(6)
    return c.str[0].map({"6": "sh", "0": "sz", "3": "sz"}) + c


def _day(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.strftime("%Y-%m-%d")


# ---------------- Y1 股东户数 ----------------

def gdhs_table(g: pd.DataFrame) -> pd.DataFrame:
    """季度末户数记录 → (code, end, avail, y1)。y1 = −(本季户数 / 上季户数 − 1)，上季也必须是季度末记录。"""
    g = g.copy()
    g["code"], g["end"], g["avail"] = _code(g.code6), _day(g.end_now), _day(g.announce)
    g["n"] = pd.to_numeric(g.n_now, errors="coerce")
    g = g.dropna(subset=["code", "end", "avail", "n"])
    g = g[pd.to_datetime(g.end).dt.is_quarter_end & (g.n > 0)]
    late = pd.to_datetime(g.avail) - pd.to_datetime(g.end) > pd.Timedelta(days=GDHS_LATE)
    g = g[~late].sort_values(["code", "end", "avail"]).drop_duplicates(["code", "end"], keep="first")
    prev_end = (pd.to_datetime(g.end) - pd.offsets.QuarterEnd(1)).dt.strftime("%Y-%m-%d")
    prev = g.set_index(["code", "end"])["n"]
    g["n_prev"] = prev.reindex(list(zip(g.code, prev_end))).values
    g["y1"] = -(g.n / g.n_prev - 1)
    return g.dropna(subset=["y1"])[["code", "end", "avail", "y1"]]


def gdhs_wide(index, columns, t: pd.DataFrame) -> pd.DataFrame:
    """每个交易日可见的最新一期（按截止日最新、且已公告）；截止日早于 d − 150 日的视为过期（NaN）。"""
    t = t[t.code.isin(columns)].sort_values(["code", "avail", "end"])
    k = t.end.str.replace("-", "").astype(int)
    t = t[k >= k.groupby(t.code).cummax()]  # 后公告的旧季度不覆盖新季度
    t = t.drop_duplicates(["code", "avail"], keep="last")
    idx = pd.Index(index)
    allidx = idx.union(pd.Index(t.avail.unique()))
    val = t.pivot(index="avail", columns="code", values="y1").reindex(allidx).ffill().reindex(idx)
    end = t.pivot(index="avail", columns="code", values="end").reindex(allidx).ffill().reindex(idx)
    floor = pd.Series((pd.to_datetime(idx) - pd.Timedelta(days=GDHS_STALE)).strftime("%Y-%m-%d"), index=idx)
    fresh = end.ge(floor, axis=0)
    return val.where(fresh).reindex(columns=columns)


# ---------------- Y2 机构调研 ----------------

def jgdy_table(j: pd.DataFrame) -> pd.DataFrame:
    j = j.copy()
    j["code"], j["avail"] = _code(j.code6), _day(j.announce)
    j["n_inst"] = pd.to_numeric(j.n_inst, errors="coerce").fillna(0)
    return j.dropna(subset=["code", "avail"])[["code", "avail", "n_inst"]]


def jgdy_wide(index, columns, t: pd.DataFrame) -> pd.DataFrame:
    """y2 = log(1 + 公告日在 [d − 90 日, d] 内的接待机构数量合计)；没有调研记为 0。"""
    idx = pd.Index(index)
    days = pd.date_range(pd.Timestamp(idx[0]) - pd.Timedelta(days=JGDY_DAYS), idx[-1], freq="D").strftime("%Y-%m-%d")
    t = t[t.code.isin(columns)]
    daily = t.groupby(["avail", "code"]).n_inst.sum().unstack().reindex(index=days, columns=columns).fillna(0)
    roll = daily.rolling(JGDY_DAYS + 1, min_periods=1).sum()
    return np.log1p(roll.reindex(idx))


# ---------------- Y3 净利润口径 SUE / 业绩预告版 SUE ----------------

def _sched_avail(sched: pd.DataFrame) -> pd.DataFrame:
    s = sched.dropna(subset=["实际披露"]).copy()
    s["period"] = s.period.str[:4] + s.period.str[4:].map(QLABEL)
    s["avail"] = (pd.to_datetime(s["实际披露"]) + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
    s = s.dropna(subset=["period"]).drop_duplicates(["股票代码", "period"], keep="last")
    return s.rename(columns={"股票代码": "code6"})[["code6", "period", "avail"]]


def _legal(period: str) -> str:
    y, q = period[:4], period[4:]
    return {"0331": f"{y}-05-01", "0630": f"{y}-09-01", "0930": f"{y}-11-01", "1231": f"{int(y) + 1}-05-01"}[q]


def np_sue_table(y: pd.DataFrame, sched: pd.DataFrame, yjyg: pd.DataFrame | None = None) -> pd.DataFrame:
    """SUE（归母净利润口径）= (单季净利润 − 去年同季) / 前 8 个季度该差值的标准差（至少 4 个）。

    可用日 = 实际披露日次日（缺失时退回法定截止日次日），与第四轮 EPS 版相同。
    yjyg 给出时（Y3）：某期预告公告日早于正式数可用日，则在 [预告公告日, 正式数可用日) 用预告累计净利润代替该期累计数
    算单季差值，标准差用同一期正式记录的（只依赖更早的季度）。
    """
    y = y.drop_duplicates(["code6", "period"]).copy()
    y["np"] = pd.to_numeric(y["np"], errors="coerce")
    cum = y.dropna(subset=["np"]).set_index(["code6", "period"])["np"]

    def single(code6, period, cumv):
        q = period[4:]
        if q == "0331":
            return cumv
        pp = period[:4] + PREVQ[q]
        return cumv - cum.get((code6, pp), np.nan)

    rows = [(c, p, single(c, p, v)) for (c, p), v in cum.items()]
    d = pd.DataFrame(rows, columns=["code6", "period", "sq"]).dropna()
    sq = d.set_index(["code6", "period"])["sq"]
    d["dq"] = [r.sq - sq.get((r.code6, str(int(r.period[:4]) - 1) + r.period[4:]), np.nan) for r in d.itertuples()]
    d = d.dropna(subset=["dq"]).sort_values(["code6", "period"])
    d["sd"] = d.groupby("code6")["dq"].transform(lambda s: s.shift(1).rolling(8, min_periods=4).std())
    d["sue"] = d.dq / d.sd.where(d.sd > 0)
    d = d.merge(_sched_avail(sched), on=["code6", "period"], how="left")
    d["avail"] = d["avail"].fillna(pd.Series([_legal(p) for p in d.period], index=d.index))
    out = d[["code6", "period", "avail", "sue"]].assign(src="actual")
    if yjyg is not None:
        f = yjyg[yjyg.metric == NP_METRIC].copy()
        f["value"] = pd.to_numeric(f.value, errors="coerce")
        f["announce"] = _day(f.announce)
        f = f.dropna(subset=["value", "announce"]).sort_values("announce").drop_duplicates(["code6", "period"], keep="last")
        f = f.merge(d[["code6", "period", "avail", "sd"]], on=["code6", "period"], how="inner")
        f = f[f.announce < f.avail]
        f["sq"] = [single(r.code6, r.period, r.value) for r in f.itertuples()]
        f["dq"] = [r.sq - sq.get((r.code6, str(int(r.period[:4]) - 1) + r.period[4:]), np.nan) for r in f.itertuples()]
        f["sue"] = f.dq / f.sd.where(f.sd > 0)
        out = pd.concat([out, f.assign(avail=f.announce)[["code6", "period", "avail", "sue"]].assign(src="forecast")])
    out["code"] = _code(out.code6)
    return out.dropna(subset=["code", "sue"]).sort_values(["code", "avail", "period"])[["code", "period", "avail", "sue", "src"]]


def sue_latest_wide(index, columns, t: pd.DataFrame) -> pd.DataFrame:
    """每个交易日可见的最新报告期 SUE；同一报告期正式数可用后覆盖预告数，后到的旧报告期不覆盖新报告期。"""
    t = t[t.code.isin(columns)].copy()
    t["rank"] = t.src.map({"forecast": 0, "actual": 1})
    t = t.sort_values(["code", "avail", "period", "rank"])
    k = t.period.astype(int)
    t = t[k >= k.groupby(t.code).cummax()]
    t = t.drop_duplicates(["code", "avail"], keep="last")
    wide = t.pivot(index="avail", columns="code", values="sue")
    return wide.reindex(wide.index.union(pd.Index(index))).ffill().reindex(index).reindex(columns=columns)


@lru_cache(maxsize=1)
def tables():
    t = {"gdhs": gdhs_table(pd.read_pickle(CACHE / "gdhs.pkl"))}
    jp = CACHE / "jgdy.pkl"
    t["jgdy"] = jgdy_table(pd.read_pickle(jp)) if jp.exists() else None
    y, sched = pd.read_pickle(CACHE / "yjbb.pkl"), pd.read_pickle(SCHED)
    t["sue_np"] = np_sue_table(y, sched)
    t["sue_fc"] = np_sue_table(y, sched, pd.read_pickle(CACHE / "yjyg.pkl"))
    return t
