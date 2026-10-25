"""影子候选引擎 v2（2026-10-25 用户批准的 P-002 阶段 2）。

定位：**只读影子**。每天与 v1 候选池并列生成，供研究任务记录 5 只 shadow shortlist，不用于 main 下单。
目标不是"选出更强的股票"，而是修正 v1 的结构问题：
- v1 的 4/6 通道是同一笔趋势/动量交易（日度超额相关 0.84–0.94），本模块改成 5 条正交风格；
- v1 按 n_channels 排序、把 60 日最差的一组放最前，本模块按 regime 配额 + 行业中性 + 风险适配排序；
- v1 候选信息只有价量+PE，本模块输出 40–60 只、每行业 ≤3、带触发位/风险标签的候选。

风格（均为离线可算的价量/估值代理，不依赖被代理挡住的基本面接口）：
- lowvol：收盘>MA120、PE 0–30（缺失允许）、60 日波动最低；防守核心，但单独限额（风格暴露）；
- squeeze：5/20 日成交额比 <0.8 且收盘>MA60；回测里唯一有增量信息的替代通道；
- pullback：中期趋势向上、回撤 8–20%、近 5 日下跌、贴近 MA60 支撑；
- breakout：接近 60 日新高 + 放量，但剔除 20 日涨幅 >30% 或 60 日波动 >50% 的极端；
- reversal：长期趋势向上、20 日超跌（全市场后 25%），避免接飞刀极端（ret_20 ≥ -25%）。

输出：data/pool_v2/<日期>.csv / .md（不入 main 的订单链路）。
"""
import math

import pandas as pd

from . import config, pool

STYLES = ("lowvol", "squeeze", "pullback", "breakout", "reversal")
STYLE_CN = {"lowvol": "低波", "squeeze": "缩量整理", "pullback": "回调", "breakout": "突破", "reversal": "反转",
            "holding": "持仓", "watchlist": "观察", "other": "补位"}

DEFAULT_CFG = {
    "total": 56,
    "per_industry": 3,
    "min_industries": 8,
    "stop_prob_max": 0.45,
    "vol_max": 0.60,
    "ret20_max": 0.30,
    "max_lot_value": 5000,     # 研究目标仓位 5%（10 万 × 5%）；P-001 若通过可改为 10000
    "weights": {
        "defense": {"lowvol": 35, "pullback": 25, "squeeze": 20, "breakout": 10, "reversal": 10},
        "neutral": {"lowvol": 20, "squeeze": 20, "pullback": 25, "breakout": 20, "reversal": 15},
        "offense": {"breakout": 30, "squeeze": 20, "pullback": 20, "lowvol": 15, "reversal": 15},
    },
}


def pool2_dir():
    return config.home() / "data" / "pool_v2"


def quotas(cfg: dict, regime: str) -> dict:
    """按 regime 权重把 total 拆成各风格名额；用最大余数法保证合计等于 total。"""
    weights = cfg["weights"].get(regime) or cfg["weights"]["neutral"]
    total = int(cfg["total"])
    s = sum(weights.values()) or 1
    raw = {k: v / s * total for k, v in weights.items()}
    out = {k: int(math.floor(v)) for k, v in raw.items()}
    rest = total - sum(out.values())
    for k in sorted(raw, key=lambda x: raw[x] - out[x], reverse=True)[:rest]:
        out[k] += 1
    return out


def _industry_counts(rows: pd.DataFrame) -> dict:
    counts = {}
    for ind in rows.get("industry", pd.Series(dtype=object)):
        if pd.notna(ind):
            counts[str(ind)] = counts.get(str(ind), 0) + 1
    return counts


def _take(df: pd.DataFrame, n: int, used: set, ind_count: dict, cap: int) -> list:
    """从已排序的 df 里取最多 n 只，遵守每行业上限与去重；返回追加的代码列表。"""
    picked = []
    if n <= 0 or df is None or not len(df):
        return picked
    for code, ind in zip(df["code"], df.get("industry", pd.Series([None] * len(df)))):
        if len(picked) >= n:
            break
        code = str(code)
        if code in used:
            continue
        ind = str(ind) if pd.notna(ind) else None
        if ind and ind_count.get(ind, 0) >= cap:
            continue
        picked.append(code)
        used.add(code)
        if ind:
            ind_count[ind] = ind_count.get(ind, 0) + 1
    return picked


def _hits(chans: dict) -> dict:
    hits = {}
    for ch, codes in (chans or {}).items():
        for c in codes:
            hits.setdefault(str(c), []).append(ch)
    return hits


def build(feat: pd.DataFrame, chans: dict, holdings: list, watch: list, cfg: dict | None = None,
          regime: str = "neutral") -> pd.DataFrame:
    """纯函数：特征表 → v2 候选池（含 action/trigger/priority）。不取数、不写盘。"""
    cfg = DEFAULT_CFG | (cfg or {})
    total = int(cfg["total"])
    cap = int(cfg["per_industry"])
    f = feat.copy()
    if "eligible" not in f.columns:
        f["eligible"] = True
    f["eligible"] = f["eligible"].astype(bool)
    for col, default in (("stop_prob", float("nan")), ("one_lot_value", float("nan")),
                         ("vol_60", float("nan")), ("ret_20", float("nan")),
                         ("amt_ratio", float("nan")), ("dd_60", float("nan")),
                         ("chg_5d", float("nan")), ("near_high_60", False),
                         ("ma20", float("nan")), ("ma60", float("nan")), ("ma120", float("nan"))):
        if col not in f.columns:
            f[col] = default
    # 兼容旧 features 缓存（v1.0 没有这些列）：全部重算，保证影子池口径一致
    f["stop_prob"] = f["vol_60"].map(pool.stop_prob)
    f["one_lot_value"] = f["close"] * 100
    pe = pd.to_numeric(f.get("pe", pd.Series(float("nan"), index=f.index)), errors="coerce")
    f["pe"] = pe
    # 硬风险过滤（持仓/观察名单不走这条；它们单独加入复核）
    keep = (f["eligible"]
            & (f["stop_prob"].isna() | (f["stop_prob"] <= cfg["stop_prob_max"]))
            & (f["vol_60"].isna() | (f["vol_60"] <= cfg["vol_max"]))
            & (f["ret_20"].isna() | (f["ret_20"] <= cfg["ret20_max"]))
            & (pe.isna() | pe.between(0, 100))
            & (f["one_lot_value"].isna() | (f["one_lot_value"] <= cfg["max_lot_value"])))
    base = f[keep].copy()
    bpe = pd.to_numeric(base["pe"], errors="coerce")
    # 行业缺失不阻塞，但排到后面；行业已覆盖的优先
    base["_ind_ok"] = base.get("industry", pd.Series([None] * len(base))).notna().astype(int)

    lowvol = base[(base["close"] > base["ma120"]) & (bpe.isna() | bpe.between(0, 30))].sort_values(
        ["vol_60", "stop_prob"], ascending=[True, True])
    squeeze = base[(base["amt_ratio"] < 0.8) & (base["close"] > base["ma60"])].sort_values("amt_ratio")
    pull = base[(base["close"] > base["ma120"]) & (base["ma60"] > base["ma120"])
                & base["dd_60"].between(-0.20, -0.08) & (base["chg_5d"] < 0)].copy()
    pull["_dist"] = (pull["close"] / pull["ma60"] - 1).abs()
    pull = pull.sort_values(["_dist", "vol_60"], ascending=[True, True])
    brk = base[(base["near_high_60"].astype(bool)) & (base["amt_ratio"] >= 1.5)
               & (base["ret_20"] <= 0.30) & (base["vol_60"] <= 0.50)].sort_values("amt_ratio", ascending=False)
    rv = base[(base["close"] > base["ma120"]) & (base["ret_20"] <= base["ret_20"].quantile(0.25))
              & (base["ret_20"] >= -0.25)].sort_values("ret_20")
    sources = {"lowvol": lowvol, "squeeze": squeeze, "pullback": pull, "breakout": brk, "reversal": rv}
    q = quotas(cfg, regime)
    used, ind_count, picked = set(), {}, []
    for style in STYLES:  # lowvol 在前：防守期优先；重复命中时归入先出现的风格
        for code in _take(sources[style], q.get(style, 0), used, ind_count, cap):
            picked.append((code, style))
    # 名额不足（行业上限/过滤太严）时，用剩余 base 补足；仍守行业上限。
    # 补位排序随 regime 变化：防守偏低波/低触损，其它状态偏风险调整动量，避免把进攻池补成低波池。
    if len(picked) < total:
        rest = base[~base["code"].isin(used)].copy()
        rest["_s_mom"] = rest["ret_20"] / rest["vol_20"].where(rest["vol_20"] > 0)
        if regime == "defense":
            fill = rest.sort_values(["stop_prob", "vol_60"], ascending=[True, True])
        else:
            fill = rest.sort_values(["_s_mom", "stop_prob"], ascending=[False, True])
        for code in _take(fill, total - len(picked), used, ind_count, cap):
            picked.append((code, "other"))

    # 持仓与观察名单必须进影子池（复核/跟踪），不受风险过滤
    for code, style in [(c, "holding") for c in (holdings or [])] + [(c, "watchlist") for c in (watch or [])]:
        # 即使已被某个风格选中，也要标成持仓/观察，确保进入复核表
        picked.append((str(code), style))
        used.add(str(code))

    rows = f[f["code"].isin([c for c, _ in picked])].copy()
    style_map = {c: s for c, s in picked}
    rows["style"] = rows["code"].map(style_map)
    hits = _hits(chans)
    rows["channels"] = rows["code"].map(lambda c: "|".join(hits.get(c, []) + ([style_map[c]] if style_map.get(c) in ("holding", "watchlist") else [])))
    rows["n_channels"] = rows["channels"].map(lambda s: len([x for x in str(s).split("|") if x]))
    rows["engine"] = "v2"
    rows = pool.triage(rows, regime, keep_style=True)
    return rows.reset_index(drop=True)


def _row(r) -> str:
    ind = r.get("industry") if pd.notna(r.get("industry")) else "-"
    pe = pool._fmt_num(r.get("pe"))
    sp = pool._fmt_num(r.get("stop_prob") * 100, 0)
    sp = sp + "%" if sp != "-" else "-"
    dist = pool._fmt_num(r.get("dist_trigger") * 100, 1)
    dist = dist + "%" if dist != "-" else "-"
    return (f"| {r.code} | {r['name']} | {STYLE_CN.get(str(r.get('style')), r.get('style'))} | {ind} | "
            f"{r.get('action_cn', '-')} | {pool._fmt_num(r.get('close'), 2)} | {pool._fmt_pct(r.get('ret_20'))} | "
            f"{pool._fmt_pct(r.get('vol_60'))} | {sp} | {pe} | {dist} | {r.get('trigger', '-')} | "
            f"{r.get('action_note', '')} | {r.get('channels', '')} |")


HEAD = ("| 代码 | 名称 | 风格 | 行业 | 动作 | 收盘 | 20日 | 60日波动 | 触损概率 | PE | 距触发 | 触发位 | 分诊备注 | 通道 |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")


def to_markdown(date: str, rows: pd.DataFrame, cfg: dict, regime: str, stats: dict | None = None) -> str:
    stats = stats or {}
    q = quotas(cfg, regime)
    regime_cn = pool.REGIME_CN.get(regime, regime)
    quota_text = "，".join(f"{STYLE_CN[k]} {v}" for k, v in q.items())
    actual_text = "，".join(f"{STYLE_CN.get(k, k)} {int((rows['style'] == k).sum())}"
                           for k in STYLES + ("holding", "watchlist", "other"))
    lines = [f"# {date} 影子候选池 v2（{len(rows)} 只，{regime_cn}）", "",
             "> **实验属性**：只与 v1 并列记录，不进入 main 下单链路；main 仍用 data/pool/。",
             "> 目的：验证“风格分层 + regime 配额 + 风险适配”是否比 v1 的“多通道技术面召回”更好；"
             "20–30 个交易日后比较前向收益，再决定是否替换。", "",
             f"- 配额：{quota_text}（合计 {sum(q.values())}）",
             f"- 实际：{actual_text}",
             f"- 行业数：{rows['industry'].nunique() if 'industry' in rows else 0}；过滤后候选域：{stats.get('base', '-')} 只",
             "- 触损概率 = 漂移率为 0 时 20 日内触及 -8% 止损的近似概率；只用于风险适配。", ""]

    def diverse(sub, limit, per_style=3, per_industry=2):
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
        sub = sub.head(limit) if limit and sub is not None else sub
        out = ["", f"## {title}（{0 if sub is None else len(sub)} 只）", ""]
        if sub is None or not len(sub):
            return out + ["无。"]
        return out + [HEAD] + [_row(r) for _, r in sub.iterrows()]

    lines += section("可立即研究（展示按风格/行业去重，完整见 CSV）",
                     diverse(rows[rows["action"] == "ready"], 12))
    lines += section("等触发（展示去重）",
                     diverse(rows[rows["action"].isin(["wait_pullback", "wait_breakout", "watching"])], 12))
    lines += section("观望", rows[rows["action"] == "watch"], limit=15)
    lines += section("持仓/观察", rows[rows["action"].isin(["holding", "watching"])], limit=12)
    return "\n".join(lines) + "\n"


def run(date: str, feat: pd.DataFrame, chans: dict, regime: str, holdings: list, watch: list,
        cfg: dict, log=print, force: bool = False) -> pd.DataFrame:
    path = pool2_dir() / f"{date}.csv"
    if path.exists() and not force:
        return pd.read_csv(path, dtype={"code": str})
    rows = build(feat, chans, holdings, watch, cfg, regime)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows.to_csv(path, index=False)
    stats = {"base": int(feat["eligible"].sum()) if "eligible" in feat.columns else len(feat)}
    (pool2_dir() / f"{date}.md").write_text(to_markdown(date, rows, cfg, regime, stats), encoding="utf-8")
    log(f"影子候选池 v2：{len(rows)} 只，已保存 {path.name}")
    return rows
