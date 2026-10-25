"""盘后结算：估值、分红送转、净值与回撤、熔断状态、止损检查。"""
from . import calendar as cal
from . import market as mk

# 单日复权因子比值超出这个区间时不当成真实除权（10 送 10 约为 2，转增 3 送 3 约为 1.6）
CORP_RATIO_RANGE = (0.5, 3.0)


class StaleData(Exception):
    """行情数据没更新到结算日 / 取价大面积失败：不结算、不冻结，交给 cron 重试。"""


def data_fresh(market, index: str, date: str) -> bool:
    """基准指数在结算日是否有日线。没有说明数据源还没更新到今天，当天不该结算也不该冻结。"""
    df = market.daily(index, date)
    return df is not None and len(df) > 0 and str(df.index[-1]) == date


def regime(market, index: str, date: str) -> str:
    """按沪深 300 均线客观判定市场状态：offense / neutral / defense。

    数据不足时返回 defense（最保守档），而不是 neutral——取不到数据时不能放宽仓位区间。
    """
    df = market.daily(index, date)
    if df is None or len(df) < 125:
        return "defense"
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

    # 1) 先取全部估值价：数据源整体故障时在写任何东西之前中止，不能把"取不到"当成"停牌"
    prices, suspended, unavailable = {}, [], []
    for code, pos in acct.positions.items():
        price = market.close(code, date)
        if price is None:
            price = market.last_close(code, date)
            if price is not None:
                suspended.append(pos["name"])
        if price is None:
            unavailable.append(pos["name"])
            price = pos["last_price"]
        prices[code] = price
    if acct.positions and len(unavailable) * 2 > len(acct.positions):
        raise StaleData(f"{len(unavailable)}/{len(acct.positions)} 只持仓取不到价格，判定为数据源故障，本次不结算")
    notes += [f"{n} 当日无成交（停牌？），按最近收盘价估值" for n in suspended]
    notes += [f"{n} 取不到任何价格，沿用上次估值价" for n in unavailable]

    # 2) 分红送转：后复权因子变化即发生了除权除息，按比例折算持股（近似为红利再投资，未扣红利税）
    for code, pos in acct.positions.items():
        f_now = market.hfq_factor(code, date)
        if f_now is None:
            notes.append(f"{pos['name']} 复权因子取数失败，今日未检查除权")
        else:
            prev = pos.get("hfq")
            update_hfq = True
            if prev and abs(f_now / prev - 1) > 1e-6:
                ratio = f_now / prev
                if CORP_RATIO_RANGE[0] <= ratio <= CORP_RATIO_RANGE[1]:
                    pos["shares"] *= ratio
                    if pos.get("stop_price"):  # 止损价是绝对价格，必须跟着除权同比例调整
                        pos["stop_price"] = round(pos["stop_price"] / ratio, 3)
                    acct.append_trade({"date": date, "code": code, "name": pos["name"], "side": "corp_action",
                                       "shares": round(pos["shares"], 4),
                                       "note": f"除权除息，持股按 {ratio:.6f} 折算"
                                               + ("，止损价同步调整" if pos.get("stop_price") else "")})
                    notes.append(f"{pos['name']} 除权除息，持股 × {ratio:.4f}")
                else:
                    # 因子跳变超出合理范围多半是数据错误：不折算、不覆盖原因子，留待人工复核
                    notes.append(f"{pos['name']} 复权因子 {prev:.4f}→{f_now:.4f}（×{ratio:.3f}）超出合理范围，"
                                 "未自动折算，需人工复核")
                    update_hfq = False
            if update_hfq:
                pos["hfq"] = f_now
        pos["last_price"] = prices[code]

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
