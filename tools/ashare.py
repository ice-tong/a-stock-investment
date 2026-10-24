#!/usr/bin/env python3
"""A 股数据取数工具（基于 AKShare），供各分析 Agent 通过 Bash 调用。

用法：.venv/bin/python tools/ashare.py <命令> [参数]
      .venv/bin/python tools/ashare.py -h            查看全部命令

每次输出都带「来源函数 + 拉取时间」，Agent 引用数据时必须一并标注。
AKShare 接口依赖第三方网站，偶尔会失效：多数命令内置了备用数据源，会自动切换；
都失败时可用 raw 命令调用其他函数，或改用 WebSearch/WebFetch 从巨潮资讯、交易所官网核实。
"""
import os

os.environ.setdefault("TQDM_DISABLE", "1")  # 关掉 AKShare 的进度条，避免污染 Agent 读到的输出

import argparse
import datetime as dt
import sys

import akshare as ak
import pandas as pd


def market_of(code: str) -> str:
    """6 位代码 -> 交易所简称 sh / sz / bj。"""
    if code.startswith(("6", "9")) and not code.startswith("92"):
        return "sh"
    if code.startswith(("4", "8", "92")):
        return "bj"
    return "sz"


def ymd(days_ago: int = 0) -> str:
    return (dt.date.today() - dt.timedelta(days=days_ago)).strftime("%Y%m%d")


def first_ok(*candidates):
    """依次尝试 (函数名, 调用) 候选，返回第一个成功且非空的结果；全失败则抛出汇总错误。"""
    errors = []
    for func, call in candidates:
        try:
            df = call()
            if df is not None and len(df) > 0:
                return df, func
            errors.append(f"{func}: 返回空数据")
        except Exception as e:
            errors.append(f"{func}: {type(e).__name__}: {str(e)[:120]}")
    raise RuntimeError("所有数据源均失败 -> " + " | ".join(errors))


def emit(df, func: str, rows: int, tail: bool, csv: str | None, note: str | None):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"> 来源: AKShare `{func}` | 拉取时间: {now}")
    if note:
        print(f"> 注意: {note}")
    if df is None or len(df) == 0:
        print("（无数据）")
        return
    if csv:
        df.to_csv(csv, index=False, encoding="utf-8-sig")
        print(f"> 完整数据已保存: {csv}（共 {len(df)} 行）")
    shown = df.tail(rows) if tail else df.head(rows)
    if len(df) > rows:
        print(f"> 共 {len(df)} 行，显示{'最新' if tail else '前'} {rows} 行（--rows 调整）")
    print(shown.to_markdown(index=False))


# ---------- 个股 ----------
def c_info(a):
    return first_ok(
        ("stock_individual_info_em", lambda: ak.stock_individual_info_em(symbol=a.code)),
        ("stock_profile_cninfo", lambda: ak.stock_profile_cninfo(symbol=a.code).T.reset_index()
         .set_axis(["项目", "值"], axis=1)),
    )


def c_hist(a):
    start, end = a.start or ymd(365), a.end or ymd()
    sym = market_of(a.code) + a.code
    return first_ok(
        ("stock_zh_a_hist", lambda: ak.stock_zh_a_hist(symbol=a.code, period=a.period, start_date=start,
                                                        end_date=end, adjust=a.adjust)),
        # 备用源只有日线
        ("stock_zh_a_daily", lambda: ak.stock_zh_a_daily(symbol=sym, start_date=start, end_date=end,
                                                          adjust=a.adjust)),
        ("stock_zh_a_hist_tx", lambda: ak.stock_zh_a_hist_tx(symbol=sym, start_date=start, end_date=end,
                                                              adjust=a.adjust)),
    )


def c_finance(a):
    return first_ok(("stock_financial_abstract_ths",
                     lambda: ak.stock_financial_abstract_ths(symbol=a.code, indicator=a.by)))


def c_indicator(a):
    return first_ok(("stock_financial_analysis_indicator",
                     lambda: ak.stock_financial_analysis_indicator(symbol=a.code, start_year=a.start_year)))


def c_valuation(a):
    return first_ok(("stock_value_em", lambda: ak.stock_value_em(symbol=a.code)))


def c_flow(a):
    def instant():
        df = ak.stock_fund_flow_individual(symbol="即时")
        return df[df["股票代码"].astype(str).str.zfill(6) == a.code]
    return first_ok(
        ("stock_individual_fund_flow",
         lambda: ak.stock_individual_fund_flow(stock=a.code, market=market_of(a.code))),
        ("stock_fund_flow_individual(即时，同花顺口径)", instant),
    )


def c_holders(a):
    return first_ok(("stock_main_stock_holder", lambda: ak.stock_main_stock_holder(stock=a.code)))


def c_pledge(a):
    return first_ok(("stock_gpzy_individual_pledge_ratio_detail_em",
                     lambda: ak.stock_gpzy_individual_pledge_ratio_detail_em(symbol=a.code)))


def c_unlock(a):
    return first_ok(("stock_restricted_release_queue_em",
                     lambda: ak.stock_restricted_release_queue_em(symbol=a.code)))


def c_northbound(a):
    return first_ok(("stock_hsgt_individual_em", lambda: ak.stock_hsgt_individual_em(symbol=a.code)))


def c_news(a):
    return first_ok(("stock_news_em", lambda: ak.stock_news_em(symbol=a.code)))


def c_notices(a):
    return first_ok(("stock_individual_notice_report",
                     lambda: ak.stock_individual_notice_report(security=a.code, symbol=a.kind,
                                                               begin_date=a.start or ymd(180),
                                                               end_date=a.end or ymd())))


def c_reports(a):
    return first_ok(("stock_research_report_em", lambda: ak.stock_research_report_em(symbol=a.code)))


# ---------- 市场 / 行业 / 宏观 ----------
def c_lhb(a):
    def call():
        df = ak.stock_lhb_detail_em(start_date=a.start or ymd(30), end_date=a.end or ymd())
        return df[df["代码"].astype(str) == a.code] if a.code else df
    return first_ok(("stock_lhb_detail_em", call))


def c_index(a):
    start, end = a.start or ymd(365), a.end or ymd()

    def sina():
        df = ak.stock_zh_index_daily(symbol=("sh" if a.code.startswith("000") else "sz") + a.code)
        d = pd.to_datetime(df["date"])
        return df[(d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))]
    return first_ok(
        ("index_zh_a_hist", lambda: ak.index_zh_a_hist(symbol=a.code, period=a.period,
                                                        start_date=start, end_date=end)),
        ("stock_zh_index_daily", sina),  # 备用源只有日线
    )


def c_industry(a):
    if a.name:
        return first_ok(("stock_board_industry_hist_em",
                         lambda: ak.stock_board_industry_hist_em(symbol=a.name, start_date=a.start or ymd(180),
                                                                 end_date=a.end or ymd(), period="日k",
                                                                 adjust="")))
    return first_ok(
        ("stock_board_industry_name_em", ak.stock_board_industry_name_em),
        ("stock_board_industry_summary_ths", ak.stock_board_industry_summary_ths),
    )


# 名称 -> (函数名, 原始数据是否新数据在前)；新数据在前的会被翻转，保证表格最后一行是最新
MACRO = {
    "gdp": ("macro_china_gdp", True),
    "pmi": ("macro_china_pmi", True),
    "cpi": ("macro_china_cpi", True),
    "ppi": ("macro_china_ppi", True),
    "m2": ("macro_china_money_supply", True),
    "credit": ("macro_china_new_financial_credit", True),
    "shrz": ("macro_china_shrzgm", False),
    "lpr": ("macro_china_lpr", False),
    "bond": ("bond_zh_us_rate", False),
}


def c_macro(a):
    func, newest_first = MACRO[a.name]
    call = (lambda: ak.bond_zh_us_rate(start_date=ymd(730))) if a.name == "bond" else getattr(ak, func)
    df, func = first_ok((func, call))
    if newest_first:
        df = df.iloc[::-1]
    return df, func


def c_market(a):
    calls = {
        "activity": ("stock_market_activity_legu", ak.stock_market_activity_legu),
        "zt": ("stock_zt_pool_em", lambda: ak.stock_zt_pool_em(date=a.date or ymd())),
        "margin": ("stock_margin_account_info", ak.stock_margin_account_info),
        "pe": ("stock_market_pe_lg", lambda: ak.stock_market_pe_lg(symbol="上证")),
        "hsgt": ("stock_hsgt_fund_flow_summary_em", ak.stock_hsgt_fund_flow_summary_em),
    }
    return first_ok(calls[a.name])


def c_raw(a):
    kwargs = dict(kv.split("=", 1) for kv in a.kwargs)
    return first_ok((a.func, lambda: getattr(ak, a.func)(**kwargs)))


NOTES = {
    "northbound": "2024-08 起北向资金个股持股改为按季度披露，日度数据可能停在 2024-08，请核对日期后再引用。",
    "flow": "主力资金口径因数据商而异，只看方向和相对变化，不要与其他来源的绝对值混用。",
    "pledge": "无数据可能表示该股没有质押记录，需到巨潮资讯核实后再下结论。",
    "unlock": "无数据可能表示近期没有解禁安排，需到巨潮资讯核实后再下结论。",
    "macro": "部分宏观数据源更新会滞后，引用前核对最后一行日期；滞后时用 WebSearch 查统计局/央行最新发布。",
}


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--rows", type=int, default=30, help="最多显示行数，默认 30")
    common.add_argument("--csv", help="把完整结果另存为 CSV")
    p = argparse.ArgumentParser(description="A 股数据取数工具（AKShare）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, help_, fn, tail=False, code=True):
        sp = sub.add_parser(name, help=help_, parents=[common])
        if code:
            sp.add_argument("code", help="6 位股票代码，如 600519")
        sp.set_defaults(fn=fn, tail=tail)
        return sp

    add("info", "个股基本信息（行业、市值、股本、上市日期）", c_info)
    sp = add("hist", "个股 K 线（默认近一年日线、前复权）", c_hist, tail=True)
    sp.add_argument("--period", default="daily", choices=["daily", "weekly", "monthly"])
    sp.add_argument("--start", help="YYYYMMDD，默认一年前")
    sp.add_argument("--end", help="YYYYMMDD，默认今天")
    sp.add_argument("--adjust", default="qfq", choices=["", "qfq", "hfq"], help="复权方式，默认前复权")
    sp = add("finance", "财务摘要（营收、净利、扣非、毛利率、ROE、现金流等）", c_finance, tail=True)
    sp.add_argument("--by", default="按报告期", choices=["按报告期", "按年度", "按单季度"])
    sp = add("indicator", "详细财务分析指标（新浪）", c_indicator, tail=True)
    sp.add_argument("--start-year", default=str(dt.date.today().year - 3))
    add("valuation", "每日估值（PE/PB/PS/PEG/市值）", c_valuation, tail=True)
    add("flow", "个股主力资金流向", c_flow, tail=True)
    add("holders", "主要股东", c_holders)
    add("pledge", "股权质押明细", c_pledge)
    add("unlock", "限售股解禁计划", c_unlock)
    add("northbound", "北向资金持股", c_northbound, tail=True)
    add("news", "个股新闻", c_news)
    sp = add("notices", "公司公告列表（含原文链接）", c_notices)
    sp.add_argument("--kind", default="全部",
                    help="全部/重大事项/财务报告/融资公告/风险提示/资产重组/信息变更/持股变动")
    sp.add_argument("--start", help="YYYYMMDD，默认 180 天前")
    sp.add_argument("--end")
    add("reports", "券商研报列表、评级与盈利预测", c_reports)
    sp = add("lhb", "龙虎榜明细", c_lhb, code=False)
    sp.add_argument("--code", help="只看某只股票")
    sp.add_argument("--start", help="YYYYMMDD，默认 30 天前")
    sp.add_argument("--end")
    sp = add("index", "指数 K 线（000300 沪深300、000905 中证500、000852 中证1000、399006 创业板指）",
             c_index, tail=True)
    sp.add_argument("--period", default="daily", choices=["daily", "weekly", "monthly"])
    sp.add_argument("--start")
    sp.add_argument("--end")
    sp = add("industry", "行业板块：不带名称列出全部行业当日表现，带名称返回该板块 K 线", c_industry, code=False)
    sp.add_argument("name", nargs="?", help="东方财富行业名，如 半导体")
    sp.add_argument("--start")
    sp.add_argument("--end")
    sp = add("macro", "宏观数据（最后一行为最新）", c_macro, tail=True, code=False)
    sp.add_argument("name", choices=list(MACRO))
    sp = add("market", "市场情绪：activity 涨跌家数 / zt 涨停池 / margin 两融 / pe 上证估值 / hsgt 沪深港通",
             c_market, tail=True, code=False)
    sp.add_argument("name", choices=["activity", "zt", "margin", "pe", "hsgt"])
    sp.add_argument("--date", help="涨停池日期 YYYYMMDD，默认今天")
    sp = add("raw", "直接调用任意 AKShare 函数：raw <函数名> key=value ...", c_raw, code=False)
    sp.add_argument("func")
    sp.add_argument("kwargs", nargs="*")

    a = p.parse_args()
    if a.cmd == "industry" and a.name:
        a.tail = True
    try:
        df, func = a.fn(a)
    except Exception as e:  # 第三方接口不稳定，报错时给出可操作的提示
        print(f"> 取数失败（{a.cmd}）：{e}")
        if a.cmd in NOTES:
            print(f"> 注意: {NOTES[a.cmd]}")
        print("> 建议：确认代码/日期参数；稍后重试；或用 WebSearch/WebFetch 从一手来源核实。"
              "不要用估计值替代。")
        sys.exit(1)
    emit(df, func, a.rows, a.tail, a.csv, NOTES.get(a.cmd))


if __name__ == "__main__":
    main()
