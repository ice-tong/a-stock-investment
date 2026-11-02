"""第五轮（预注册 docs/research/选股策略_预注册_第五轮_2026-11-01.md）排雷筛选的原始数据：分红、解禁、股东增减持、回购、退市股日线；
第六轮（docs/research/选股策略_预注册_第六轮_2026-11-02.md）新增：股东户数、机构调研、业绩预告。

产出（gitignored，data/cache/full/）：fhps.pkl、unlock.pkl、ggcg.pkl、repurchase.pkl、delist_list.pkl、delist_daily/<code>.csv、
gdhs.pkl、jgdy.pkl、yjyg.pkl
用法：.venv/bin/python tools/select_events.py fetch [--only fhps,unlock,ggcg,repurchase,delist,gdhs,jgdy,yjyg]
"""
import argparse
import os
import time
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "cache" / "full"


def _retry(fn, n=3):
    for a in range(n):
        try:
            return fn()
        except Exception as e:
            err = e
            time.sleep(3 * (a + 1))
    raise err


def fetch_fhps():
    import akshare as ak
    frames = []
    for y in range(2016, 2026):
        for q in ("0630", "1231"):
            d = _retry(lambda: ak.stock_fhps_em(date=f"{y}{q}"))
            d = d[["代码", "现金分红-现金分红比例", "总股本", "预案公告日"]].copy()
            d.columns = ["code6", "cash_per10", "shares", "announce"]
            d["period"] = f"{y}{q}"
            frames.append(d)
            print("fhps", y, q, len(d), flush=True)
    pd.concat(frames, ignore_index=True).to_pickle(OUT / "fhps.pkl")


def fetch_unlock():
    import akshare as ak
    frames = []
    for m in pd.date_range("2020-01-01", "2026-12-01", freq="MS"):
        e = (m + pd.offsets.MonthEnd(0)).strftime("%Y%m%d")
        d = _retry(lambda: ak.stock_restricted_release_detail_em(start_date=m.strftime("%Y%m%d"), end_date=e))
        frames.append(d[["股票代码", "解禁时间", "解禁数量"]].rename(columns={"股票代码": "code6", "解禁时间": "date", "解禁数量": "qty"}))
        print("unlock", m.strftime("%Y-%m"), len(d), flush=True)
    pd.concat(frames, ignore_index=True).to_pickle(OUT / "unlock.pkl")


def fetch_ggcg():
    import akshare as ak
    d = _retry(lambda: ak.stock_ggcg_em(symbol="全部"))
    d = d[["代码", "持股变动信息-增减", "持股变动信息-占流通股比例", "公告日"]].copy()
    d.columns = ["code6", "side", "pct_float", "announce"]
    d.to_pickle(OUT / "ggcg.pkl")
    print("ggcg", d.shape, d.announce.min(), d.announce.max())


def fetch_repurchase():
    import akshare as ak
    d = _retry(lambda: ak.stock_repurchase_em())
    d = d[["股票代码", "计划回购金额区间-下限", "回购起始时间"]].copy()
    d.columns = ["code6", "amt_low", "start"]
    d.to_pickle(OUT / "repurchase.pkl")
    print("repurchase", d.shape)


def fetch_delist():
    """2020 年后终止上市的沪深 A 股：名单 + 腾讯前复权日线（成交量单位为手，统一换成股）。"""
    import akshare as ak
    sz = ak.stock_info_sz_delist(symbol="终止上市公司").rename(columns={"证券代码": "code6", "终止上市日期": "delist"})
    sh = ak.stock_info_sh_delist(symbol="全部").rename(columns={"公司代码": "code6", "暂停上市日期": "delist"})
    lst = pd.concat([sz.assign(code="sz" + sz.code6.astype(str)), sh.assign(code="sh" + sh.code6.astype(str))])
    lst["delist"] = pd.to_datetime(lst["delist"], errors="coerce").dt.strftime("%Y-%m-%d")
    lst = lst[(lst.delist >= "2020-01-01") & lst.code.str[2:].str.startswith(("0", "3", "6"))][["code", "delist"]]
    lst.to_pickle(OUT / "delist_list.pkl")
    dd = OUT / "delist_daily"
    dd.mkdir(exist_ok=True)
    ok = 0
    for c in lst.code:
        p = dd / f"{c}.csv"
        if p.exists():
            ok += 1
            continue
        try:
            d = _retry(lambda: ak.stock_zh_a_hist_tx(symbol=c, start_date="20200101", end_date="20261031", adjust="qfq"))
        except Exception:
            print("delist failed", c, flush=True)
            continue
        if d is None or not len(d):
            continue
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        d["volume"] = d["volume"] * 100
        d.to_csv(p, index=False)
        ok += 1
    print("delist", len(lst), "fetched", ok)


def _qends(first="2019-03-31", last="2026-09-30"):
    return [d.strftime("%Y%m%d") for d in pd.date_range(first, last, freq="QE")]


def fetch_gdhs():
    """季度末截止日的股东户数（本次 / 上次 / 公告日）。"""
    import akshare as ak
    frames = []
    for q in _qends("2018-12-31"):
        d = _retry(lambda: ak.stock_zh_a_gdhs(symbol=q))
        d = d[["代码", "股东户数-本次", "股东户数-上次", "股东户数统计截止日-本次", "股东户数统计截止日-上次", "公告日期"]].copy()
        d.columns = ["code6", "n_now", "n_prev", "end_now", "end_prev", "announce"]
        frames.append(d)
        print("gdhs", q, len(d), flush=True)
    pd.concat(frames, ignore_index=True).to_pickle(OUT / "gdhs.pkl")


def fetch_jgdy():
    """机构调研：东方财富 RPT_ORG_SURVEY 的历史明细（akshare 的统计接口只回近一年）。

    NUMBERNEW="1" 每次调研只取一行，SUM = 接待机构数量。接口单页上限约 50 行，按月分页拉，断点续拉（data/cache/full/jgdy_parts/）。
    """
    import requests
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    parts = OUT / "jgdy_parts"
    parts.mkdir(exist_ok=True)

    def page(a, b, n):
        p = {"sortColumns": "NOTICE_DATE,SECURITY_CODE,RECEIVE_START_DATE", "sortTypes": "1,1,1", "pageSize": "50", "pageNumber": str(n),
             "reportName": "RPT_ORG_SURVEY", "columns": "SECURITY_CODE,NOTICE_DATE,RECEIVE_START_DATE,SUM,NUMBERNEW",
             "source": "WEB", "client": "WEB", "filter": f"(NUMBERNEW=\"1\")(NOTICE_DATE>='{a}')(NOTICE_DATE<='{b}')"}
        for k in range(8):
            j = requests.get(url, params=p, timeout=60).json()
            if j.get("result"):
                return j["result"]
            if j.get("message") == "返回数据为空":
                return {"pages": 0, "data": []}
            time.sleep(2 * (k + 1))
        raise RuntimeError(f"jgdy {a} p{n}: {j.get('message')}")

    for m in pd.date_range("2019-01-01", "2026-11-01", freq="MS"):
        f = parts / f"{m:%Y%m}.pkl"
        if f.exists():
            continue
        rows = []
        # 深翻页（约 50 页以后）服务器会报「繁忙」：按周取，单周超过 40 页再按天取
        for w0 in pd.date_range(m, m + pd.offsets.MonthEnd(0), freq="7D"):
            w1 = min(w0 + pd.Timedelta(days=6), m + pd.offsets.MonthEnd(0))
            r = page(f"{w0:%Y-%m-%d}", f"{w1:%Y-%m-%d}", 1)
            wins = [(w0, w1, r)] if (r.get("pages") or 0) <= 40 else \
                [(x, x, page(f"{x:%Y-%m-%d}", f"{x:%Y-%m-%d}", 1)) for x in pd.date_range(w0, w1)]
            for x0, x1, r in wins:
                rows += list(r["data"] or [])
                for n in range(2, (r.get("pages") or 0) + 1):
                    rows += page(f"{x0:%Y-%m-%d}", f"{x1:%Y-%m-%d}", n)["data"] or []
                    time.sleep(0.2)
        pd.DataFrame(rows).to_pickle(f)
        print("jgdy", m.strftime("%Y-%m"), len(rows), flush=True)
    d = pd.concat([pd.read_pickle(f) for f in sorted(parts.glob("*.pkl"))], ignore_index=True)
    d = d.rename(columns={"SECURITY_CODE": "code6", "NOTICE_DATE": "announce", "RECEIVE_START_DATE": "visit", "SUM": "n_inst"})
    d["announce"] = d["announce"].astype(str).str[:10]
    d = d.drop_duplicates(["code6", "announce", "visit"])[["code6", "n_inst", "visit", "announce"]]
    d.to_pickle(OUT / "jgdy.pkl")
    print("jgdy", d.shape, d.announce.min(), d.announce.max())
    print(d.groupby(d.announce.str[:4]).size().to_string())


def fetch_yjyg():
    """业绩预告（按报告期）。"""
    import akshare as ak
    frames = []
    for q in _qends():
        try:
            d = _retry(lambda: ak.stock_yjyg_em(date=q))
        except Exception as e:
            print("yjyg failed", q, e, flush=True)
            continue
        d = d[["股票代码", "预测指标", "预测数值", "上年同期值", "公告日期"]].copy()
        d.columns = ["code6", "metric", "value", "prev_year", "announce"]
        d["period"] = q
        frames.append(d)
        print("yjyg", q, len(d), flush=True)
    pd.concat(frames, ignore_index=True).to_pickle(OUT / "yjyg.pkl")


JOBS = {"fhps": fetch_fhps, "unlock": fetch_unlock, "ggcg": fetch_ggcg, "repurchase": fetch_repurchase, "delist": fetch_delist,
        "gdhs": fetch_gdhs, "jgdy": fetch_jgdy, "yjyg": fetch_yjyg}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch"])
    ap.add_argument("--only", default=",".join(JOBS))
    a = ap.parse_args()
    for k in a.only.split(","):
        try:
            JOBS[k]()
        except Exception as e:
            print("JOB FAILED", k, type(e).__name__, e, flush=True)
