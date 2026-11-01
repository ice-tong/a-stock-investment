"""选股研究的时点财务数据：东方财富业绩报表（stock_yjbb_em），按法定截止日次日生效。

产出（gitignored）：data/cache/full/yjbb.pkl（原始，按报告期）、data/cache/full/fin_pit.pkl（code, avail, eps_ttm, np_ttm, cfps_ttm）、
data/cache/full/sue_pit.pkl（code, period, avail, sue；可用日 = 实际披露日次日，第四轮预注册）
用法：.venv/bin/python tools/select_fin.py fetch | build | sue
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "cache" / "full"
SCHED = ROOT / "data" / "cache" / "earnings_sched_2019_2026.pkl"
QLABEL = {"一季": "0331", "半年报": "0630", "三季": "0930", "年报": "1231"}
AVAIL = {"0331": "-05-01", "0630": "-09-01", "0930": "-11-01", "1231": None}  # 年报：次年 05-01


def periods():
    return [f"{y}{q}" for y in range(2018, 2027) for q in ("0331", "0630", "0930", "1231") if f"{y}{q}" <= "20260930"]


def fetch():
    import akshare as ak

    frames = []
    for p in periods():
        for a in range(3):
            try:
                d = ak.stock_yjbb_em(date=p)
                break
            except Exception:
                time.sleep(3 * (a + 1))
        else:
            print("failed", p)
            continue
        d = d[["股票代码", "每股收益", "净利润-净利润", "每股经营现金流量", "每股净资产", "营业总收入-营业总收入"]].copy()
        d.columns = ["code6", "eps", "np", "cfps", "bps", "rev"]  # rev 为第五轮 X1 新增
        d["period"] = p
        frames.append(d)
        print(p, len(d), flush=True)
    pd.concat(frames, ignore_index=True).to_pickle(OUT / "yjbb.pkl")


def build():
    f = fin_pit_table(pd.read_pickle(OUT / "yjbb.pkl"))
    f.to_pickle(OUT / "fin_pit.pkl")
    print(f.shape, f.avail.min(), f.avail.max())
    build_sue()


def fin_pit_table(y: pd.DataFrame) -> pd.DataFrame:
    """按报告期的 yjbb 原表 → 时点 TTM 表（code, avail, eps_ttm, np_ttm, cfps_ttm）。"""
    y = y.drop_duplicates(["code6", "period"]).copy()
    if "rev" not in y:  # 第四轮及更早的缓存没有营收
        y["rev"] = float("nan")
    for c in ("eps", "np", "cfps", "bps", "rev"):
        y[c] = pd.to_numeric(y[c], errors="coerce")
    y["year"], y["q"] = y.period.str[:4].astype(int), y.period.str[4:]
    key = y.set_index(["code6", "year", "q"])
    rows = []
    for (code6, year, q), r in key.iterrows():
        if q == "1231":
            ttm = r[["eps", "np", "cfps", "rev"]]
            avail = f"{year + 1}-05-01"
        else:
            try:
                ann, prev = key.loc[(code6, year - 1, "1231")], key.loc[(code6, year - 1, q)]
            except KeyError:
                continue
            k = ["eps", "np", "cfps", "rev"]
            ttm = ann[k] + r[k] - prev[k]
            avail = f"{year}{AVAIL[q]}"
        rows.append({"code6": code6, "period": f"{year}{q}", "avail": avail, "eps_ttm": ttm["eps"],
                     "np_ttm": ttm["np"], "cfps_ttm": ttm["cfps"],
                     "rev_ttm": ttm["rev"], "bps": r["bps"]})
    f = pd.DataFrame(rows)
    pfx = {"6": "sh", "0": "sz", "3": "sz"}
    f["code"] = f.code6.str[0].map(pfx) + f.code6
    return f.dropna(subset=["code"]).sort_values(["code", "avail", "period"])


def sue_table(y: pd.DataFrame, sched: pd.DataFrame) -> pd.DataFrame:
    """SUE = (单季 EPS − 去年同季单季 EPS) / 前 8 个季度该差值的标准差（至少 4 个）。

    y：code6, period(YYYYMMDD), eps（年初至今累计）；sched：股票代码, period（如「2020一季」）, 实际披露。
    可用日 = 实际披露日次日；缺失时退回法定截止日次日（AVAIL）。
    """
    y = y.drop_duplicates(["code6", "period"]).copy()
    y["eps"] = pd.to_numeric(y["eps"], errors="coerce")
    y["year"], y["q"] = y.period.str[:4].astype(int), y.period.str[4:]
    cum = y.set_index(["code6", "year", "q"])["eps"]
    prevq = {"0630": "0331", "0930": "0630", "1231": "0930"}
    sq = {}
    for (code6, year, q), v in cum.items():
        if q == "0331":
            sq[(code6, year, q)] = v
        elif (code6, year, prevq[q]) in cum.index:
            sq[(code6, year, q)] = v - cum[(code6, year, prevq[q])]
    sq = pd.Series(sq, dtype=float)
    sq.index.names = ["code6", "year", "q"]
    d = sq.reset_index(name="sq")
    last = d.set_index(["code6", "year", "q"])["sq"]
    d["dq"] = [r.sq - last.get((r.code6, r.year - 1, r.q), float("nan")) for r in d.itertuples()]
    d = d.dropna(subset=["dq"]).sort_values(["code6", "year", "q"])
    sd = d.groupby("code6")["dq"].transform(lambda s: s.shift(1).rolling(8, min_periods=4).std())
    d["sue"] = d["dq"] / sd.where(sd > 0)
    d["period"] = d.year.astype(str) + d.q

    s = sched.dropna(subset=["实际披露"]).copy()
    s["period"] = s.period.str[:4] + s.period.str[4:].map(QLABEL)
    s["avail"] = (pd.to_datetime(s["实际披露"]) + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
    s = s.dropna(subset=["period"]).drop_duplicates(["股票代码", "period"], keep="last")
    d = d.merge(s[["股票代码", "period", "avail"]].rename(columns={"股票代码": "code6"}), on=["code6", "period"], how="left")
    legal = [f"{int(p[:4]) + 1}-05-01" if p[4:] == "1231" else f"{p[:4]}{AVAIL[p[4:]]}" for p in d.period]
    d["avail"] = d["avail"].fillna(pd.Series(legal, index=d.index))
    pfx = {"6": "sh", "0": "sz", "3": "sz"}
    d["code"] = d.code6.str[0].map(pfx) + d.code6
    return d.dropna(subset=["code", "sue"])[["code", "code6", "period", "avail", "sue"]].sort_values(["code", "avail", "period"])


def build_sue():
    d = sue_table(pd.read_pickle(OUT / "yjbb.pkl"), pd.read_pickle(SCHED))
    d.to_pickle(OUT / "sue_pit.pkl")
    print("sue", d.shape, d.avail.min(), d.avail.max())


if __name__ == "__main__":
    {"fetch": fetch, "build": build, "sue": build_sue}[sys.argv[1]]()
