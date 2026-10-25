"""主策略候选池：全市场多路召回，供 Agent 晚间研究做"轻量点评 → 深度研究"漏斗的第一层。

和 screen.py 的区别：
- screen.py 是量化/随机对照组的选股输入（成交额前 120 → 单一动量排序），口径保持不变，保证对照组可比；
- pool.py 只服务 main：全市场按流动性预过滤后逐只算特征，再按多个互不相同的选股逻辑（通道）各取前 N，
  并上持仓与 Agent 维护的观察名单，得到 100-200 只、标注了命中通道的候选池。

通道之间刻意选不相关的逻辑（短期动量、中期趋势、上升趋势中的回调、低波动低估值、放量突破、强势行业龙头），
避免整个求解空间被一个因子钉死。排序只决定"谁进池"，不决定买不买——买不买由 Agent 研究决定。

输出：data/pool/<日期>.csv（候选池，入库留档）、data/pool/<日期>.md（按通道分组的精简表，给 LLM 读）、
data/cache/pool/<日期>_features.csv（全市场特征，不入库）。同一天重复运行直接读缓存。
"""
import json
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from . import config, risk

CHANNELS = {
    "momentum": "短期动量：收盘>MA20>MA60，按 20 日收益/波动率排序",
    "trend": "中期趋势：收盘>MA120 且 MA60>MA120，按 120 日收益/60 日波动率排序",
    "pullback": "趋势中回调：中期趋势向上，但距 60 日高点回撤 8%-20% 且近 5 日下跌，按 120 日收益排序",
    "defensive": "低波低估：0<PE≤25、收盘>MA120，按 60 日波动率从低到高",
    "breakout": "放量突破：收盘接近 60 日新高且 5 日/20 日成交额比 ≥1.5，按量比排序",
    "sector": "强势行业：20 日收益中位数前列的行业里，各取趋势向上的头部个股",
    "holding": "main 当前持仓（每晚必须复核）",
    "watchlist": "观察名单 data/watchlist.json 中仍在跟踪的标的",
}


def pool_dir():
    return config.home() / "data" / "pool"


def watchlist_path():
    return config.home() / "data" / "watchlist.json"


# ---------------- 纯计算（可离线测试） ----------------
def features(df: pd.DataFrame) -> dict | None:
    """单只股票的前复权日线 → 特征。数据不足 130 个交易日（次新或停牌太久）返回 None。"""
    if df is None or len(df) < 130:
        return None
    c, amt = df["close"], df["amount"]
    ret = c.pct_change()
    hi60 = df["high"].tail(60).max()
    f = {"close": float(c.iloc[-1]), "listed_days": float(len(df)),
         "chg_1d": c.iloc[-1] / c.iloc[-2] - 1, "chg_5d": c.iloc[-1] / c.iloc[-6] - 1,
         "ret_20": c.iloc[-1] / c.iloc[-21] - 1, "ret_60": c.iloc[-1] / c.iloc[-61] - 1,
         "ret_120": c.iloc[-1] / c.iloc[-121] - 1,
         "vol_20": ret.tail(20).std() * 250 ** 0.5, "vol_60": ret.tail(60).std() * 250 ** 0.5,
         "ma20": c.tail(20).mean(), "ma60": c.tail(60).mean(), "ma120": c.tail(120).mean(),
         "dd_60": c.iloc[-1] / hi60 - 1, "near_high_60": c.iloc[-1] >= 0.97 * hi60,
         "avg_amount": float(amt.tail(20).mean()),
         "amt_ratio": float(amt.tail(5).mean() / amt.tail(20).mean()) if amt.tail(20).mean() > 0 else 0.0}
    if "turnover" in df.columns:
        f["turnover_20"] = float(df["turnover"].tail(20).mean())
    return f


def channels(feat: pd.DataFrame, cfg: dict) -> dict:
    """feat：每行一只股票（含 code/name/industry/pe 及 features() 的字段，eligible 已算好）。
    返回 {通道名: [代码, ...]}，各通道内按得分降序。只从 eligible 的股票里召回。"""
    f = feat[feat["eligible"]].copy()
    n = cfg["per_channel"]
    out = {}

    def top(mask, key, asc=False, k=n):
        return f[mask].sort_values(key, ascending=asc).head(k)["code"].tolist()

    trend_short = (f["close"] > f["ma20"]) & (f["ma20"] > f["ma60"])
    trend_mid = (f["close"] > f["ma120"]) & (f["ma60"] > f["ma120"])
    f["s_mom"] = f["ret_20"] / f["vol_20"].where(f["vol_20"] > 0)
    f["s_trend"] = f["ret_120"] / f["vol_60"].where(f["vol_60"] > 0)
    out["momentum"] = top(trend_short, "s_mom")
    out["trend"] = top(trend_mid, "s_trend")
    out["pullback"] = top(trend_mid & f["dd_60"].between(-0.20, -0.08) & (f["chg_5d"] < 0), "ret_120")
    pe = f["pe"] if "pe" in f.columns else pd.Series(float("nan"), index=f.index)
    out["defensive"] = top((pe > 0) & (pe <= cfg["defensive_max_pe"]) & (f["close"] > f["ma120"]), "vol_60", asc=True)
    out["breakout"] = top(f["near_high_60"].astype(bool) & (f["amt_ratio"] >= cfg["breakout_amt_ratio"]), "amt_ratio")
    sec = []
    if "industry" in f.columns and f["industry"].notna().any():
        g = f[f["industry"].notna()].groupby("industry")
        med = g["ret_20"].median()[g.size() >= cfg["sector_min_size"]].sort_values(ascending=False)
        for ind in med.head(cfg["sector_top"]).index:
            sec += top(trend_short & (f["industry"] == ind), "s_mom", k=cfg["sector_per"])
    out["sector"] = sec
    return out


def assemble(feat: pd.DataFrame, chans: dict, holdings: list, watch: list) -> pd.DataFrame:
    """合并各通道、持仓与观察名单，标注命中通道；命中通道越多越靠前，其次按 20 日风险调整动量。"""
    hits = {}
    for ch, codes in list(chans.items()) + [("holding", holdings), ("watchlist", watch)]:
        for c in codes:
            hits.setdefault(c, []).append(ch)
    rows = feat[feat["code"].isin(hits)].copy()
    rows["channels"] = rows["code"].map(lambda c: "|".join(hits[c]))
    rows["n_channels"] = rows["code"].map(lambda c: len(hits[c]))
    rows["s_mom"] = rows["ret_20"] / rows["vol_20"].where(rows["vol_20"] > 0)
    return rows.sort_values(["n_channels", "s_mom"], ascending=False).reset_index(drop=True)


def to_markdown(date: str, pool: pd.DataFrame, chans: dict, stats: dict) -> str:
    def row(r):
        pe = f"{r.pe:.1f}" if pd.notna(r.get("pe")) else "-"
        nmc = f"{r.nmc / 1e8:.0f}" if pd.notna(r.get("nmc")) else "-"
        flag = "" if r.eligible else "（不合规）"
        ind = r.get("industry") if pd.notna(r.get("industry")) else "-"  # 新浪行业表未覆盖的股票
        return (f"| {r.code} | {r['name']}{flag} | {ind} | {r.close:.2f} | {r.ret_20:+.1%} | "
                f"{r.ret_120:+.1%} | {r.dd_60:+.1%} | {r.vol_60:.0%} | {pe} | {nmc} | {r.avg_amount / 1e8:.1f} | "
                f"{r.channels} |")

    head = ("| 代码 | 名称 | 行业 | 收盘 | 20日 | 120日 | 距60日高 | 60日波动 | PE | 流通市值亿 | 日均成交亿 | 通道 |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|")
    lines = [f"# {date} 主策略候选池（{len(pool)} 只）", "",
             f"全市场 {stats['spot']} 只 → 基础过滤 {stats['base']} 只 → 流动性预过滤 {stats['pre']} 只 → "
             f"有效特征 {stats['feat']} 只 → 合规 {stats['eligible']} 只 → 多路召回 {len(pool)} 只。",
             "数据：新浪前复权日线、新浪行业板块（行业/PE/流通市值）。排序只决定进池，不代表买入建议。", "",
             "## 通道说明", ""] + [f"- **{k}**：{v}（{len(chans.get(k, []))} 只）" for k, v in CHANNELS.items()
                                   if k in chans or k in ("holding", "watchlist")]
    multi = pool[pool["n_channels"] >= 2]
    lines += ["", f"## 多通道共振（{len(multi)} 只）", "", head] + [row(r) for _, r in multi.iterrows()]
    for ch in CHANNELS:
        sub = pool[pool["channels"].str.split("|").map(lambda x: ch in x)]
        if len(sub):
            lines += ["", f"## {ch}（{len(sub)} 只）", "", head] + [row(r) for _, r in sub.iterrows()]
    return "\n".join(lines) + "\n"


# ---------------- 取数与运行 ----------------
def _fetch_qfq(code: str, start: str, end: str):
    import akshare as ak
    try:
        df = ak.stock_zh_a_daily(symbol=code, start_date=start, end_date=end, adjust="qfq")
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    df["date"] = df["date"].astype(str).str[:10]
    return df.set_index("date")


def _one(job):
    code, name, start, end, date = job
    df = _fetch_qfq(code, start, end)
    if df is None or df.index[-1] != date:
        return None
    f = features(df)
    return None if f is None else {"code": code, "name": name} | f


def industry_map(log=print) -> pd.DataFrame:
    """新浪行业板块 → 每只股票的行业、PE、PB、流通市值。失败返回空表（行业/估值通道随之为空）。"""
    import akshare as ak
    try:
        sectors = ak.stock_sector_spot(indicator="新浪行业")
    except Exception as e:
        log(f"候选池：行业板块获取失败（{type(e).__name__}），跳过行业与估值通道")
        return pd.DataFrame()

    def one(item):
        label, name = item
        try:
            d = ak.stock_sector_detail(sector=label)
            d["industry"] = name
            return d[["symbol", "industry", "per", "pb", "nmc"]]
        except Exception:
            return None

    parts = [p for p in map(one, zip(sectors["label"], sectors["板块"])) if p is not None]
    if not parts:
        return pd.DataFrame()
    m = pd.concat(parts).drop_duplicates("symbol").rename(columns={"symbol": "code", "per": "pe"})
    m["nmc"] = pd.to_numeric(m["nmc"], errors="coerce") * 1e4  # 新浪单位：万元
    for k in ("pe", "pb"):
        m[k] = pd.to_numeric(m[k], errors="coerce")
    return m


def load_watchlist() -> list:
    p = watchlist_path()
    if not p.exists():
        return []
    items = json.loads(p.read_text(encoding="utf-8")).get("items", [])
    return [i["code"] for i in items if i.get("status", "watching") in ("watching", "researched")]


def run(date: str, market, rcfg: dict, pcfg: dict, max_lot_value: float, holdings: list,
        log=print, force: bool = False) -> pd.DataFrame:
    path = pool_dir() / f"{date}.csv"
    if path.exists() and not force:
        return pd.read_csv(path, dtype={"code": str})
    import datetime as dt
    u = rcfg["universe"]
    spot = market.spot().rename(columns={"代码": "code", "名称": "name", "最新价": "price", "成交额": "amount"})
    n_spot = len(spot)
    spot = spot[spot["price"] > 0]
    num = spot["code"].str[2:]
    bad = spot["code"].str.startswith("bj") | num.str.startswith(tuple(u["exclude_prefixes"]))
    bad |= spot["name"].str.contains("ST|退", regex=True) | spot["name"].str.match(r"^[NC]")
    spot = spot[~bad & (spot["price"] * 100 <= max_lot_value)]
    n_base = len(spot)
    pre = spot[(spot["amount"] >= pcfg["prefilter_min_amount"]) | spot["code"].isin(holdings)]
    pre = pre.sort_values("amount", ascending=False).head(pcfg["prefilter_max"])
    log(f"候选池：全市场 {n_spot} 只，基础过滤后 {n_base} 只，流动性预过滤 {len(pre)} 只，开始拉取日线")

    start = (dt.date.fromisoformat(date) - dt.timedelta(days=pcfg["history_days"])).strftime("%Y%m%d")
    end = date.replace("-", "")

    jobs = [(c, n, start, end, date) for c, n in zip(pre["code"], pre["name"])]
    with ProcessPoolExecutor(pcfg["workers"]) as ex:  # 新浪日线用 py_mini_racer(V8) 解码，多线程会崩溃，必须多进程
        rows = [r for r in ex.map(_one, jobs, chunksize=20) if r is not None]
    feat = pd.DataFrame(rows)
    if len(feat) == 0:
        raise RuntimeError(f"候选池：{date} 没有取到任何有效日线（数据未更新或接口失败），不生成候选池")
    ind = industry_map(log)
    if len(ind):
        feat = feat.merge(ind, on="code", how="left")
    else:
        feat[["industry", "pe", "pb", "nmc"]] = None
    # 与 risk.universe_check 同一口径，保证进池的股票下单时不会被股票池规则拒绝
    feat["eligible"] = [risk.universe_check(r.code, r.name, {"avg_amount": r.avg_amount, "chg_1d": r.chg_1d,
                                                               "chg_5d": r.chg_5d, "listed_days": r.listed_days},
                                            u) is None for r in feat.itertuples()]
    cache = config.cache_dir() / "pool"
    cache.mkdir(parents=True, exist_ok=True)
    feat.to_csv(cache / f"{date}_features.csv", index=False)

    chans = channels(feat, pcfg)
    out = assemble(feat, chans, holdings, load_watchlist())
    stats = {"spot": n_spot, "base": n_base, "pre": len(pre), "feat": len(feat), "eligible": int(feat["eligible"].sum())}
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    (pool_dir() / f"{date}.md").write_text(to_markdown(date, out, chans, stats), encoding="utf-8")
    log(f"候选池完成：{len(out)} 只（" + "，".join(f"{k} {len(v)}" for k, v in chans.items()) + f"），已保存 {path.name}")
    return out
