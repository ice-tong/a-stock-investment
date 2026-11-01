"""隔夜实验：尾盘集合竞价买入 → 次日开盘集合竞价卖出。设计见 docs/research/隔夜策略_可行性与设计.md

三个账户同规则、同成本、同可成交性，只有"谁选股"不同：
  agent   Agent 决策（读 orders/<日>/overnight_agent.draft.json，缺失时用午间 provisional）
  quant   on-scan 量化打分前 N
  random  可交易池随机 N 只

账本 accounts/overnight/<账户>/（复用 ledger.Account，sim 主流程的 all_accounts 不会扫到）；
订单 orders/<交易日>/overnight_<账户>.json（复用 orders.freeze 的哈希冻结）；
研究与打分数据 data/overnight/。

一天的命令：
  14:40  on-scan     全市场快照 + 打分 → data/overnight/cand/<日>.md（Agent 的工具，也是 quant 组）
  14:54  on-freeze   读草稿 → 风控 → 冻结三组订单（必须早于 freeze_deadline）
  15:30  on-settle   卖出前一晚持仓（今日开盘价）→ 买入今日订单（今日收盘价）→ 记净值、打分
"""
import csv
import datetime as dt
import json
import math
import random as rnd

import pandas as pd

from . import broker, calendar as cal, config, orders, settle
from .ledger import Account
from .market import limit_pct, limit_prices

SCORE_FIELDS = ["night", "exit_date", "account", "code", "name", "status", "weight", "buy_price", "sell_price",
                "ret_gross", "ret_net", "win", "p_win", "exp_gap", "thesis_type", "source", "note"]
SPOT_COLS = {"代码": "code", "名称": "name", "最新价": "price", "涨跌幅": "pct", "昨收": "prev", "今开": "open",
             "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount", "时间戳": "ts"}


def cfg() -> dict:
    return config.load("overnight")


def rules() -> dict:
    return config.load("overnight_risk")


def acct_name(n: str) -> str:
    return f"overnight/{n}"


def order_key(n: str) -> str:
    return f"overnight_{n}"


def data_dir():
    return config.home() / "data" / "overnight"


def accounts() -> list:
    d = config.accounts_dir() / "overnight"
    return [Account(acct_name(n)) for n in cfg()["accounts"] if (d / n / "state.json").exists()]


def short(acct) -> str:
    return acct.name.split("/", 1)[1]


def init(start: str, log) -> None:
    c = cfg()
    for n, kind in c["accounts"].items():
        try:
            Account.create(acct_name(n), f"overnight_{kind}", c["initial_cash"], start)
            log(f"创建隔夜账户 {n}，起始日 {start}，初始资金 {c['initial_cash']:,}")
        except FileExistsError:
            log(f"隔夜账户 {n} 已存在，跳过")


# ---------------- 快照与打分 ----------------
def _z(s: pd.Series) -> pd.Series:
    sd = s.std()
    return (s - s.mean()) / sd if sd and sd > 0 else s * 0.0


def prepare_spot(spot: pd.DataFrame) -> pd.DataFrame:
    df = spot.rename(columns=SPOT_COLS)[list(SPOT_COLS.values())].copy()
    for c in ("price", "pct", "prev", "open", "high", "low", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[(df["price"] > 0) & (df["prev"] > 0)].copy()
    lim = [limit_prices(c, n, p) for c, n, p in zip(df["code"], df["name"], df["prev"])]
    df["up"], df["down"] = [x[0] for x in lim], [x[1] for x in lim]
    df["limit"] = [limit_pct(c, n) * 100 for c, n in zip(df["code"], df["name"])]
    rng = (df["high"] - df["low"]).where(lambda s: s > 0)
    df["pos"] = ((df["price"] - df["low"]) / rng).fillna(0.5)
    return df


def breadth(df: pd.DataFrame) -> dict:
    zt = int((df["price"] >= df["up"] - 1e-9).sum())
    touched = int((df["high"] >= df["up"] - 1e-9).sum())
    return {"total": len(df), "up": int((df["pct"] > 0).sum()), "down": int((df["pct"] < 0).sum()),
            "zt": zt, "dt": int((df["price"] <= df["down"] + 1e-9).sum()),
            "zhaban": touched - zt, "zhaban_rate": round((touched - zt) / touched, 3) if touched else 0.0,
            "median_pct": round(float(df["pct"].median()), 2)}


def universe(df: pd.DataFrame, r: dict) -> pd.DataFrame:
    num = df["code"].str[2:]
    bad = df["name"].str.match(r"^[NC]") | (df["price"] < r["min_price"])
    bad |= ~df["code"].str.startswith(("sh", "sz", "bj"))
    if r.get("exclude_bj"):
        bad |= df["code"].str.startswith("bj")
    if r.get("exclude_st"):
        bad |= df["name"].str.contains("ST|退", regex=True)
    bad |= ~num.str.match(r"^(00|30|60|68)")  # 只做 A 股个股（排除 B 股、指数、基金代码段）
    return df[~bad]


def tail_strength(market, code: str, date: str, price: float, t_from: str) -> float | None:
    m = market._minutes(code)
    if m is None:
        return None
    day = m[m["day"].str.startswith(date)]
    before = day[day["day"].str[11:16] <= t_from]
    if len(before) == 0:
        return None
    base = float(before["close"].iloc[-1])
    return price / base - 1 if base > 0 else None


def scan(date: str, market, log, now: dt.datetime | None = None) -> pd.DataFrame:
    now = now or dt.datetime.now()
    c, r = cfg(), rules()
    sc = c["scan"]
    raw = market.spot()
    snap_dir = data_dir() / "snap"
    snap_dir.mkdir(parents=True, exist_ok=True)
    raw.to_csv(snap_dir / f"{date}_{now:%H%M}.csv", index=False, encoding="utf-8-sig")
    df = prepare_spot(raw)
    br = breadth(df)
    uni = universe(df, r)
    uni = uni[uni["price"] * 100 <= c["initial_cash"] * r["max_total"] / c["quant"]["n"]]  # 等权一份买不起一手的不进对照组
    pre = uni[(uni["amount"] >= sc["min_amount"]) & (uni["pct"] >= sc["pct_min"])
              & (uni["pct"] <= uni["limit"] - sc["limit_gap"]) & (uni["pos"] >= sc["pos_min"])].copy()
    w = sc["weights"]
    pre["la"] = pre["amount"].apply(math.log)
    pre["score"] = w["pos"] * _z(pre["pos"]) + w["pct"] * _z(pre["pct"]) + w["amount"] * _z(pre["la"])
    pre = pre.sort_values("score", ascending=False)
    top = pre.head(sc["minute_top"])
    # 分钟线接口走 mini_racer（V8），多线程并发会崩进程，只能串行
    tails = [tail_strength(market, code, date, price, sc["tail_from"]) for code, price in zip(top["code"], top["price"])]
    pre["tail"] = pd.Series(tails, index=top.index, dtype=float).reindex(pre.index)
    pre["score"] = pre["score"] + w["tail"] * _z(pre["tail"]).fillna(0.0)
    pre = pre.sort_values("score", ascending=False).reset_index(drop=True)
    pre["rank"] = range(1, len(pre) + 1)
    cols = ["rank", "code", "name", "price", "pct", "pos", "tail", "amount", "up", "score"]
    out = data_dir() / "cand"
    out.mkdir(parents=True, exist_ok=True)
    pre[cols].round(4).to_csv(out / f"{date}.csv", index=False, encoding="utf-8-sig")
    rp = uni[(uni["amount"] >= c["random"]["min_amount"]) & (uni["price"] < uni["up"] * 0.995)]
    rp[["code", "name", "price"]].to_csv(out / f"{date}_random_pool.csv", index=False, encoding="utf-8-sig")
    (out / f"{date}.md").write_text(scan_markdown(date, now, br, pre[cols].head(sc["show_top"]), len(uni), sc),
                                    encoding="utf-8")
    (out / f"{date}_breadth.json").write_text(json.dumps(br, ensure_ascii=False), encoding="utf-8")
    log(f"快照 {len(df)} 只（{df['ts'].max()}），可交易 {len(uni)}，量化候选 {len(pre)}；"
        f"涨停 {br['zt']} / 跌停 {br['dt']} / 炸板率 {br['zhaban_rate']:.0%}")
    return pre


def scan_markdown(date, now, br, top, n_uni, sc) -> str:
    lines = [f"# 隔夜候选 {date}（快照 {now:%H:%M}）", "",
             "> 量化打分只是工具：Agent 可以选表外的票，也可以一只都不选。分数 = 日内位置 + 涨幅 + 尾盘强度 + 成交额（z 分数加权）。",
             "", "## 市场情绪", "",
             f"上涨 {br['up']} / 下跌 {br['down']}（共 {br['total']}），涨幅中位数 {br['median_pct']}%；"
             f"涨停 {br['zt']}，跌停 {br['dt']}，炸板 {br['zhaban']}（炸板率 {br['zhaban_rate']:.0%}）。可交易池 {n_uni} 只。",
             "", f"## 量化前 {len(top)}（涨幅 ≥{sc['pct_min']}%、距涨停 ≥{sc['limit_gap']} 个点、日内位置 ≥{sc['pos_min']}、"
             f"成交额 ≥{sc['min_amount'] / 1e8:.0f} 亿）", "",
             "| # | 代码 | 名称 | 现价 | 涨幅% | 日内位置 | 尾盘(自 " + sc["tail_from"] + ") | 成交额(亿) | 涨停价 | 分数 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for _, x in top.iterrows():
        tail = "" if pd.isna(x["tail"]) else f"{x['tail']:+.2%}"
        lines.append(f"| {x['rank']} | {x['code']} | {x['name']} | {x['price']} | {x['pct']:.2f} | {x['pos']:.2f} | "
                     f"{tail} | {x['amount'] / 1e8:.1f} | {x['up']} | {x['score']:.2f} |")
    return "\n".join(lines) + "\n"


# ---------------- 冻结 ----------------
def _latest_snapshot(date: str) -> pd.DataFrame | None:
    files = sorted((data_dir() / "snap").glob(f"{date}_*.csv"))
    if not files:
        return None
    return prepare_spot(pd.read_csv(files[-1], dtype={"代码": str}))


def _read_csv(path) -> pd.DataFrame | None:
    return pd.read_csv(path, dtype={"code": str}) if path.exists() else None


def load_draft(date: str, now: dt.datetime) -> tuple:
    """返回 (草稿, 来源)。尾盘草稿优先；缺失或无效时退回午间 provisional；都没有 = missed（与主动空仓区分）。"""
    d = config.orders_dir() / date
    errors = []
    for fname, source in (("overnight_agent.draft.json", "draft"), ("overnight_agent.provisional.json", "provisional")):
        p = d / fname
        if not p.exists():
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            at = dt.datetime.fromisoformat(doc["decided_at"])
        except Exception as e:
            errors.append(f"{fname} 无法解析：{type(e).__name__}: {e}")
            continue
        if at.date().isoformat() != date or at > now:
            errors.append(f"{fname} 的 decided_at={doc['decided_at']} 不在 {date} 冻结前，作废")
            continue
        return doc, source, errors
    return {"picks": [], "vetoed": []}, "missed", errors


def review(picks: list, market, snap: pd.DataFrame | None, date: str, r: dict, require: bool) -> list:
    """逐只风控：硬性条件不满足 → rejected；仓位超限 → 缩到上限。三组共用。"""
    px = {} if snap is None else snap.set_index("code").to_dict("index")
    prev = cal.prev_day(date)
    out, seen = [], set()
    for p in picks:
        o = {k: p.get(k) for k in ("code", "name", "weight", "p_win", "exp_gap", "thesis_type", "thesis", "kill")}
        o["code"] = str(o["code"] or "").lower()
        o["notes"], o["status"] = [], "approved"
        info = px.get(o["code"])
        if info:
            o["name"] = o["name"] or info["name"]
        name = o["name"] or ""
        if not o["code"][:2] in ("sh", "sz", "bj") or len(o["code"]) != 8:
            o["notes"].append("代码格式应为 sh600000 / sz000001")
        elif o["code"] in seen:
            o["notes"].append("重复")
        elif r.get("exclude_bj") and o["code"].startswith("bj"):
            o["notes"].append("北交所不做")
        elif r.get("exclude_st") and ("ST" in name.upper() or "退" in name):
            o["notes"].append("ST/退市股不做")
        if require:
            missing = [f for f in r["require_fields"] if o.get(f) in (None, "", [])]
            if missing:
                o["notes"].append(f"缺少字段 {missing}")
        if not o["notes"]:
            if info and info["price"] < r["min_price"]:
                o["notes"].append(f"股价 {info['price']} 低于 {r['min_price']}")
            daily = market.daily(o["code"], prev)
            if daily is None or len(daily) < r["min_listed_days"]:
                o["notes"].append(f"上市不足 {r['min_listed_days']} 个交易日或取不到日线")
            elif (avg := float(daily["amount"].tail(20).mean())) < r["min_avg_amount"]:
                o["notes"].append(f"近 20 日日均成交额 {avg / 1e8:.2f} 亿 < {r['min_avg_amount'] / 1e8:.1f} 亿")
        if o["notes"]:
            o["status"] = "rejected"
        else:
            seen.add(o["code"])
            if info and info["price"] >= info["up"] * 0.995:
                o["notes"].append("快照时已贴近涨停，收盘封板则买不到")
        out.append(o)
    ok = [o for o in out if o["status"] == "approved"]
    for o in ok[r["max_picks"]:]:
        o["status"], o["notes"] = "rejected", o["notes"] + [f"超过每晚最多 {r['max_picks']} 只"]
    ok = ok[:r["max_picks"]]
    for o in ok:
        w = float(o["weight"] or 0)
        if w <= 0:
            w = r["max_total"] / len(ok)
        if w > r["max_single"]:
            o["notes"].append(f"仓位 {w:.0%} 缩至单只上限 {r['max_single']:.0%}")
            w = r["max_single"]
        o["weight"] = w
    total = sum(o["weight"] for o in ok)
    if total > r["max_total"] + 1e-9:
        for o in ok:
            o["weight"] = o["weight"] * r["max_total"] / total
            o["notes"].append(f"总仓位 {total:.0%} 按比例缩至 {r['max_total']:.0%}")
    for o in ok:
        o["weight"] = round(o["weight"], 4)
    return out


def freeze(date: str, market, log, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now()
    c, r = cfg(), rules()
    if now.date().isoformat() != date or now.strftime("%H:%M") >= r["freeze_deadline"]:
        log(f"冻结必须在 {date} {r['freeze_deadline']} 之前完成（现在 {now:%Y-%m-%d %H:%M}），本次不冻结，三组当晚空仓")
        return 1
    cand = _read_csv(data_dir() / "cand" / f"{date}.csv")
    rpool = _read_csv(data_dir() / "cand" / f"{date}_random_pool.csv")
    snap = _latest_snapshot(date)
    if cand is None:
        log("警告：今天没跑 on-scan，quant / random 组当晚空仓")
    failed = 0
    for acct in accounts():
        n = short(acct)
        kind = c["accounts"][n]
        key = order_key(n)
        if orders.exists(date, key):
            log(f"[{n}] {date} 已冻结，跳过")
            continue
        try:
            source, errors, vetoed, view = kind, [], [], ""
            if kind == "agent":
                draft, source, errors = load_draft(date, now)
                picks, vetoed, view = draft.get("picks", []), draft.get("vetoed", []), draft.get("market_view", "")
            elif kind == "quant":
                picks = [] if cand is None else [{"code": x.code, "name": x.name} for x in cand.head(c["quant"]["n"]).itertuples()]
            else:
                pool = [] if rpool is None else list(zip(rpool["code"], rpool["name"]))
                pick = rnd.Random(f"{date}-overnight-random").sample(pool, min(c["random"]["n"], len(pool)))
                picks = [{"code": code, "name": name} for code, name in pick]
            if acct.state["halted"]:
                errors.append(f"账户回撤触发 {r['halt_drawdown']:.0%} 暂停，不开新仓（on-resume 人工恢复）")
                picks = []
            reviewed = review(picks, market, snap, date, r, require=(kind == "agent"))
            doc = {"account": n, "strategy": acct.state["strategy"], "version": c["version"], "trade_date": date,
                   "frozen_at": now.isoformat(timespec="seconds"), "source": source, "market_view": view,
                   "draft_errors": errors, "orders": reviewed, "vetoed": vetoed}
            digest = orders.freeze(date, key, doc)
            ok = [o for o in reviewed if o["status"] == "approved"]
            log(f"[{n}] 冻结（{source}）：{len(ok)} 只有效 / {len(reviewed)} 只，sha256 {digest[:12]}")
            for e in errors:
                log(f"[{n}]   草稿问题：{e}")
            for o in reviewed:
                log(f"[{n}]   {o['status']:8} {o.get('name') or ''}({o['code']}) {float(o['weight'] or 0):.0%}"
                    + (f" | {'；'.join(o['notes'])}" if o["notes"] else ""))
        except Exception as e:
            failed += 1
            log(f"[{n}] 冻结失败：{type(e).__name__}: {e}")
    commit(f"sim: {date} 隔夜订单冻结")
    return 1 if failed else 0


# ---------------- 结算 ----------------
def append_scores(rows: list):
    path = data_dir() / "scores.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, SCORE_FIELDS)
        if new:
            w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in SCORE_FIELDS})


def _rt_cost(bcfg) -> float:
    f = bcfg["fees"]
    return 2 * f["commission_rate"] + f["stamp_tax_sell"] + 2 * f["transfer_fee"]


def settle_day(acct, d: str, market, bcfg, slip: float, log, allow_missing: bool = True) -> dict:
    n, st, rows = short(acct), acct.state, []
    notes = []
    key = order_key(n)
    doc = orders.load_verified(d, key) if orders.exists(d, key) else None
    need = set(acct.positions) | {o["code"] for o in (doc or {}).get("orders", []) if o["status"] == "approved"}
    missing = sorted(c for c in need if market.bar(c, d) is None)
    if missing and not allow_missing:  # 个股日线可能比指数晚更新：先等，20:00 后仍缺才按停牌处理
        raise settle.StaleData(f"{', '.join(missing)} 的 {d} 日线还没更新")
    # 1) 卖出：前一晚（或更早卖不掉）的持仓，按今日开盘价
    for code, pos in list(acct.positions.items()):
        bar, prev = market.bar(code, d), market.prev_close(code, d)
        if bar is None or prev is None:
            notes.append(f"{pos['name']}({code}) 停牌或无数据，顺延")
            continue
        _, down = limit_prices(code, pos["name"], prev)
        op = float(bar["open"])
        if op <= down + 1e-9:
            notes.append(f"{pos['name']}({code}) 开盘跌停 {op}，卖不出，顺延")
            pos["last_price"] = float(bar["close"])
            continue
        fill = round(op * (1 - slip), 3)
        gross = pos["shares"] * fill
        cm, tax, tf = broker.fees(bcfg, "stock", "sell", gross)
        net = gross - cm - tax - tf
        acct.state["cash"] = round(acct.cash + net, 2)
        pnl = net - pos["cost"]
        acct.append_trade({"date": d, "order_id": pos["order_id"], "code": code, "name": pos["name"], "side": "sell",
                           "shares": pos["shares"], "price": fill, "amount": round(gross, 2), "commission": cm,
                           "stamp_tax": tax, "transfer_fee": tf, "slippage_cost": round((op - fill) * pos["shares"], 2),
                           "realized_pnl": round(pnl, 2), "closed": True, "reason_type": "overnight_exit",
                           "fill_source": "open_auction"})
        p = pos["pred"]
        rows.append(p | {"exit_date": d, "status": "filled" if pos["night"] == cal.prev_day(d) else "carried",
                         "sell_price": fill, "ret_gross": round(fill / pos["buy_price"] - 1, 6),
                         "ret_net": round(pnl / pos["cost"], 6), "win": int(pnl > 0)})
        del acct.positions[code]
    # 2) 买入：今日冻结订单，按今日收盘价
    if doc:
        held = sum(p["shares"] * p["last_price"] for p in acct.positions.values())
        equity = acct.cash + held
        for o in doc["orders"]:
            if o["status"] != "approved":
                continue
            code, name = o["code"], o.get("name") or o["code"]
            pred = {"night": d, "account": n, "code": code, "name": name, "weight": o["weight"],
                    "p_win": o.get("p_win", ""), "exp_gap": o.get("exp_gap", ""),
                    "thesis_type": o.get("thesis_type", ""), "source": doc["source"]}
            bar, prev = market.bar(code, d), market.prev_close(code, d)
            why = None
            if code in acct.positions:
                why = "上一笔仍未卖出"
            elif bar is None or prev is None:
                why = "停牌或无数据"
            else:
                close = float(bar["close"])
                up, _ = limit_prices(code, name, prev)
                if close >= up - 1e-9:
                    why = f"收盘涨停 {close}，买不到"
            if why is None:
                fill = round(close * (1 + slip), 3)
                qty = math.floor(o["weight"] * equity / fill / bcfg["lot_size"]) * bcfg["lot_size"]
                while qty > 0 and qty * fill + sum(broker.fees(bcfg, "stock", "buy", qty * fill)) > acct.cash:
                    qty -= bcfg["lot_size"]
                if qty <= 0:
                    why = "资金不足一手"
            if why:
                rows.append(pred | {"status": "unfilled", "note": why})
                notes.append(f"未成交 {name}({code})：{why}")
                continue
            gross = qty * fill
            cm, tax, tf = broker.fees(bcfg, "stock", "buy", gross)
            acct.state["cash"] = round(acct.cash - gross - cm - tax - tf, 2)
            oid = f"{d.replace('-', '')}-{key}-{code}"
            acct.positions[code] = {"name": name, "kind": "stock", "shares": qty, "cost": round(gross + cm + tax + tf, 2),
                                    "buy_price": fill, "night": d, "last_price": close, "order_id": oid,
                                    "pred": pred | {"buy_price": fill}}
            acct.append_trade({"date": d, "order_id": oid, "code": code, "name": name, "side": "buy", "shares": qty,
                               "price": fill, "amount": round(gross, 2), "commission": cm, "stamp_tax": tax,
                               "transfer_fee": tf, "slippage_cost": round((fill - close) * qty, 2), "new_position": True,
                               "reason_type": "overnight_entry", "fill_source": "close_auction"})
        # Agent 否决名单的反事实：假如按收盘买、次日开盘卖会怎样（次日结算时计算）
        st["veto_pending"] = [v | {"night": d} for v in doc.get("vetoed", []) if v.get("code")]
    # 3) 否决反事实：前一晚的否决名单，今日开盘价揭晓
    if n == "agent":
        rows += veto_outcomes(st.get("veto_prev", []), d, market, bcfg)
        st["veto_prev"] = st.pop("veto_pending", [])
    # 4) 净值
    for code, pos in acct.positions.items():
        if (b := market.bar(code, d)) is not None:
            pos["last_price"] = float(b["close"])
    equity = acct.cash + sum(p["shares"] * p["last_price"] for p in acct.positions.values())
    st["peak"] = max(st["peak"], equity)
    dd = 1 - equity / st["peak"] if st["peak"] else 0.0
    if dd >= rules()["halt_drawdown"] and not st["halted"]:
        st["halted"] = True
        notes.append(f"回撤 {dd:.1%} 触发暂停，不再开新仓")
    held = sum(p["shares"] * p["last_price"] for p in acct.positions.values())
    acct.append_nav({"date": d, "cash": acct.cash, "stock_value": round(held, 2), "etf_value": 0,
                     "equity": round(equity, 2), "stock_weight": round(held / equity, 4) if equity else 0,
                     "etf_weight": 0, "peak": round(st["peak"], 2), "drawdown": round(dd, 4),
                     "regime": "", "risk_state": "halted" if st["halted"] else "normal"})
    st["last_settled"] = d
    acct.save()
    append_scores(rows)
    return {"equity": equity, "drawdown": dd, "notes": notes, "rows": rows}


def veto_outcomes(vetoed: list, d: str, market, bcfg) -> list:
    rows, cost = [], _rt_cost(bcfg)
    for v in vetoed:
        code = str(v["code"]).lower()
        base = {"night": v["night"], "exit_date": d, "account": "agent_veto", "code": code, "name": v.get("name", ""),
                "p_win": v.get("p_win", ""), "thesis_type": v.get("reason", ""), "source": "veto"}
        b0, b1, prev = market.bar(code, v["night"]), market.bar(code, d), market.prev_close(code, v["night"])
        if b0 is None or b1 is None or prev is None:
            rows.append(base | {"status": "unfilled", "note": "无数据"})
            continue
        up, _ = limit_prices(code, base["name"], prev)
        if float(b0["close"]) >= up - 1e-9:
            rows.append(base | {"status": "unfilled", "note": "收盘涨停，本来也买不到"})
            continue
        g = float(b1["open"]) / float(b0["close"]) - 1
        rows.append(base | {"status": "filled", "buy_price": float(b0["close"]), "sell_price": float(b1["open"]),
                            "ret_gross": round(g, 6), "ret_net": round(g - cost, 6), "win": int(g - cost > 0)})
    return rows


def run_settle(date: str, market, log, now: dt.datetime | None = None) -> int:
    bcfg, rcfg = config.load("broker"), config.load("risk")
    if not settle.data_fresh(market, rcfg["regime"]["index"], date):
        raise settle.StaleData(f"基准指数日线还没更新到 {date}，稍后再结算隔夜账户")
    slip = cfg()["fills"]["slippage"]
    now = now or dt.datetime.now()
    failed = 0
    for acct in accounts():
        n = short(acct)
        last = acct.state["last_settled"]
        days = [x for x in _days_between(last or cal.prev_day(acct.state["start_date"]), date)
                if x >= acct.state["start_date"]]
        if not days:
            log(f"[{n}] {date} 已结算，跳过")
            continue
        for d in days:
            try:
                res = settle_day(acct, d, market, bcfg, slip, log,
                                 allow_missing=d < now.date().isoformat() or now.hour >= 20)
            except settle.StaleData as e:
                failed += 1
                log(f"[{n}] {d} 暂不结算：{e}（20:30 补跑会再试）")
                break
            except Exception as e:
                failed += 1
                log(f"[{n}] {d} 结算失败：{type(e).__name__}: {e}")
                break
            done = [x for x in res["rows"] if x["status"] in ("filled", "carried") and x["account"] == n]
            log(f"[{n}] {d} 总资产 {res['equity']:,.2f}，回撤 {res['drawdown']:.2%}；今日平仓 {len(done)} 笔"
                + (f"，平均 {sum(x['ret_net'] for x in done) / len(done):+.2%}" if done else ""))
            for note in res["notes"]:
                log(f"[{n}]   {note}")
    commit(f"sim: {date} 隔夜结算")
    return 1 if failed else 0


def _days_between(after: str, until: str) -> list:
    out, d = [], after
    while True:
        d = cal.next_day(d)
        if d > until:
            return out
        out.append(d)


def commit(message: str) -> bool:
    import subprocess
    home = config.home()
    if (home / ".git").exists() and data_dir().exists():
        subprocess.run(["git", "-C", str(home), "add", str(data_dir().relative_to(home))], check=False,
                       capture_output=True)
    return orders.git_commit(message)


# ---------------- 实验报告 ----------------
TARGET_TRADES = 200   # 设计文档 3.4：每组至少 200 笔才下结论


def _bootstrap_diff(a: list, b: list, iters: int = 2000) -> tuple:
    g = rnd.Random(20261101)
    diffs = sorted(sum(g.choices(a, k=len(a))) / len(a) - sum(g.choices(b, k=len(b))) / len(b) for _ in range(iters))
    return diffs[int(iters * 0.025)], diffs[int(iters * 0.975)]


def _nights(n: str) -> dict:
    out = {}
    for p in sorted(config.orders_dir().glob(f"*/{order_key(n)}.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        ok = sum(o["status"] == "approved" for o in doc["orders"])
        src = doc.get("source", "")
        out[doc["trade_date"]] = "missed" if src == "missed" else ("flat" if ok == 0 else src)
    return out


def report(date: str) -> str:
    path = data_dir() / "scores.csv"
    sc = pd.read_csv(path, dtype={"code": str}) if path.exists() else pd.DataFrame(columns=SCORE_FIELDS)
    done = sc[sc["status"].isin(["filled", "carried"])].copy()
    lines = [f"# 隔夜实验报告（截至 {date}）", "",
             "> 收盘集合竞价买 → 次日开盘集合竞价卖。ret_net 已扣佣金、印花税、过户费（含 5 元最低佣金）。"
             f"每组满 {TARGET_TRADES} 笔前，结论都只是观察。", "",
             "## 三组对照", "",
             "| 组 | 已平仓笔数 | 进度 | 每笔均值(净) | 胜率 | 中位数 | 未成交 | 账户净值 | 最大回撤 |",
             "|---|---|---|---|---|---|---|---|---|"]
    rets = {}
    for n in list(cfg()["accounts"]) + ["agent_veto"]:
        g = done[done["account"] == n]
        r = g["ret_net"].astype(float).tolist()
        rets[n] = r
        unf = int(((sc["account"] == n) & (sc["status"] == "unfilled")).sum())
        nav_s, mdd = "", ""
        if n != "agent_veto" and (config.accounts_dir() / "overnight" / n / "nav.csv").exists():
            nav = Account(acct_name(n)).nav()
            if nav:
                eq = [float(x["equity"]) for x in nav]
                nav_s = f"{eq[-1] / cfg()['initial_cash'] - 1:+.2%}"
                mdd = f"{max(float(x['drawdown']) for x in nav):.2%}"
        label = "agent 否决的票（反事实）" if n == "agent_veto" else n
        if r:
            lines.append(f"| {label} | {len(r)} | {min(len(r) / TARGET_TRADES, 1):.0%} | {sum(r) / len(r):+.3%} | "
                         f"{sum(x > 0 for x in r) / len(r):.1%} | {pd.Series(r).median():+.3%} | {unf} | {nav_s} | {mdd} |")
        else:
            lines.append(f"| {label} | 0 | 0% | | | | {unf} | {nav_s} | {mdd} |")
    lines += ["", "## Agent 是否有增益", ""]
    for other in ("quant", "random"):
        a, b = rets.get("agent", []), rets.get(other, [])
        if len(a) >= 10 and len(b) >= 10:
            lo, hi = _bootstrap_diff(a, b)
            verdict = "显著为正" if lo > 0 else ("显著为负" if hi < 0 else "不显著")
            lines.append(f"- agent − {other}：每笔均值差 {sum(a) / len(a) - sum(b) / len(b):+.3%}，"
                         f"bootstrap 95% 区间 [{lo:+.3%}, {hi:+.3%}] → **{verdict}**"
                         + ("" if min(len(a), len(b)) >= TARGET_TRADES else "（样本未满，仅供观察）"))
        else:
            lines.append(f"- agent − {other}：样本不足 10 笔，暂不计算")
    ag = done[(done["account"] == "agent") & pd.to_numeric(done["p_win"], errors="coerce").notna()].copy()
    lines += ["", "## Agent 预测校准", ""]
    if len(ag):
        ag["p"] = ag["p_win"].astype(float)
        brier = float(((ag["p"] - ag["win"].astype(float)) ** 2).mean())
        lines += [f"Brier 分数 {brier:.3f}（越低越好；永远报 0.5 的 Brier 是 0.250）", "",
                  "| p_win 区间 | 笔数 | 平均 p_win | 实际胜率 |", "|---|---|---|---|"]
        ag["b"] = pd.cut(ag["p"], [0, 0.4, 0.5, 0.6, 0.7, 1.0], include_lowest=True)
        for k, g in ag.groupby("b", observed=True):
            lines.append(f"| {k} | {len(g)} | {g['p'].mean():.2f} | {g['win'].astype(float).mean():.1%} |")
        lines += ["", "| 逻辑类型 | 笔数 | 每笔均值(净) | 胜率 |", "|---|---|---|---|"]
        for k, g in ag.groupby(ag["thesis_type"].fillna("未标注")):
            r = g["ret_net"].astype(float)
            lines.append(f"| {k} | {len(g)} | {r.mean():+.3%} | {(r > 0).mean():.1%} |")
    else:
        lines.append("还没有 Agent 平仓记录。")
    nights = _nights("agent")
    cnt = pd.Series(list(nights.values())).value_counts().to_dict() if nights else {}
    lines += ["", "## Agent 决策来源", "",
              f"共 {len(nights)} 晚：尾盘草稿 {cnt.get('draft', 0)}、午间 provisional {cnt.get('provisional', 0)}、"
              f"主动空仓 {cnt.get('flat', 0)}、**漏跑 {cnt.get('missed', 0)}**（漏跑说明定时任务没按时写草稿，需要排查）。",
              "", "---", "本报告由程序生成，仅供研究参考，不构成投资建议。"]
    out = data_dir() / "report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)
