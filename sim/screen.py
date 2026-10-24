"""量化初筛：全市场 → 按成交额初选 → 拉日线细筛 → 打分排序。结果按日期缓存，所有账户共用。

注意：全市场快照只能取到"当前"数据，所以初筛只能在当天收盘后运行，不能补跑历史日期。
"""
import pandas as pd

from . import config


def run(date: str, market, rcfg: dict, scfg: dict, max_lot_value: float, log=print) -> pd.DataFrame:
    path = config.screen_dir() / f"{date}.csv"
    if path.exists():
        return pd.read_csv(path, dtype={"code": str})
    u = rcfg["universe"]
    spot = market.spot()
    spot = spot.rename(columns={"代码": "code", "名称": "name", "最新价": "price", "涨跌幅": "chg_1d",
                                "成交额": "amount"})
    spot = spot[spot["price"] > 0].copy()
    num = spot["code"].str[2:]
    bad = spot["code"].str.startswith("bj") | num.str.startswith(tuple(u["exclude_prefixes"]))
    bad |= spot["name"].str.contains("ST|退", regex=True) | spot["name"].str.match(r"^[NC]")
    spot = spot[~bad]
    spot = spot[spot["price"] * 100 <= max_lot_value]           # 一手都买不起的剔除
    spot = spot[spot["chg_1d"] / 100 <= u["max_day_change"]]
    pre = spot.sort_values("amount", ascending=False).head(scfg["screen"]["prefilter_top"])
    log(f"初筛：全市场 {len(spot)} 只通过基础过滤，取成交额前 {len(pre)} 只细筛")

    rows = []
    for code, name in zip(pre["code"], pre["name"]):
        df = market.daily(code, date)
        if df is None or len(df) < 61 or df.index[-1] != date:
            continue
        c = df["close"]
        ret = c.pct_change()
        r = {"code": code, "name": name, "close": c.iloc[-1],
             "listed_days": len(df),  # 拉取窗口内的交易日数，足够判断是否次新
             "avg_amount": df["amount"].tail(20).mean(),
             "chg_1d": c.iloc[-1] / c.iloc[-2] - 1, "chg_5d": c.iloc[-1] / c.iloc[-6] - 1,
             "ret_20": c.iloc[-1] / c.iloc[-21] - 1, "vol_20": ret.tail(20).std() * (250 ** 0.5),
             "ma20": c.tail(20).mean(), "ma60": c.tail(60).mean()}
        r["trend_ok"] = bool(r["close"] > r["ma20"] > r["ma60"])
        r["score"] = r["ret_20"] / r["vol_20"] if r["vol_20"] > 0 else 0.0
        rows.append(r)
    out = pd.DataFrame(rows)
    if len(out):
        ok = (out["trend_ok"] & (out["avg_amount"] >= u["min_avg_amount"])
              & (out["chg_5d"] <= u["max_5d_change"]) & (out["chg_1d"] <= u["max_day_change"]))
        out["eligible"] = ok
        out = out.sort_values(["eligible", "score"], ascending=False).reset_index(drop=True)
        out["rank"] = range(1, len(out) + 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    log(f"初筛完成：{int(out['eligible'].sum()) if len(out) else 0} 只符合条件，已保存 {path.name}")
    return out


def info_for(code: str, date: str, market) -> dict | None:
    """初筛之外的单只股票（如 Agent 草稿里的）现算风控字段。"""
    df = market.daily(code, date)
    if df is None or len(df) < 6 or df.index[-1] != date:
        return None
    c = df["close"]
    return {"avg_amount": float(df["amount"].tail(20).mean()), "chg_1d": float(c.iloc[-1] / c.iloc[-2] - 1),
            "chg_5d": float(c.iloc[-1] / c.iloc[-6] - 1), "listed_days": float(len(df))}


def info_map(df: pd.DataFrame) -> dict:
    """代码 -> 风控需要的字段。"""
    cols = ["avg_amount", "chg_1d", "chg_5d", "listed_days"]
    return {r["code"]: {k: float(r[k]) for k in cols} for _, r in df.iterrows()} if len(df) else {}
