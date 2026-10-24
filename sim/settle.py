"""盘后结算：估值、分红送转、净值与回撤、熔断状态、止损检查。"""
from . import calendar as cal
from . import market as mk


def regime(market, index: str, date: str) -> str:
    """按沪深 300 均线客观判定市场状态：offense / neutral / defense。数据不足时为 neutral。"""
    df = market.daily(index, date)
    if df is None or len(df) < 125:
        return "neutral"
    close = df["close"]
    last = close.iloc[-1]
    ma20, ma60, ma120 = (close.rolling(n).mean() for n in (20, 60, 120))
    if last > ma120.iloc[-1] and ma20.iloc[-1] > ma20.iloc[-6]:
        return "offense"
    if last < ma120.iloc[-1] and last < ma60.iloc[-1]:
        return "defense"
    return "neutral"


def risk_state(state: dict, date: str) -> str:
    if state["halted"]:
        return "halt"
    if state["breaker_until"] and date <= state["breaker_until"]:
        return "breaker"
    return "warn" if state["dd_warn"] else "normal"


def settle(acct, date: str, market, rcfg: dict, regime_name: str) -> dict:
    st = acct.state
    if st["last_settled"] and st["last_settled"] >= date:
        return {"skipped": True, "note": f"{date} 已结算"}
    notes = []

    prices = {}
    for code, pos in list(acct.positions.items()):
        price = market.close(code, date)
        if price is None:
            price = market.last_close(code, date) or pos["last_price"]
            notes.append(f"{pos['name']} 当日无成交（停牌？），按 {price} 估值")
        # 分红送转：后复权因子变化即发生了除权除息，按比例折算持股（近似为红利再投资，未扣红利税）
        f_now = market.hfq_factor(code, date)
        if pos.get("hfq") and abs(f_now / pos["hfq"] - 1) > 1e-6:
            ratio = f_now / pos["hfq"]
            pos["shares"] *= ratio
            acct.append_trade({"date": date, "code": code, "name": pos["name"], "side": "corp_action",
                               "shares": round(pos["shares"], 4), "note": f"除权除息，持股按 {ratio:.6f} 折算"})
            notes.append(f"{pos['name']} 除权除息，持股 × {ratio:.4f}")
        pos["hfq"] = f_now
        pos["last_price"] = price
        prices[code] = price

    stock_v, etf_v, equity = acct.values(prices)
    st["peak"] = max(st["peak"], equity)
    dd = 1 - equity / st["peak"]
    d = rcfg["drawdown"]
    if dd >= d["halt"] and not st["halted"]:
        st["halted"] = True
        notes.append(f"回撤 {dd:.1%} 触发系统暂停，需人工 resume")
    elif dd >= d["breaker"] and st.get("breaker_peak") != st["peak"]:
        # 同一个净值高点只熔断一次，熔断期满后不会因回撤仍在而无限续期；创出新高后才会重新计算
        st["breaker_until"] = cal.offset(date, d["breaker_days"])
        st["breaker_peak"] = st["peak"]
        notes.append(f"回撤 {dd:.1%} 触发熔断，至 {st['breaker_until']} 暂停新开个股仓位")
    st["dd_warn"] = dd >= d["warn"]
    state_now = risk_state(st, date)

    # 强制卖出清单：每天重新计算
    p = rcfg["position"]
    pending = {}
    for code, pos in acct.positions.items():
        if pos["kind"] != "stock":
            continue
        if state_now in ("breaker", "halt"):
            pending[code] = state_now
        elif prices[code] <= pos["stop_price"]:
            pending[code] = "stop_loss"
        elif (cal.count_between(pos["first_buy_date"], date) >= p["time_stop_days"]
              and prices[code] * pos["shares"] < pos["cost"]):
            pending[code] = "time_stop"
    st["pending_exits"] = pending

    acct.append_nav({"date": date, "cash": round(acct.cash, 2), "stock_value": round(stock_v, 2),
                     "etf_value": round(etf_v, 2), "equity": round(equity, 2),
                     "stock_weight": round(stock_v / equity, 4), "etf_weight": round(etf_v / equity, 4),
                     "peak": round(st["peak"], 2), "drawdown": round(dd, 4), "regime": regime_name,
                     "risk_state": state_now})
    st["last_settled"] = date
    return {"equity": equity, "drawdown": dd, "risk_state": state_now, "pending_exits": pending, "notes": notes}
