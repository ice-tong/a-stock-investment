"""订单风控校验。任何来源的订单（代码策略或 Agent）都必须经过这里，才能冻结和执行。

输入：订单草稿（目标仓位）。输出：每笔订单标注 approved / adjusted / rejected 及原因，
并自动补上强制卖出（止损、时间止损、熔断）和底仓 ETF 调整。
"""
from . import calendar as cal
from . import etf
from . import market as mk

TIERS = ["offense", "neutral", "defense"]
FORCED = ("stop_loss", "time_stop", "breaker", "halt")


def band_for(rcfg: dict, regime: str, risk_state: str) -> dict:
    if risk_state in ("breaker", "halt"):
        return dict(rcfg["bands"]["breaker"])
    band = dict(rcfg["bands"][regime])
    if risk_state == "warn":  # 回撤预警：个股上限降一档
        i = min(TIERS.index(regime) + 1, len(TIERS) - 1)
        band["stock_max"] = min(band["stock_max"], rcfg["bands"][TIERS[i]]["stock_max"])
    return band


def universe_check(code: str, name: str, info: dict | None, ucfg: dict) -> str | None:
    """不符合股票池规则时返回原因。info 来自初筛数据：avg_amount / chg_1d / chg_5d / listed_days。"""
    num = code[2:]
    if code.startswith("bj") or any(num.startswith(p) for p in ucfg["exclude_prefixes"]):
        return "所属板块在排除名单（无交易权限）"
    if ucfg["exclude_st"] and ("ST" in name.upper() or "退" in name):
        return "ST/退市风险股"
    if info is None:
        return "缺少行情数据，无法校验"
    if info["listed_days"] < ucfg["min_listed_days"]:
        return f"上市不足 {ucfg['min_listed_days']} 个交易日"
    if info["avg_amount"] < ucfg["min_avg_amount"]:
        return f"日均成交额 {info['avg_amount'] / 1e8:.2f} 亿，低于下限"
    if info["chg_1d"] > ucfg["max_day_change"]:
        return f"当日涨幅 {info['chg_1d']:.1%} 过高，不追"
    if info["chg_5d"] > ucfg["max_5d_change"]:
        return f"近 5 日涨幅 {info['chg_5d']:.1%} 过高，不追"
    return None


def review(acct, drafts: list, ctx: dict) -> tuple:
    """ctx: trade_date, regime, risk_state, rcfg, prices(代码->最新收盘价), info(代码->初筛数据),
    equity, base_etf{code,name}, fixed_etf_weight(仅 ETF 对照组)。返回 (订单列表, 使用的仓位区间)。"""
    rcfg, p = ctx["rcfg"], ctx["rcfg"]["position"]
    prices, equity, date = ctx["prices"], ctx["equity"], ctx["trade_date"]
    state = ctx["risk_state"]
    band = band_for(rcfg, ctx["regime"], state)
    etf_code = ctx["base_etf"]["code"]
    out = []

    def w_now(code):
        return acct.weight(code, prices, equity)

    # 1) 强制卖出优先，且不受最短持有期限制
    pending = acct.state["pending_exits"]
    drafts = [d for d in drafts if d["code"] not in pending]
    for code, reason in pending.items():
        out.append({"code": code, "name": acct.positions[code]["name"], "target_weight": 0.0,
                    "reason_type": reason, "reason": f"系统强制卖出：{reason}", "status": "approved",
                    "risk_notes": ["强制卖出"]})

    # 2) 逐笔检查草稿
    held_after = {c for c, pos in acct.positions.items() if (pos.get("role") or "stock") != "park"} - set(pending)
    new_this_week = _new_positions_this_week(acct, date)
    for d in drafts:
        o = {"target_weight": 0.0, "reason_type": "entry", "risk_notes": []} | d
        o["status"] = "approved"
        code, name, tw = o["code"], o.get("name", ""), float(o["target_weight"])
        notes = o["risk_notes"]
        kind = mk.kind(code)
        if code == etf_code:
            continue  # 底仓 ETF 由系统统一计算
        if kind not in ("stock", "etf"):
            o.update(status="rejected"), notes.append("只允许交易个股与 ETF（底仓 ETF 由系统统一计算）")
            out.append(o)
            continue
        cur = w_now(code)
        if tw < cur:  # 减仓/卖出
            pos = acct.positions.get(code)
            if pos and o["reason_type"] not in FORCED:
                held = cal.count_between(pos["last_buy_date"], ctx["settle_date"])
                if held < p["min_hold_days"] - 1:  # 次日执行时持有天数 = held + 1
                    o["status"] = "rejected"
                    notes.append(f"未满最短持有期 {p['min_hold_days']} 个交易日")
            if o["status"] != "rejected" and tw <= 0:
                held_after.discard(code)
            out.append(o)
            continue
        # 加仓/买入
        if state == "halt":
            o["status"] = "rejected"; notes.append("系统已暂停，禁止买入")
        elif state == "breaker":
            o["status"] = "rejected"; notes.append("熔断期间禁止新开或增加个股/主动 ETF 仓位")
        elif kind == "etf":
            ok, why = etf.check(ctx["market"], code, ctx["settle_date"], name)
            if not ok:
                o["status"] = "rejected"; notes.append(why)
        elif (why := universe_check(code, name, ctx["info"].get(code), rcfg["universe"])):
            o["status"] = "rejected"; notes.append(why)
        if o["status"] == "rejected":
            out.append(o)
            continue
        price = prices.get(code)
        if price is None:
            o["status"] = "rejected"; notes.append("无最新价格")
        else:
            stop_pct = etf.profile(code, name)["stop"] if kind == "etf" else p["stop_loss_pct"]
            stop = o.get("stop_price") or round(price * (1 - stop_pct), 3)
            dist = (price - stop) / price
            if dist <= 0:
                # 止损价不低于现价：距离被截断后单笔风险约束会退化成单票上限，且买入次日就触发止损
                o["status"] = "rejected"; notes.append(f"止损价 {stop} 不低于现价 {price}，拒绝买入")
            elif dist < p.get("min_stop_distance", 0.01):
                o["status"] = "rejected"; notes.append(f"止损距离 {dist:.2%} 小于下限，仓位会被算得过大")
            else:
                cap = min(p["max_single"], p["max_risk_per_trade"] / dist)
                if tw > cap + 1e-9:
                    o["target_weight"], o["status"] = round(cap, 4), "adjusted"
                    notes.append(f"按单票上限/单笔风险 {p['max_risk_per_trade']:.0%} 下调至 {cap:.1%}")
                if price * 100 > o["target_weight"] * equity:
                    o["status"] = "rejected"; notes.append(f"一手 {price * 100:.0f} 元超过目标仓位金额")
                if kind == "etf" and o["status"] != "rejected":
                    o["stop_price"] = stop  # ETF 没有成本价重算，直接把类别止损写进订单
                is_new = code not in acct.positions
                if o["status"] != "rejected" and is_new:
                    if len(held_after) >= p["max_stocks"]:
                        o["status"] = "rejected"; notes.append(f"持股数已达上限 {p['max_stocks']}")
                    elif new_this_week >= p["max_new_per_week"]:
                        o["status"] = "rejected"; notes.append(f"本周新开仓已达上限 {p['max_new_per_week']}")
                    else:
                        new_this_week += 1
                        held_after.add(code)

        out.append(o)

    # 3) 个股 + 主动 ETF 的总仓位不超过上限：按比例压缩买入
    def risk_weight_after():
        targets = {o["code"]: o["target_weight"] for o in out if o["status"] != "rejected"}
        codes = (set(acct.positions) | set(targets)) - {etf_code}
        return sum(targets.get(c, w_now(c)) for c in codes if mk.kind(c) in ("stock", "etf"))

    total = risk_weight_after()
    if total > band["stock_max"] + 1e-9:
        buys = [o for o in out if o["status"] != "rejected" and o["target_weight"] > w_now(o["code"])]
        extra = sum(o["target_weight"] - w_now(o["code"]) for o in buys)
        excess = total - band["stock_max"]
        scale = max(0.0, 1 - excess / extra) if extra > 0 else 0.0
        for o in buys:
            base = w_now(o["code"])
            o["target_weight"] = round(base + (o["target_weight"] - base) * scale, 4)
            o["status"] = "adjusted" if o["target_weight"] > base else "rejected"
            o["risk_notes"].append(f"股票+主动 ETF 总仓位上限 {band['stock_max']:.0%}，按比例压缩")
        total = risk_weight_after()

    # 4) 底仓 ETF：闲置资金停泊到 ETF，使总仓位达到区间上沿
    etf_target = ctx.get("fixed_etf_weight")
    if etf_target is None:
        etf_target = max(0.0, band["max"] - total)
    etf_now = w_now(etf_code)
    has_buy = any(o["status"] != "rejected" and o["target_weight"] > w_now(o["code"]) for o in out)
    if has_buy or abs(etf_target - etf_now) > p["etf_rebalance_tolerance"]:
        out.append({"code": etf_code, "name": ctx["base_etf"]["name"], "target_weight": round(etf_target, 4),
                    "reason_type": "park", "reason": "底仓 ETF：闲置资金停泊", "status": "approved",
                    "risk_notes": [f"市场状态 {ctx['regime']}，风控状态 {state}，总仓位区间 "
                                   f"{band['min']:.0%}-{band['max']:.0%}"]})
    return out, band


def _new_positions_this_week(acct, date: str) -> int:
    import datetime as dt
    week = dt.date.fromisoformat(date).isocalendar()[:2]
    return sum(1 for t in acct.trades()
               if t["side"] == "buy" and t["new_position"] == "True"
               and dt.date.fromisoformat(t["date"]).isocalendar()[:2] == week)
