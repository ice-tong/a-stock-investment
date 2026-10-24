"""各账户的订单草稿生成。草稿只表达意图（目标仓位），最终由 risk.review 校验。

- agent：读取 Agent 写的 orders/<交易日>/<账户>.draft.json（第二阶段接入）；没有草稿则只做强制卖出和底仓
- quant：买初筛排名靠前的股票，跌出 keep_rank 才卖
- random：同样候选池随机挑股，持有满 rotate_days 换股
- etf：只持有底仓 ETF
草稿里个股的目标仓位统一写个股上限 / 目标持股数，风控会再按单票上限、单笔风险等下调。
"""
import json
import random as rnd

from . import calendar as cal
from . import config


def drafts(acct, trade_date: str, settle_date: str, screen, band: dict, scfg: dict) -> list:
    s = acct.state["strategy"]
    if s == "agent":
        path = config.orders_dir() / trade_date / f"{acct.name}.draft.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))["orders"]
    if s == "etf":
        return []
    eligible = screen[screen["eligible"]] if len(screen) else screen
    pending = acct.state["pending_exits"]
    held = [c for c in acct.stock_codes() if c not in pending]
    out = []
    n = scfg[s]["n_stocks"]
    weight = band["stock_max"] / n
    target_value = weight * acct.values({})[2]

    def affordable(r):  # 一手金额超过单票目标金额的不选，否则会被风控拒绝、白白空出仓位
        return r.close * 100 <= target_value

    if s == "quant":
        cfg = scfg["quant"]
        rank = {r.code: r.rank for r in eligible.itertuples()}
        keep = [c for c in held if rank.get(c, 10 ** 9) <= cfg["keep_rank"]]
        for c in set(held) - set(keep):
            out.append({"code": c, "name": acct.positions[c]["name"], "target_weight": 0.0,
                        "reason_type": "exit", "reason": "跌出初筛排名前 %d 或不再符合条件" % cfg["keep_rank"]})
        picks = [r for r in eligible.itertuples() if r.code not in acct.positions and affordable(r)]
        picks = picks[: max(cfg["n_stocks"] - len(keep), 0)]
    elif s == "random":
        cfg = scfg["random"]
        keep = [c for c in held
                if cal.count_between(acct.positions[c]["first_buy_date"], settle_date) < cfg["rotate_days"]]
        for c in set(held) - set(keep):
            out.append({"code": c, "name": acct.positions[c]["name"], "target_weight": 0.0,
                        "reason_type": "exit", "reason": f"持有满 {cfg['rotate_days']} 个交易日轮换"})
        pool = [r for r in eligible.itertuples() if r.code not in acct.positions and affordable(r)]
        rng = rnd.Random(f"{acct.name}-{trade_date}")  # 固定种子：可复现
        picks = rng.sample(pool, min(max(cfg["n_stocks"] - len(keep), 0), len(pool)))
    else:
        raise ValueError(f"未知策略 {s}")

    for r in picks:
        out.append({"code": r.code, "name": r.name, "target_weight": round(weight, 4), "reason_type": "entry",
                    "reason": (f"随机抽取（初筛排名 {r.rank}）" if s == "random"
                               else f"初筛排名 {r.rank}，20 日风险调整动量 {r.score:.2f}")})
    return out
