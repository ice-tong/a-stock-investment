"""模拟撮合：按真实 A 股规则成交，并计算全部交易成本。

订单用"目标仓位"（占总资产比例）表达，执行时按执行时点价格换算成股数：
先卖后买；个股买入优先于底仓 ETF；现金不足时按顺序缩量。
"""
import math

from . import market as mk


def fees(cfg: dict, kind: str, side: str, amount: float) -> tuple:
    f = cfg["fees"]
    commission = max(amount * f["commission_rate"], f["commission_min"]) if amount > 0 else 0.0
    stamp = amount * f["stamp_tax_sell"] if (side == "sell" and kind == "stock") else 0.0
    transfer = amount * f["transfer_fee"] if kind == "stock" else 0.0
    return round(commission, 2), round(stamp, 2), round(transfer, 2)


def slippage_rate(cfg: dict, kind: str, avg_amount: float | None, source: str) -> float:
    if kind == "etf":
        rate = cfg["etf_slippage"]
    else:
        rate = cfg["slippage"][-1]["rate"]
        for tier in cfg["slippage"]:
            if (avg_amount or 0) >= tier["min_avg_amount"]:
                rate = tier["rate"]
                break
    return rate + (cfg["fallback_penalty"] if source == "close_fallback" else 0.0)


def _floor_lot(x: float, lot: int) -> int:
    return int(math.floor(x / lot + 1e-9)) * lot


def execute(acct, doc: dict, date: str, market, bcfg: dict, stop_loss_pct: float) -> list:
    """执行一份已冻结的订单文件，返回每笔订单的结果。调用方负责保存账户。"""
    if date in acct.state["executed"]:
        return [{"status": "skipped", "note": f"{date} 已执行过，跳过（防止重复下单）"}]
    lot = bcfg["lot_size"]
    orders = [o for o in doc["orders"] if o.get("status") in ("approved", "adjusted")]
    names = {o["code"]: o.get("name", "") for o in orders} | {c: p["name"] for c, p in acct.positions.items()}

    snaps = {c: market.snapshot(c, date, names.get(c, "")) for c in set(names)}
    prices = {c: s.price for c, s in snaps.items() if s}
    _, _, equity = acct.values(prices)

    plans = []
    for o in orders:
        code, snap = o["code"], snaps.get(o["code"])
        cur = acct.positions.get(code, {}).get("shares", 0.0)
        if snap is None:
            plans.append((o, None, cur, 0))
            continue
        target = 0 if o["target_weight"] <= 0 else _floor_lot(o["target_weight"] * equity / snap.price, lot)
        plans.append((o, snap, cur, target))

    def order_key(item):
        o, snap, cur, target = item
        selling = target < cur
        return (0 if selling else 1, 1 if mk.kind(o["code"]) == "etf" else 0)

    results = []
    for o, snap, cur, target in sorted(plans, key=order_key):
        code, kind = o["code"], mk.kind(o["code"])
        res = {"order_id": o.get("id", ""), "code": code, "name": names.get(code, ""), "status": "rejected"}
        results.append(res)
        if snap is None:
            res["note"] = "停牌或取不到行情，不成交"
            continue
        delta = target - cur
        if abs(delta) < 1e-9:
            res.update(status="noop", note="已在目标仓位")
            continue
        slip = slippage_rate(bcfg, kind, market.avg_amount(code, date), snap.source)
        vol_cap = snap.volume * bcfg["volume_cap_ratio"]

        if delta < 0:  # ---------- 卖出 ----------
            pos = acct.positions[code]
            if snap.price <= snap.limit_down + 1e-9:
                res["note"] = "跌停无法卖出，顺延"
                continue
            if pos["last_buy_date"] == date:
                res["note"] = "T+1：当日买入不能卖出"
                continue
            qty = cur if target == 0 else -delta
            partial = False
            if qty > vol_cap:
                qty, partial = _floor_lot(vol_cap, lot), True
            if qty <= 0:
                res["note"] = "超过成交量上限，未成交"
                continue
            fill = round(snap.price * (1 - slip), 3)
            gross = qty * fill
            c, s, t = fees(bcfg, kind, "sell", gross)
            net = gross - c - s - t
            cost_part = pos["cost"] * qty / cur
            acct.state["cash"] += net
            pos["shares"] -= qty
            pos["cost"] -= cost_part
            closed = pos["shares"] < 1e-6
            if closed:
                del acct.positions[code]
            acct.append_trade({"date": date, "order_id": res["order_id"], "code": code, "name": res["name"],
                               "side": "sell", "shares": round(qty, 4), "price": fill, "amount": round(gross, 2),
                               "commission": c, "stamp_tax": s, "transfer_fee": t,
                               "slippage_cost": round((snap.price - fill) * qty, 2),
                               "realized_pnl": round(net - cost_part, 2), "closed": closed,
                               "reason_type": o.get("reason_type", ""), "fill_source": snap.source,
                               "note": "部分成交（成交量上限）" if partial else ""})
            res.update(status="partial" if partial else "filled", side="sell", shares=qty, price=fill)

        else:  # ---------- 买入 ----------
            if snap.price >= snap.limit_up - 1e-9:
                res["note"] = "涨停无法买入"
                continue
            if o.get("limit_price") and snap.price > o["limit_price"]:
                res["note"] = f"执行价 {snap.price} 高于限价 {o['limit_price']}，放弃"
                continue
            fill = round(snap.price * (1 + slip), 3)
            qty = delta
            note = ""
            if qty > vol_cap:
                qty, note = _floor_lot(vol_cap, lot), "成交量上限缩量"
            while qty > 0 and qty * fill + sum(fees(bcfg, kind, "buy", qty * fill)) > acct.cash:
                qty -= lot
                note = "现金不足缩量"
            if qty <= 0:
                res["note"] = note or "现金不足"
                continue
            gross = qty * fill
            c, s, t = fees(bcfg, kind, "buy", gross)
            acct.state["cash"] -= gross + c + s + t
            pos = acct.positions.get(code)
            new_position = pos is None
            if new_position:
                role = "park" if kind == "etf" and o.get("reason_type") == "park" else kind
                pos = acct.positions[code] = {"name": res["name"], "kind": kind, "role": role,
                                              "shares": 0.0, "cost": 0.0,
                                              "first_buy_date": date, "hfq": market.hfq_factor(code, date)}
            pos["shares"] += qty
            pos["cost"] += gross + c + s + t
            pos["last_buy_date"] = date
            pos["last_price"] = snap.price
            if kind == "stock":  # 个股止损按成本价 −8%
                pos["stop_price"] = o.get("stop_price") or round(pos["cost"] / pos["shares"] * (1 - stop_loss_pct), 3)
            elif pos.get("role") == "active":  # 主动 ETF 用订单里的类别止损
                pos["stop_price"] = o.get("stop_price") or round(pos["cost"] / pos["shares"] * (1 - stop_loss_pct), 3)
            acct.append_trade({"date": date, "order_id": res["order_id"], "code": code, "name": res["name"],
                               "side": "buy", "shares": qty, "price": fill, "amount": round(gross, 2),
                               "commission": c, "stamp_tax": s, "transfer_fee": t,
                               "slippage_cost": round((fill - snap.price) * qty, 2), "realized_pnl": "",
                               "new_position": new_position and kind in ("stock", "etf"),
                               "reason_type": o.get("reason_type", ""), "fill_source": snap.source, "note": note})
            res.update(status="partial" if qty < delta else "filled", side="buy", shares=qty, price=fill, note=note)

    acct.state["executed"].append(date)
    return results
