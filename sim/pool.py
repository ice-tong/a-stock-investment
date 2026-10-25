"""主策略候选池：全市场多路召回 + 分诊（triage），供 Agent 晚间研究做"轻量点评 → 深度研究"漏斗的第一层。

和 screen.py 的区别：
- screen.py 是量化/随机对照组的选股输入（成交额前 120 → 单一动量排序），口径保持不变，保证对照组可比；
- pool.py 只服务 main：全市场按流动性预过滤后逐只算特征，再按多个互不相同的选股逻辑（通道）各取前 N，
  并上持仓与 Agent 维护的观察名单，得到 100-200 只、标注了命中通道的候选池。

v1.1（2026-10-25 用户批准方案 2）：
- 新增确定性字段：`stop_prob`（按 -8% 止损、20 日持有估算触损概率）、`one_lot_value`、
  `dist_ma20/ma60/high60`、`trigger/action`（可行动 / 等回调 / 等突破 / 观望 / 回避）、`persist_days`（近 10 日进池次数）。
- 不再按 `n_channels` 作为第一排序键；改为按"regime 风格配额 + 可行动性 + 风险适配"排序。
- markdown 改成按可行动性分组的"分诊表"，把多通道共振降级为文末的参考信息。
- 观察名单支持结构化触发条件（`trigger_type` / `trigger_level`），每晚评估是否触发。

输出：data/pool/<日期>.csv（候选池，入库留档）、data/pool/<日期>.md（分诊表，给 LLM 读）、
data/cache/pool/<日期>_features.csv（全市场特征，不入库）。同一天重复运行直接读缓存。
"""
import json
import math
import re
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

STOP_LOSS_PCT = 0.08          # 与 config/risk.yaml 的默认止损一致；只用于估算，不替代风控
STOP_HORIZON = 20             # 时间止损/评估窗口（交易日）
TRADING_DAYS = 250

REGIME_CN = {"offense": "进攻", "neutral": "中性", "defense": "防守"}
REGIME_STYLE_WEIGHTS = {
    # 风格权重只影响研究顺序，不改变下单；数值是相对权重，不是仓位
    "defense": {"lowvol": 5.0, "pullback": 4.0, "breakout": 2.0, "trend": 1.5, "sector": 1.2,
                "momentum": 1.0, "holding": 0.0, "watchlist": 0.0, "other": 1.0},
    "neutral": {"lowvol": 3.0, "pullback": 3.0, "breakout": 3.0, "trend": 2.5, "sector": 2.5,
                "momentum": 2.0, "holding": 0.0, "watchlist": 0.0, "other": 1.5},
    "offense": {"lowvol": 1.5, "pullback": 2.0, "breakout": 5.0, "trend": 4.0, "sector": 4.0,
                "momentum": 3.0, "holding": 0.0, "watchlist": 0.0, "other": 1.5},
}
ACTION_BONUS = {"ready": 3.0, "wait_pullback": 1.0, "wait_breakout": 1.0, "watch": 0.0,
                "watching": 0.0, "holding": 2.0, "avoid": -10.0}
ACTION_ORDER = {"ready": 0, "wait_pullback": 1, "wait_breakout": 2, "watch": 3,
                "watching": 4, "holding": 5, "avoid": 9}
ACTION_CN = {"ready": "可行动", "wait_pullback": "等回调", "wait_breakout": "等突破",
             "watch": "观望", "watching": "观察", "holding": "持仓复核", "avoid": "回避/降权"}
REGIME_HINT = {
    "defense": "防守：低波/回调优先，momentum/trend/breakout 只作观察；研究配额建议低波+回调 ≥50%",
    "neutral": "中性：低波/回调/突破均衡，单一风格不超过研究配额的 40%",
    "offense": "进攻：突破/强势行业优先，低波只作防守储备；单一风格不超过研究配额的 50%",
}


def pool_dir():
    return config.home() / "data" / "pool"


def watchlist_path():
    return config.home() / "data" / "watchlist.json"


def _nan(x) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


# ---------------- 纯计算（可离线测试） ----------------
def stop_prob(vol: float) -> float:
    """漂移率为 0 时，20 个交易日内触及 -8% 止损的近似概率：2Φ(-b/(σ√T))。

    只是候选层风险适配的标签，不是对个股的预测；vol 为年化波动率（小数）。
    """
    vol = _nan(vol)
    if not math.isfinite(vol) or vol <= 0:
        return float("nan")
    z = (-STOP_LOSS_PCT / (vol * math.sqrt(STOP_HORIZON / TRADING_DAYS))) / math.sqrt(2)
    return 1.0 + math.erf(z)


def features(df: pd.DataFrame) -> dict | None:
    """单只股票的前复权日线 → 特征。数据不足 130 个交易日（次新或停牌太久）返回 None。"""
    if df is None or len(df) < 130:
        return None
    c, amt = df["close"], df["amount"]
    ret = c.pct_change()
    hi60 = float(df["high"].tail(60).max())
    close = float(c.iloc[-1])
    ma20, ma60, ma120 = float(c.tail(20).mean()), float(c.tail(60).mean()), float(c.tail(120).mean())
    vol_60 = float(ret.tail(60).std() * 250 ** 0.5)
    f = {"close": close, "listed_days": float(len(df)),
         "chg_1d": c.iloc[-1] / c.iloc[-2] - 1, "chg_5d": c.iloc[-1] / c.iloc[-6] - 1,
         "ret_20": c.iloc[-1] / c.iloc[-21] - 1, "ret_60": c.iloc[-1] / c.iloc[-61] - 1,
         "ret_120": c.iloc[-1] / c.iloc[-121] - 1,
         "vol_20": ret.tail(20).std() * 250 ** 0.5, "vol_60": vol_60,
         "ma20": ma20, "ma60": ma60, "ma120": ma120,
         "hi60": hi60, "dd_60": close / hi60 - 1 if hi60 > 0 else float("nan"),
         "near_high_60": close >= 0.97 * hi60,
         "avg_amount": float(amt.tail(20).mean()),
         "amt_ratio": float(amt.tail(5).mean() / amt.tail(20).mean()) if amt.tail(20).mean() > 0 else 0.0,
         "stop_prob": stop_prob(vol_60), "one_lot_value": round(close * 100, 2),
         "dist_ma20": close / ma20 - 1 if ma20 > 0 else float("nan"),
         "dist_ma60": close / ma60 - 1 if ma60 > 0 else float("nan"),
         "dist_high60": close / hi60 - 1 if hi60 > 0 else float("nan")}
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


def style_of(channels: str) -> str:
    """把命中通道映射成候选风格；持仓/观察名单优先，便于单独复核。"""
    s = set(str(channels or "").split("|"))
    if "holding" in s:
        return "holding"
    if "watchlist" in s:
        return "watchlist"
    for name in ("defensive", "pullback", "breakout", "sector", "trend", "momentum"):
        if name in s:
            return "lowvol" if name == "defensive" else name
    return "other"


def _trigger(row: pd.Series) -> tuple:
    """返回 (trigger_type, trigger_level, trigger_text)。触发位只用于研究排队，不是下单指令。"""
    style = str(row.get("style") or style_of(row.get("channels")))
    close = _nan(row.get("close"))
    if style == "breakout":
        level = _nan(row.get("hi60")) or close
        return "breakout_high", level, f"收盘站上 {level:.2f}（60 日高）"
    if style in ("pullback", "lowvol"):
        level = _nan(row.get("ma60")) or close
        return "pullback_to_ma60", level, f"回踩 {level:.2f}（MA60）不破"
    level = _nan(row.get("ma20")) or close
    return "pullback_to_ma20", level, f"回踩 {level:.2f}（MA20）"


def _assess(row: pd.Series, regime: str) -> dict:
    """单只股票的分诊：动作、触发位、风险标签。纯函数，便于单测。"""
    ch = set(str(row.get("channels") or "").split("|"))
    style = str(row.get("style") or style_of(row.get("channels")))
    eligible = bool(row.get("eligible", True))
    close = _nan(row.get("close"))
    pe = _nan(row.get("pe"))
    ret20, vol = _nan(row.get("ret_20")), _nan(row.get("vol_60"))
    sp = _nan(row.get("stop_prob"))
    # 极端/不合规：只作回避/降权，不直接买入；持仓仍进复核表
    over = ((math.isfinite(ret20) and ret20 > 0.30) or (math.isfinite(vol) and vol > 0.60)
            or (math.isfinite(pe) and (pe > 100 or pe <= 0)))
    typ, level, trigger = _trigger(row)
    dist = close / level - 1 if level > 0 and math.isfinite(close) else float("nan")

    if "holding" in ch:
        return {"action": "holding", "action_note": "持仓：每晚复核持有/减仓/卖出",
                "trigger_type": "holding", "trigger_level": level, "trigger": "持仓复核（止损/时间止损由代码执行）",
                "dist_trigger": float("nan")}
    if "watchlist" in ch:
        # 观察名单以 data/watchlist.json 的结构化触发条件为准（见 md 的“观察名单触发状态”），
        # 主表只做跟踪展示，避免用 MA20/MA60 触发位误判“可行动”。
        return {"action": "watching", "action_note": "观察名单跟踪中（触发状态见下节）",
                "trigger_type": typ, "trigger_level": level, "trigger": trigger, "dist_trigger": dist}
    if not eligible:
        return {"action": "avoid", "action_note": "不合规：下单会被风控拒绝",
                "trigger_type": typ, "trigger_level": level, "trigger": trigger, "dist_trigger": dist}
    if over:
        return {"action": "avoid", "action_note": "涨幅/波动/估值极端，等回调或降权",
                "trigger_type": typ, "trigger_level": level, "trigger": trigger, "dist_trigger": dist}
    if math.isfinite(sp) and sp > 0.50:
        return {"action": "avoid", "action_note": f"触损概率 {sp:.0%} 偏高，等波动收敛",
                "trigger_type": typ, "trigger_level": level, "trigger": trigger, "dist_trigger": dist}
    if typ == "breakout_high":
        if dist >= -0.005:
            return {"action": "ready", "action_note": f"接近/站上突破位（距 {dist:+.1%}）",
                    "trigger_type": typ, "trigger_level": level, "trigger": trigger, "dist_trigger": dist}
        return {"action": "wait_breakout", "action_note": f"距突破位 {dist:+.1%}",
                "trigger_type": typ, "trigger_level": level, "trigger": trigger, "dist_trigger": dist}
    if dist < -0.02:
        return {"action": "watch", "action_note": f"已跌破触发位 {dist:+.1%}，等重新站上再看",
                "trigger_type": typ, "trigger_level": level, "trigger": trigger, "dist_trigger": dist}
    if dist <= 0.02:
        return {"action": "ready", "action_note": f"已到/接近触发位（距 {dist:+.1%}）",
                "trigger_type": typ, "trigger_level": level, "trigger": trigger, "dist_trigger": dist}
    if style in ("pullback", "lowvol") or dist <= 0.05:
        return {"action": "wait_pullback", "action_note": f"距触发位还有 {dist:+.1%}",
                "trigger_type": typ, "trigger_level": level, "trigger": trigger, "dist_trigger": dist}
    return {"action": "watch", "action_note": f"距触发位 {dist:+.1%}，暂不进入研究",
            "trigger_type": typ, "trigger_level": level, "trigger": trigger, "dist_trigger": dist}


def triage(pool: pd.DataFrame, regime: str = "neutral", keep_style: bool = False) -> pd.DataFrame:
    """给候选池打上 action / trigger / priority，并按"可行动性 + regime 风格权重"排序。

    - 不再用 n_channels 作为第一排序键；n_channels 只保留为展示字段；
    - stop_prob 高的标的降权（同一风格里排在后面，极端值进"回避/降权"）；
    - regime 只改变研究顺序，不改变仓位或风控。
    """
    out = pool.copy()
    if not len(out):
        return out
    if (not keep_style) or ("style" not in out.columns):
        out["style"] = out["channels"].map(style_of)
    if "stop_prob" not in out.columns:
        out["stop_prob"] = out["vol_60"].map(stop_prob)
    if "one_lot_value" not in out.columns:
        out["one_lot_value"] = out["close"] * 100
    if "hi60" not in out.columns:
        # 兼容旧 features 缓存：用 dd_60 反推 60 日高，避免 breakout 触发位退化成收盘价
        out["hi60"] = out["close"] / (1 + out["dd_60"]) if "dd_60" in out.columns else out["close"]
    assessed = out.apply(lambda r: pd.Series(_assess(r, regime)), axis=1)
    out = pd.concat([out.drop(columns=[c for c in assessed.columns if c in out.columns]), assessed], axis=1)
    weights = REGIME_STYLE_WEIGHTS.get(regime, REGIME_STYLE_WEIGHTS["neutral"])
    out["priority"] = [
        weights.get(str(s), weights["other"]) + ACTION_BONUS.get(str(a), 0.0)
        - 4.0 * max(0.0, (_nan(sp) if math.isfinite(_nan(sp)) else 0.0) - 0.35)
        + 0.2 * min(int(nc) if pd.notna(nc) else 0, 3)
        for s, a, sp, nc in zip(out["style"], out["action"], out.get("stop_prob"), out.get("n_channels", [0] * len(out)))
    ]
    out["_order"] = out["action"].map(ACTION_ORDER).fillna(8).astype(int)
    out = out.sort_values(["_order", "priority"], ascending=[True, False]).drop(columns="_order").reset_index(drop=True)
    out["action_cn"] = out["action"].map(ACTION_CN).fillna(out["action"])
    return out


def assemble(feat: pd.DataFrame, chans: dict, holdings: list, watch: list) -> pd.DataFrame:
    """合并各通道、持仓与观察名单，标注命中通道；排序保留 n_channels/s_mom（供兼容），
    真正的研究顺序由 triage() 决定。"""
    hits = {}
    for ch, codes in list(chans.items()) + [("holding", holdings), ("watchlist", watch)]:
        for c in codes:
            if c not in hits:
                hits[c] = []
            if ch not in hits[c]:
                hits[c].append(ch)
    rows = feat[feat["code"].isin(hits)].copy()
    rows["channels"] = rows["code"].map(lambda c: "|".join(hits[c]))
    rows["n_channels"] = rows["code"].map(lambda c: len(hits[c]))
    rows["s_mom"] = rows["ret_20"] / rows["vol_20"].where(rows["vol_20"] > 0)
    return rows.sort_values(["n_channels", "s_mom"], ascending=False).reset_index(drop=True)


def history_counts(date: str, days: int = 10) -> dict:
    """统计近 N 个交易日进池次数（不含当天），用于 persist_days。"""
    d = pool_dir()
    if not d.exists():
        return {}
    files = sorted(p for p in d.glob("*.csv") if p.stem < date)[-days:]
    counts = {}
    for p in files:
        try:
            df = pd.read_csv(p, dtype={"code": str})
        except Exception:
            continue
        if "code" not in df.columns:
            continue
        for c in df["code"].dropna():
            counts[c] = counts.get(c, 0) + 1
    return counts


def load_watchlist_items() -> list:
    p = watchlist_path()
    if not p.exists():
        return []
    try:
        items = json.loads(p.read_text(encoding="utf-8")).get("items", [])
    except (json.JSONDecodeError, OSError, AttributeError):
        return []
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and i.get("code")
            and i.get("status", "watching") in ("watching", "researched")]


def load_watchlist() -> list:
    return [i["code"] for i in load_watchlist_items()]


def watch_status(items: list, feat: pd.DataFrame) -> list:
    """评估观察名单的结构化触发条件。无法解析的保留原文并标注"需人工复核"。"""
    if feat is None or not len(feat) or "code" not in feat.columns:
        return []
    rows = feat.set_index("code")
    out = []
    for it in items or []:
        code = str(it.get("code"))
        item = {"code": code, "name": it.get("name", ""), "status": it.get("status", "watching"),
                "trigger": it.get("trigger", ""), "hit": None, "note": "", "dist": None}
        if code not in rows.index:
            item["note"] = "不在今日特征中（停牌/数据缺失）"
            out.append(item)
            continue
        r = rows.loc[code]
        close = _nan(r.get("close"))
        level = _nan(it.get("trigger_level"))
        typ = str(it.get("trigger_type") or "").strip()
        if not math.isfinite(level):
            m = re.search(r"(\d+(?:\.\d+)?)", str(it.get("trigger", "")))
            if m:
                level = float(m.group(1))
                if not typ:
                    typ = "price_below" if level <= close else "price_above"
        if not math.isfinite(level) or not typ:
            item["note"] = "触发条件未结构化，需人工复核"
            out.append(item)
            continue
        item["dist"] = close / level - 1 if level > 0 else None
        if typ in ("price_above", "breakout_high"):
            item["hit"] = close >= level
        elif typ in ("price_below", "pullback_to_ma60", "pullback_to_ma20"):
            item["hit"] = close <= level
        else:
            item["note"] = f"未知触发类型 {typ}，需人工复核"
        out.append(item)
    return out


def _fmt_pct(x) -> str:
    v = _nan(x)
    return f"{v:+.1%}" if math.isfinite(v) else "-"


def _fmt_num(x, digits=1) -> str:
    v = _nan(x)
    return f"{v:.{digits}f}" if math.isfinite(v) else "-"


def to_markdown(date: str, pool: pd.DataFrame, chans: dict, stats: dict, regime: str = "neutral",
                watch_items: list | None = None) -> str:
    if "action" not in pool.columns:
        pool = triage(pool, regime)

    def row(r):
        ind = r.get("industry") if pd.notna(r.get("industry")) else "-"
        nmc = _fmt_num(r.get("nmc") / 1e8, 0) if pd.notna(r.get("nmc")) else "-"
        pe = _fmt_num(r.get("pe"))
        flag = "" if bool(r.get("eligible", True)) else "（不合规）"
        return (f"| {r.code} | {r['name']}{flag} | {r.get('style', '-')} | {ind} | {r.get('action_cn', '-')} | "
                f"{_fmt_num(r.get('close'), 2)} | {_fmt_pct(r.get('ret_20'))} | {_fmt_pct(r.get('vol_60'))} | "
                f"{_fmt_num(r.get('stop_prob') * 100, 0) + '%' if math.isfinite(_nan(r.get('stop_prob'))) else '-'} | "
                f"{pe} | {nmc} | {_fmt_num(r.get('dist_trigger') * 100, 1) + '%' if math.isfinite(_nan(r.get('dist_trigger'))) else '-'} | "
                f"{r.get('trigger', '-')} | {int(r.get('persist_days', 0) or 0)} | {r.get('channels', '')} |")

    head = ("| 代码 | 名称 | 风格 | 行业 | 动作 | 收盘 | 20日 | 60日波动 | 触损概率* | PE | 流通市值亿 | 距触发 | 触发位 | 近10日进池 | 通道 |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")

    def table(sub, limit=None):
        if sub is None or not len(sub):
            return ["无。"]
        sub = sub.head(limit) if limit else sub
        return [head] + [row(r) for _, r in sub.iterrows()]

    def diverse(sub, limit, per_style=3, per_industry=2):
        """展示用去重：同一风格/行业最多 N 只，避免“可行动”里全是同一类；完整名单在 CSV。"""
        if sub is None or not len(sub):
            return sub
        keep, sc, ic = [], {}, {}
        for _, r in sub.iterrows():
            st, ind = str(r.get("style")), r.get("industry")
            ind = str(ind) if pd.notna(ind) else None
            if sc.get(st, 0) >= per_style or (ind and ic.get(ind, 0) >= per_industry):
                continue
            keep.append(r)
            sc[st] = sc.get(st, 0) + 1
            if ind:
                ic[ind] = ic.get(ind, 0) + 1
            if len(keep) >= limit:
                break
        return pd.DataFrame(keep)

    def section(title, sub, limit=None):
        return ["", f"## {title}（{0 if sub is None else len(sub)} 只）", ""] + table(sub, limit)

    regime_cn = REGIME_CN.get(regime, regime)
    lines = [f"# {date} 主策略候选池（{len(pool)} 只，{regime_cn}）", "",
             f"全市场 {stats.get('spot', '-')} 只 → 基础过滤 {stats.get('base', '-')} 只 → "
             f"流动性预过滤 {stats.get('pre', '-')} 只 → 有效特征 {stats.get('feat', '-')} 只 → "
             f"合规 {stats.get('eligible', '-')} 只 → 多路召回 {len(pool)} 只。",
             "数据：新浪前复权日线、新浪行业板块（行业/PE/流通市值）。**排序只决定研究顺序，不代表买入**；"
             "*触损概率 = 漂移率为 0 时 20 日内触及 -8% 止损的近似概率，仅用于风险适配，不是预测。", "",
             "## 今日状态", "",
             f"- 市场状态：**{regime_cn}**",
             f"- 分诊规则：{REGIME_HINT.get(regime, REGIME_HINT['neutral'])}",
             "- 动作分布：" + "，".join(f"{ACTION_CN[k]} {int((pool['action'] == k).sum())}"
                                        for k in ("ready", "wait_pullback", "wait_breakout", "watch", "watching", "holding", "avoid")),
             "- 说明：v1.1 起不再按“多通道共振”优先；多通道只在文末作参考。"]

    lines += section("今日可行动（展示按风格/行业去重，完整见 CSV）", diverse(pool[pool["action"] == "ready"], 12))
    lines += section("等回调（触发位 = MA60 支撑；展示去重）", diverse(pool[pool["action"] == "wait_pullback"], 12))
    lines += section("等突破（触发位 = 60 日高）", pool[pool["action"] == "wait_breakout"], limit=10)
    lines += section("观望", pool[pool["action"] == "watch"], limit=10)
    lines += section("观察名单（今日入池）", pool[pool["action"] == "watching"], limit=10)
    lines += section("持仓复核", pool[pool["action"] == "holding"], limit=10)
    lines += section("回避/降权（不合规或极端）", pool[pool["action"] == "avoid"], limit=12)

    # 观察名单触发状态
    if watch_items is None:
        watch_items = load_watchlist_items()
    ws = watch_status(watch_items, pool)
    lines += ["", f"## 观察名单触发状态（{len(ws)} 只）", ""]
    if not ws:
        lines.append("观察名单为空。")
    else:
        lines += ["| 代码 | 名称 | 状态 | 触发条件 | 当前距离 | 是否触发 | 备注 |", "|---|---|---|---|---|---|---|"]
        for w in ws:
            dist = _fmt_pct(w["dist"]) if w["dist"] is not None else "-"
            hit = "是" if w["hit"] is True else ("否" if w["hit"] is False else "-")
            lines.append(f"| {w['code']} | {w['name']} | {w['status']} | {w['trigger']} | {dist} | {hit} | {w['note']} |")

    multi = pool[pool["n_channels"] >= 2]
    lines += ["", f"## 多通道共振（仅供了解，不再作为优先依据）（{len(multi)} 只）", ""] + table(multi, limit=10)
    lines += ["", "## 通道说明", ""] + [f"- **{k}**：{v}（{len(chans.get(k, []))} 只）" for k, v in CHANNELS.items()
                                       if k in chans or k in ("holding", "watchlist")]
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


def _stats_from_md(date: str) -> dict:
    """从旧版 md 解析漏斗统计；解析失败返回空 dict（只影响表头展示）。"""
    p = pool_dir() / f"{date}.md"
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = re.search(r"全市场 (\d+) 只 → 基础过滤 (\d+) 只 → 流动性预过滤 (\d+) 只 → "
                  r"有效特征 (\d+) 只 → 合规 (\d+) 只", text)
    if not m:
        return {}
    return {"spot": int(m.group(1)), "base": int(m.group(2)), "pre": int(m.group(3)),
            "feat": int(m.group(4)), "eligible": int(m.group(5))}


def _write_pool(date: str, feat: pd.DataFrame, holdings: list, watch: list, pcfg: dict,
                regime: str, stats: dict, log=print) -> pd.DataFrame:
    chans = channels(feat, pcfg)
    out = assemble(feat, chans, holdings, watch)
    persist = history_counts(date)
    out["persist_days"] = out["code"].map(persist).fillna(0).astype(int)
    out = triage(out, regime)
    path = pool_dir() / f"{date}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    (pool_dir() / f"{date}.md").write_text(
        to_markdown(date, out, chans, stats, regime=regime, watch_items=load_watchlist_items()), encoding="utf-8")
    log(f"候选池完成：{len(out)} 只（" + "，".join(f"{k} {len(v)}" for k, v in chans.items()) + f"），已保存 {path.name}")
    return out


def run(date: str, market, rcfg: dict, pcfg: dict, max_lot_value: float, holdings: list,
        log=print, force: bool = False, regime: str = "neutral") -> pd.DataFrame:
    path = pool_dir() / f"{date}.csv"
    if path.exists() and not force:
        cached = pd.read_csv(path, dtype={"code": str})
        if {"action", "stop_prob", "persist_days"} <= set(cached.columns):
            return cached
        # v1.0 旧格式：优先用当天 features 缓存重建分诊版本，避免为了补字段重新联网拉 2500 只日线
        cache = config.cache_dir() / "pool" / f"{date}_features.csv"
        if cache.exists():
            feat = pd.read_csv(cache, dtype={"code": str})
            stats = _stats_from_md(date) or {"spot": "-", "base": "-", "pre": "-",
                                             "feat": len(feat), "eligible": int(feat["eligible"].sum())}
            log("候选池：检测到旧版 CSV，使用当天 features 缓存重建 v1.1 分诊（不联网）")
            return _write_pool(date, feat, holdings, load_watchlist(), pcfg, regime, stats, log)
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
    watch = load_watchlist()
    keep = set(holdings) | set(watch)
    pre = spot[(spot["amount"] >= pcfg["prefilter_min_amount"]) | spot["code"].isin(keep)]
    pre = pre.sort_values("amount", ascending=False).head(pcfg["prefilter_max"])
    # 持仓与观察名单无论成交额多少都必须进池复核，不能被 head 截断
    rest = spot[spot["code"].isin(keep) & ~spot["code"].isin(pre["code"])]
    if len(rest):
        pre = pd.concat([pre, rest], ignore_index=True)
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

    stats = {"spot": n_spot, "base": n_base, "pre": len(pre), "feat": len(feat), "eligible": int(feat["eligible"].sum())}
    return _write_pool(date, feat, holdings, watch, pcfg, regime, stats, log)
