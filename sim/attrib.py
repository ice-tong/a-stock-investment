"""策略归因：给策略进化官用的确定性统计。数字全部由代码从账本和冻结订单算出，Agent 只负责解读。

- 账户层：收益、回撤、Calmar、平均个股仓位，以及主策略相对 etf / random / quant 对照组的超额；
- 交易层：把 main 的成交按"一次完整持仓"（首次买入到清仓）聚合，回连到当时的冻结订单，
  按命中通道、置信度、策略版本、离场原因分组统计胜率与平均盈亏；
- 证据门槛：已平仓笔数是否达到提出参数提案的最低样本（risk 规则之外的唯一硬门槛）。

输出 evolution/attribution/<日期>.md（入库，作为进化提案的证据底稿）。
"""
import json
import re

from . import config, report
from .ledger import Account, all_accounts

MIN_TRADES_FOR_PROPOSAL = 15  # 与 docs/模拟交易系统设计.md 5.2 一致：一条参数提案至少 15 笔相关交易
CONTROLS = ("etf", "etf_band", "random", "quant")
# 对照口径：etf 固定 98% 仓位，etf_band 跟当日仓位区间上沿（和 main 无草稿时的停泊逻辑一致）。
# main − etf_band 是选股净贡献，etf_band − etf 是仓位控制贡献。


def evolution_dir():
    return config.home() / "evolution"


def order_index(account: str) -> dict:
    """order_id -> 冻结订单（附 trade_date 与该日的 strategy_version）。"""
    idx = {}
    for f in sorted(config.orders_dir().glob(f"*/{account}.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        meta = doc.get("meta") or {}
        for o in doc.get("orders", []):
            if o.get("id"):
                idx[o["id"]] = o | {"trade_date": doc["trade_date"],
                                    "strategy_version": o.get("strategy_version") or doc.get("strategy_version"),
                                    "model": o.get("model") or meta.get("model") or "未标注"}
    return idx


def confidence_of(o: dict) -> str:
    if o.get("confidence"):
        return str(o["confidence"])
    m = re.search(r"置信度\s*[:：]?\s*([高中低])", o.get("reason", ""))
    return m.group(1) if m else "未标注"


def episodes(acct, idx: dict) -> list:
    """个股的完整持仓：首次买入开启，清仓关闭；未平仓的用最近结算价计浮动盈亏。"""
    open_, done = {}, []
    for t in acct.trades():
        if t["side"] not in ("buy", "sell") or not t["code"] or t["code"].startswith(("sh5", "sz15", "sz16")):
            continue
        code = t["code"]
        if t["side"] == "buy":
            ep = open_.get(code)
            if ep is None:
                o = idx.get(t["order_id"], {})
                ep = open_[code] = {"code": code, "name": t["name"], "entry_date": t["date"], "invested": 0.0,
                                    "realized": 0.0, "channels": o.get("channels") or "未标注",
                                    "confidence": confidence_of(o), "version": o.get("strategy_version") or "未标注",
                                    "model": o.get("model") or "未标注",
                                    "entry_reason": o.get("reason", "")}
            ep["invested"] += float(t["amount"] or 0) + sum(float(t[k] or 0) for k in ("commission", "transfer_fee"))
        else:
            ep = open_.get(code)
            if ep is None:
                continue
            ep["realized"] += float(t["realized_pnl"] or 0)
            if t["closed"] == "True":
                ep.update(exit_date=t["date"], exit_reason=t["reason_type"] or "未标注", closed=True,
                          pnl=ep["realized"])
                done.append(open_.pop(code))
    for code, ep in open_.items():
        pos = acct.positions.get(code)
        unreal = pos["shares"] * pos["last_price"] - pos["cost"] if pos else 0.0
        ep.update(closed=False, exit_date="", exit_reason="持有中", pnl=ep["realized"] + unreal)
        done.append(ep)
    for ep in done:
        ep["pnl_pct"] = ep["pnl"] / ep["invested"] if ep["invested"] else 0.0
    return done


def group_stats(eps: list, key) -> list:
    """key(ep) -> 分组名（可返回多个，如命中多个通道）。只统计已平仓。"""
    groups = {}
    for ep in eps:
        if not ep["closed"]:
            continue
        for k in (key(ep) if isinstance(key(ep), list) else [key(ep)]):
            groups.setdefault(k, []).append(ep["pnl_pct"])
    rows = []
    for k, xs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        wins = [x for x in xs if x > 0]
        losses = [x for x in xs if x <= 0]
        payoff = (sum(wins) / len(wins)) / abs(sum(losses) / len(losses)) if wins and losses and sum(losses) else None
        rows.append({"group": k, "n": len(xs), "win_rate": len(wins) / len(xs), "avg": sum(xs) / len(xs),
                     "payoff": payoff, "enough": len(xs) >= MIN_TRADES_FOR_PROPOSAL})
    return rows


def account_rows() -> list:
    rows = []
    for acct in all_accounts():
        m = report.metrics(acct)
        nav = acct.nav()
        sw = [float(r["stock_weight"] or 0) for r in nav]
        rows.append({"name": acct.name, "strategy": acct.state["strategy"], "days": m.get("days", 0),
                     "total": m.get("total"), "mdd": m.get("mdd"), "calmar": m.get("calmar"),
                     "stock_weight": sum(sw) / len(sw) if sw else None, "sells": m.get("sells", 0),
                     "win_rate": m.get("win_rate"), "costs": m.get("costs", 0.0)})
    return rows


def build(date: str) -> dict:
    accts = {r["name"]: r for r in account_rows()}
    main = Account("main") if "main" in accts else None
    eps = episodes(main, order_index("main")) if main else []
    closed = [e for e in eps if e["closed"]]
    excess = {}
    if main:
        for c in CONTROLS:
            if c in accts and accts[c]["total"] is not None and accts["main"]["total"] is not None:
                excess[c] = accts["main"]["total"] - accts[c]["total"]
    # 把"选股"和"仓位控制"拆开：main − etf_band 只看选股，etf_band − etf 只看按 regime 调仓位
    selection = positioning = None
    if main:
        t = {k: v["total"] for k, v in accts.items()}
        if t.get("main") is not None and t.get("etf_band") is not None:
            selection = t["main"] - t["etf_band"]
        if t.get("etf_band") is not None and t.get("etf") is not None:
            positioning = t["etf_band"] - t["etf"]
    return {"date": date, "accounts": list(accts.values()), "excess": excess,
            "selection": selection, "positioning": positioning, "episodes": eps,
            "n_closed": len(closed), "gate": len(closed) >= MIN_TRADES_FOR_PROPOSAL,
            "by_channel": group_stats(eps, lambda e: e["channels"].split("|")),
            "by_confidence": group_stats(eps, lambda e: e["confidence"]),
            "by_version": group_stats(eps, lambda e: e["version"]),
            "by_model": group_stats(eps, lambda e: e["model"]),
            "by_exit": group_stats(eps, lambda e: e["exit_reason"])}


def to_markdown(r: dict) -> str:
    pct = report._pct
    lines = [f"# {r['date']} 策略归因", "",
             "> 由 `python -m sim attrib` 从账本与冻结订单计算，不含任何人工判断。", "",
             "## 账户对照", "", "| 账户 | 策略 | 天数 | 累计收益 | 最大回撤 | Calmar | 平均个股仓位 | 卖出笔数 | 胜率 | 交易成本 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for a in r["accounts"]:
        lines.append(f"| {a['name']} | {a['strategy']} | {a['days']} | {pct(a['total'], 2)} | {pct(a['mdd'], 2)} | "
                     f"{report._num(a['calmar'])} | {pct(a['stock_weight'])} | {a['sells']} | {pct(a['win_rate'])} | "
                     f"{a['costs']:.0f} |")
    lines += ["", "**main 超额收益**：" + ("，".join(f"相对 {k} {pct(v, 2)}" for k, v in r["excess"].items()) or "暂无"),
              "", "**选股与仓位拆解**：" + (
                  f"选股净贡献（main − etf_band）{pct(r['selection'], 2)}；"
                  f"仓位控制贡献（etf_band − etf）{pct(r['positioning'], 2)}"
                  if r.get("selection") is not None and r.get("positioning") is not None
                  else "etf_band 尚未产生净值，暂不可拆解"),
              "", f"## 证据门槛", "",
              f"main 已平仓个股 {r['n_closed']} 笔；参数类提案需 ≥{MIN_TRADES_FOR_PROPOSAL} 笔相关交易："
              + ("**已达到**" if r["gate"] else "**未达到**，只能记观察项或开挑战者账户做实验")]
    for title, key in (("按通道", "by_channel"), ("按置信度", "by_confidence"), ("按策略版本", "by_version"),
                       ("按模型", "by_model"), ("按离场原因", "by_exit")):
        lines += ["", f"## {title}（仅已平仓）", ""]
        if not r[key]:
            lines.append("暂无已平仓交易。")
            continue
        lines += ["| 分组 | 笔数 | 胜率 | 平均盈亏 | 盈亏比 | 样本够提案 |", "|---|---|---|---|---|---|"]
        lines += [f"| {g['group']} | {g['n']} | {pct(g['win_rate'])} | {pct(g['avg'], 2)} | {report._num(g['payoff'])} | "
                  f"{'是' if g['enough'] else '否'} |" for g in r[key]]
    lines += ["", "## main 持仓明细", ""]
    if r["episodes"]:
        lines += ["| 代码 | 名称 | 开仓 | 平仓 | 离场 | 盈亏 | 通道 | 置信度 | 版本 |", "|---|---|---|---|---|---|---|---|---|"]
        lines += [f"| {e['code']} | {e['name']} | {e['entry_date']} | {e['exit_date'] or '-'} | {e['exit_reason']} | "
                  f"{pct(e['pnl_pct'], 2)} | {e['channels']} | {e['confidence']} | {e['version']} |"
                  for e in sorted(r["episodes"], key=lambda e: e["entry_date"])]
    else:
        lines.append("暂无个股交易。")
    return "\n".join(lines) + "\n"


def run(date: str) -> str:
    r = build(date)
    d = evolution_dir() / "attribution"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{date}.md"
    path.write_text(to_markdown(r), encoding="utf-8")
    return str(path)
