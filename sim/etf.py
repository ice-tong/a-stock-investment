"""主动 ETF：品种分类、可交易性校验、候选池。

与个股的区别：ETF 没有 ST、几乎不停牌，但流动性和规模差异极大（货币 ETF 与冷门行业 ETF
差几个数量级），所以入池和下单都要过一道流动性门槛；涨跌幅与默认止损按品种类别给
（科创板/创业板 ETF 是 20%，债券 ETF 波动小、止损更紧）。

配置在 config/etf.yaml；本模块不读也不改 config/risk.yaml。
"""
import functools

import pandas as pd

from . import config
from . import market as mk


@functools.lru_cache(maxsize=1)
def cfg() -> dict:
    return config.load("etf")


def profile(code: str, name: str = "") -> dict:
    """ETF 品种类别 → {class, limit, stop}。按关键词和代码前缀匹配，equity 兜底。"""
    c = cfg()
    eq = c["classes"]["equity"]
    # 名字里带商品关键词、但实际持有的是股票：黄金股 / 有色金属 / 矿业，以及因子 ETF 里的"现金流"
    if any(k in (name or "") for k in ("黄金股", "有色", "矿业", "现金流")):
        return {"class": "equity", "limit": float(eq["limit"]), "stop": float(eq["stop"])}
    for key, cl in c["classes"].items():
        if key == "equity":
            continue
        if any(k in (name or "") for k in (cl.get("names") or [])):
            return {"class": key, "limit": float(cl["limit"]), "stop": float(cl["stop"])}
        if any(code.startswith(p) for p in (cl.get("prefixes") or [])):
            return {"class": key, "limit": float(cl["limit"]), "stop": float(cl["stop"])}
    return {"class": "equity", "limit": float(eq["limit"]), "stop": float(eq["stop"])}


def amount_series(df: pd.DataFrame) -> pd.Series:
    """ETF 日线来自 fund_etf_hist_sina，没有 amount 列，用 close × volume 估算成交额。"""
    if "amount" in df.columns and df["amount"].notna().any():
        return df["amount"]
    return df["close"] * df["volume"]


def features(df: pd.DataFrame) -> dict | None:
    """ETF 日线 → 研究用特征。不足 60 个交易日返回 None。"""
    if df is None or len(df) < 60:
        return None
    c = df["close"]
    ret = c.pct_change()
    return {
        "close": float(c.iloc[-1]),
        "chg_1d": float(c.iloc[-1] / c.iloc[-2] - 1),
        "chg_5d": float(c.iloc[-1] / c.iloc[-6] - 1),
        "ret_20": float(c.iloc[-1] / c.iloc[-21] - 1),
        "ret_60": float(c.iloc[-1] / c.iloc[-61] - 1),
        "vol_20": float(ret.tail(20).std() * 250 ** 0.5),
        "dd_60": float(c.iloc[-1] / df["high"].tail(60).max() - 1),
        "avg_amount": float(amount_series(df).tail(20).mean()),
        "listed_days": float(len(df)),
    }


def check(market, code: str, date: str, name: str = "") -> tuple:
    """主动 ETF 的下单门槛。返回 (ok, reason)。"""
    if mk.kind(code) != "etf":
        return False, "不是 ETF"
    df = market.daily(code, date)
    if df is None or len(df) == 0:
        return False, "没有行情数据"
    p = cfg()["pool"]
    if len(df) < p["min_listed_days"]:
        return False, f"上市不足 {p['min_listed_days']} 个交易日"
    amt = float(amount_series(df).tail(20).mean())
    if amt < p["min_avg_amount"]:
        return False, f"近 20 日日均成交额 {amt / 1e8:.2f} 亿，低于下限 {p['min_avg_amount'] / 1e8:.1f} 亿"
    if float(df["close"].iloc[-1]) <= 0:
        return False, "价格异常"
    return True, ""


def build_pool(date: str, market, log) -> pd.DataFrame:
    """全市场 ETF 候选池（新浪清单 + 逐只日线特征）。产物 data/pool/<日期>_etf.csv|.md。

    逐只取日线是串行的：fund_etf_hist_sina 内部每次新建一个 V8 实例（mini_racer），
    并发调用会让进程直接崩掉，不能用线程池。
    """
    import akshare as ak

    spot = ak.fund_etf_category_sina(symbol="ETF基金")
    spot = spot.rename(columns={"代码": "code", "名称": "name", "最新价": "close",
                                "涨跌幅": "chg_pct", "成交额": "amount"})
    spot["code"] = spot["code"].astype(str).str.lower()
    for col in ("close", "amount", "chg_pct"):
        spot[col] = pd.to_numeric(spot[col], errors="coerce")
    c = cfg()["pool"]
    spot = spot[spot["code"].str.match(r"^(sh|sz)\d{6}$") & (spot["close"] > 0)]
    spot = spot[spot["amount"] >= c["min_amount"]].sort_values("amount", ascending=False)
    spot = spot.assign(cls=[profile(code, name)["class"] for code, name in zip(spot["code"], spot["name"])])
    # 分类取前 N：债券 ETF 的成交额比权益类高一个量级，混在一起排会把权益类全部挤掉
    spot = spot.groupby("cls", group_keys=False).head(c["top_per_class"])
    log(f"ETF 清单 {len(spot)} 只达到门槛（当日成交额 ≥ {c['min_amount'] / 1e8:.1f} 亿，每个类别最多 {c['top_per_class']} 只），逐只取日线")

    rows = []
    for i, row in enumerate(spot.itertuples(), 1):
        try:
            f = features(market.daily(row.code, date))
        except Exception:
            f = None
        if not f or f["avg_amount"] < c["min_avg_amount"]:
            continue
        prof = profile(row.code, row.name)
        rows.append({"code": row.code, "name": row.name, "class": prof["class"],
                     "limit": prof["limit"], "stop": prof["stop"], "amount": row.amount, **f})
        if i % 20 == 0:
            log(f"  ETF 特征 {i}/{len(spot)}")
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values(["class", "amount"], ascending=[True, False])
    out = config.home() / "data" / "pool"
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / f"{date}_etf.csv", index=False, encoding="utf-8-sig")
    cols = ["code", "name", "class", "close", "chg_1d", "chg_5d", "ret_20", "ret_60",
            "vol_20", "dd_60", "avg_amount"]
    md = [f"# {date} ETF 候选池（{len(df)} 只）", "",
          "> 品种不设限，只过滤流动性：当日成交额 ≥ "
          f"{c['min_amount'] / 1e8:.1f} 亿、近 20 日日均 ≥ {c['min_avg_amount'] / 1e8:.1f} 亿、上市 ≥ {c['min_listed_days']} 个交易日。",
          "> `class` 决定涨跌幅与默认止损：star/chinext 20%、bond 3%、money 2%、其余 10% / 8%。", ""]
    show = df[cols].copy()
    for k in ("chg_1d", "chg_5d", "ret_20", "ret_60", "vol_20", "dd_60"):
        show[k] = (show[k] * 100).round(2)
    show["close"] = show["close"].round(3)
    show["avg_amount"] = (show["avg_amount"] / 1e8).round(2)
    show.columns = ["代码", "名称", "类别", "价格", "当日%", "5日%", "20日%", "60日%",
                    "年化波动%", "距60日高%", "20日均额(亿)"]
    md.append(show.to_markdown(index=False))
    (out / f"{date}_etf.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return df
