"""各账户的订单草稿生成。草稿只表达意图（目标仓位），最终由 risk.review 校验。

- agent：读取 Agent 写的 orders/<交易日>/<账户>.draft.json（第二阶段接入）；没有草稿则只做强制卖出和底仓
- quant：买初筛排名靠前的股票，跌出 keep_rank 才卖
- random：同样候选池随机挑股，持有满 rotate_days 换股
- etf / etf_band：只持有底仓 ETF（etf 固定 98%，etf_band 跟当日仓位区间上沿，见 risk.review）
草稿里个股的目标仓位统一写个股上限 / 目标持股数，风控会再按单票上限、单笔风险等下调。
"""
import json
import random as rnd
import re

from . import calendar as cal
from . import config

CODE_RE = re.compile(r"^(sh|sz)\d{6}$")


def validate_draft(doc) -> tuple:
    """校验 Agent 草稿，返回 (orders, meta, errors)。

    整份草稿不合格（不是对象、没有 orders 列表）按空草稿处理；单笔不合格只剔除该笔。
    两种情况都把原因带回去，由调用方写进冻结订单留痕——绝不因为草稿坏了就崩溃。
    """
    if not isinstance(doc, dict):
        return [], {}, ["草稿不是一个 JSON 对象，按空草稿处理"]
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    raw = doc.get("orders")
    if not isinstance(raw, list):
        return [], meta, ["草稿缺少 orders 列表，按空草稿处理"]
    good, errors = [], []
    for i, o in enumerate(raw, 1):
        if not isinstance(o, dict):
            errors.append(f"第 {i} 笔不是 JSON 对象，已剔除")
            continue
        code = str(o.get("code") or "")
        if not CODE_RE.match(code):
            errors.append(f"第 {i} 笔代码 {code!r} 不是 sh/sz + 6 位数字，已剔除")
            continue
        tw = o.get("target_weight")
        if isinstance(tw, bool) or not isinstance(tw, (int, float)) or not 0 <= tw <= 1:
            errors.append(f"第 {i} 笔 {code} 的 target_weight={tw!r} 不是 0 到 1 之间的数字，已剔除")
            continue
        sp = o.get("stop_price")
        if sp is not None and (isinstance(sp, bool) or not isinstance(sp, (int, float)) or sp <= 0):
            errors.append(f"第 {i} 笔 {code} 的 stop_price={sp!r} 不是正数，已剔除")
            continue
        good.append(o | {"code": code, "target_weight": float(tw)})
    return good, meta, errors


def fresh_enough(drafts: list, meta: dict, settle_date: str) -> tuple:
    """草稿数据日校验。返回 (ok, reason)。

    有订单却没有 meta.decided_on，或者 decided_on 早于结算日，都整份丢弃——前者无法
    确认数据日，后者等于拿旧数据下单。decided_on 晚于结算日是允许的：前夜冻结时，周末
    的研究信息日会晚于上一交易日的结算日。
    """
    if not drafts:
        return True, ""
    decided = str((meta or {}).get("decided_on") or "")
    if not decided:
        return False, "草稿有订单但缺少 meta.decided_on，整份丢弃（无法确认数据日）"
    if decided < settle_date:
        return False, f"草稿基于 {decided} 的数据，早于结算日 {settle_date}，整份丢弃（不拿旧数据下单）"
    return True, ""


def load_draft(path) -> tuple:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [], {}, [f"草稿无法解析（{type(e).__name__}），按空草稿处理：{e}"]
    return validate_draft(doc)


def drafts(acct, trade_date: str, settle_date: str, screen, band: dict, scfg: dict) -> tuple:
    """返回 (订单列表, 草稿 meta, 草稿问题列表)。非 Agent 账户 meta 与问题均为空。"""
    s = acct.state["strategy"]
    if s == "agent":
        path = config.orders_dir() / trade_date / f"{acct.name}.draft.json"
        if not path.exists():
            return [], {}, []
        orders, meta, errors = load_draft(path)
        # 新鲜度只约束 Agent 草稿：程序生成的订单没有 decided_on 概念
        ok, why = fresh_enough(orders, meta, settle_date)
        if not ok:
            return [], meta, errors + [why]
        return orders, meta, errors
    if s in ("etf", "etf_band"):
        return [], {}, []

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
    return out, {}, []
