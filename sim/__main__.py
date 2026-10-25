"""模拟交易命令行。在项目根目录运行：.venv/bin/python -m sim <命令>

日常只需要两个定时任务：
  每个交易日 15:10 之后   python -m sim execute     按冻结订单、以 14:45 价格成交
  每个交易日 20:00 之后   python -m sim evening     结算 → 初筛 → 生成并冻结次日订单 → 报告
非交易日运行会自动跳过；同一天重复运行不会重复结算或下单。
"""
import argparse
import datetime as dt
import sys

from . import broker, calendar as cal, config, dashboard, orders, pool, report, risk, screen, settle, strategies
from .ledger import Account, all_accounts
from .market import Market


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def resolve_date(arg: str | None) -> str:
    return arg or cal.today()


def cmd_init(a):
    bcfg = config.load("broker")
    start = resolve_date(a.date)
    for name, strategy in bcfg["accounts"].items():
        try:
            Account.create(name, strategy, bcfg["initial_cash"], start)
            log(f"创建账户 {name}（{strategy}），初始资金 {bcfg['initial_cash']:,}")
        except FileExistsError:
            log(f"账户 {name} 已存在，跳过")


def do_settle(date, market, rcfg, bcfg):
    reg = settle.regime(market, rcfg["regime"]["index"], date)
    log(f"{date} 市场状态：{reg}")
    for acct in all_accounts():
        r = settle.settle(acct, date, market, rcfg, reg)
        acct.save()
        if r.get("skipped"):
            log(f"[{acct.name}] {r['note']}")
            continue
        log(f"[{acct.name}] 总资产 {r['equity']:,.2f}，回撤 {r['drawdown']:.2%}，风控 {r['risk_state']}"
            + (f"，强制卖出 {r['pending_exits']}" if r["pending_exits"] else ""))
        for n in r["notes"]:
            log(f"[{acct.name}]   {n}")
    return reg


def do_plan(date, market, rcfg, bcfg, scfg, reg):
    trade_date = cal.next_day(date)
    today = cal.today()
    latest = today if cal.is_trading_day(today) else cal.prev_day(today)  # 周末/节假日的快照就是上一交易日收盘
    if date != latest:
        log(f"警告：初筛使用的是 {latest} 的全市场快照，不是 {date} 的数据")
    max_lot = bcfg["initial_cash"] * rcfg["position"]["max_single"]
    sc = screen.run(date, market, rcfg, scfg, max_lot, log)
    info = screen.info_map(sc)
    etf = bcfg["base_etf"]
    for acct in all_accounts():
        if orders.exists(trade_date, acct.name):
            log(f"[{acct.name}] {trade_date} 订单已冻结，跳过")
            continue
        if acct.state["last_settled"] != date:
            log(f"[{acct.name}] 尚未完成 {date} 结算，不生成订单")
            continue
        # 估值价：持仓用结算价，候选股用初筛收盘价，草稿里的其他股票和底仓 ETF 现取收盘价
        prices = {c: p["last_price"] for c, p in acct.positions.items()}
        if len(sc):
            prices.update(dict(zip(sc["code"], sc["close"].astype(float))))
        prices.setdefault(etf["code"], market.close(etf["code"], date))
        _, _, equity = acct.values(prices)
        state = settle.risk_state(acct.state, trade_date)
        band = risk.band_for(rcfg, reg, state)
        drafts = strategies.drafts(acct, trade_date, date, sc, band, scfg)
        for d in drafts:  # Agent 草稿里可能有初筛之外的股票
            if d["code"] not in prices and (px := market.close(d["code"], date)) is not None:
                prices[d["code"]] = px
            if d["code"] not in info and (row := screen.info_for(d["code"], date, market)):
                info[d["code"]] = row
        reviewed, band = risk.review(acct, drafts, {
            "trade_date": trade_date, "settle_date": date, "regime": reg, "risk_state": state, "rcfg": rcfg,
            "prices": prices, "info": info, "equity": equity, "base_etf": etf,
            "fixed_etf_weight": 0.98 if acct.state["strategy"] == "etf" else None})
        for i, o in enumerate(reviewed, 1):
            o["id"] = f"{trade_date.replace('-', '')}-{acct.name}-{i:02d}"
        doc = {"account": acct.name, "strategy": acct.state["strategy"], "trade_date": trade_date,
               "decided_on": date, "frozen_at": dt.datetime.now().isoformat(timespec="seconds"),
               "regime": reg, "risk_state": state, "band": band, "equity_estimate": round(equity, 2),
               "orders": reviewed}
        digest = orders.freeze(trade_date, acct.name, doc)
        ok = sum(o["status"] != "rejected" for o in reviewed)
        log(f"[{acct.name}] {trade_date} 订单已冻结：{ok} 笔有效 / {len(reviewed)} 笔，sha256 {digest[:12]}")
        for o in reviewed:
            log(f"[{acct.name}]   {o['status']:8} {o.get('name', '')}({o['code']}) 目标 {o['target_weight']:.1%}"
                + (f" | {'；'.join(o['risk_notes'])}" if o.get("risk_notes") else ""))
    orders.git_commit(f"sim: {date} 结算，冻结 {trade_date} 订单")


def cmd_settle(a):
    date = resolve_date(a.date)
    if not cal.is_trading_day(date):
        return log(f"{date} 不是交易日，跳过")
    do_settle(date, Market(), config.load("risk"), config.load("broker"))


def cmd_plan(a):
    date = resolve_date(a.date)
    if not cal.is_trading_day(date):
        return log(f"{date} 不是交易日，跳过")
    market, rcfg = Market(), config.load("risk")
    reg = settle.regime(market, rcfg["regime"]["index"], date)
    do_plan(date, market, rcfg, config.load("broker"), config.load("strategy"), reg)


def cmd_evening(a):
    date = resolve_date(a.date)
    if not cal.is_trading_day(date):
        return log(f"{date} 不是交易日，跳过")
    market, rcfg, bcfg, scfg = Market(config.load("broker")["exec_time"]), config.load("risk"), \
        config.load("broker"), config.load("strategy")
    reg = do_settle(date, market, rcfg, bcfg)
    do_plan(date, market, rcfg, bcfg, scfg, reg)
    log(f"报告：{report.build(date, market, bcfg)}")
    log(f"资金看板：{dashboard.build(date, market, bcfg)}")


def cmd_execute(a):
    date = resolve_date(a.date)
    if not cal.is_trading_day(date):
        return log(f"{date} 不是交易日，跳过")
    now = dt.datetime.now()
    bcfg, rcfg = config.load("broker"), config.load("risk")
    if date == cal.today() and now.strftime("%H:%M") <= bcfg["exec_time"]:
        return log(f"还没到执行时点 {bcfg['exec_time']}，稍后再运行")
    market = Market(bcfg["exec_time"])
    for acct in all_accounts():
        if not orders.exists(date, acct.name):
            log(f"[{acct.name}] {date} 没有冻结订单，不交易")
            continue
        doc = orders.load_verified(date, acct.name)
        results = broker.execute(acct, doc, date, market, bcfg, rcfg["position"]["stop_loss_pct"])
        acct.save()
        for r in results:
            if r["status"] in ("filled", "partial"):
                log(f"[{acct.name}] {r['status']:7} {r['side']} {r['name']}({r['code']}) {r['shares']:.0f} 股 @ {r['price']}"
                    + (f"（{r['note']}）" if r.get("note") else ""))
            elif r["status"] != "noop":
                log(f"[{acct.name}] {r['status']:7} {r.get('name', '')}({r.get('code', '')})：{r.get('note', '')}")
    orders.git_commit(f"sim: {date} 执行订单")


def cmd_pool(a):
    """主策略候选池。默认取今天或之前最近的交易日；全市场快照只有"当前"，所以只能当天收盘后跑当天。"""
    today = cal.today()
    latest = today if cal.is_trading_day(today) else cal.prev_day(today)
    date = a.date or latest
    if date != latest:
        log(f"警告：全市场快照是 {latest} 的，不是 {date} 的；候选池只能生成最近交易日")
        return
    bcfg, rcfg = config.load("broker"), config.load("risk")
    main = [n for n, s in bcfg["accounts"].items() if s == "agent"]
    holdings = [c for n in main for c in Account(n).stock_codes()] if config.accounts_dir().exists() else []
    out = pool.run(date, Market(bcfg["exec_time"]), rcfg, config.load("strategy")["pool"],
                   bcfg["initial_cash"] * rcfg["position"]["max_single"], holdings, log, force=a.force)
    log(f"候选池 {len(out)} 只：{pool.pool_dir() / (date + '.md')}")


def cmd_report(a):
    date = resolve_date(a.date)
    bcfg = config.load("broker")
    market = Market(bcfg["exec_time"])
    log(f"报告：{report.build(date, market, bcfg)}")
    log(f"资金看板：{dashboard.build(date, market, bcfg)}")


def cmd_status(a):
    for acct in all_accounts():
        st = acct.state
        print(f"== {acct.name}（{st['strategy']}）现金 {st['cash']:,.2f}  最近结算 {st['last_settled']}  "
              f"熔断至 {st['breaker_until']}  暂停 {st['halted']}")
        for c, p in st["positions"].items():
            print(f"   {p['name']}({c}) {p['shares']:.0f} 股  成本 {p['cost'] / p['shares']:.3f}  "
                  f"现价 {p['last_price']}  止损 {p.get('stop_price')}")
        if st["pending_exits"]:
            print(f"   待强制卖出：{st['pending_exits']}")


def cmd_resume(a):
    acct = Account(a.account)
    acct.state["halted"] = False
    acct.state["peak"] = acct.values({})[2]  # 以当前净值作为新的高点重新计算回撤
    acct.save()
    log(f"[{acct.name}] 已人工恢复，净值高点重置为 {acct.state['peak']:,.2f}")


def main():
    p = argparse.ArgumentParser(prog="python -m sim", description="A 股模拟交易")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in [("init", cmd_init, "创建账户"), ("settle", cmd_settle, "盘后结算"),
                            ("plan", cmd_plan, "初筛并冻结下一交易日订单"),
                            ("evening", cmd_evening, "晚间任务：结算 + 生成订单 + 报告"),
                            ("execute", cmd_execute, "执行当日冻结订单（14:45 之后任意时间）"),
                            ("report", cmd_report, "生成日报和资金看板"), ("status", cmd_status, "查看账户")]:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--date", help="YYYY-MM-DD，默认今天")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("pool", help="主策略候选池：全市场多路召回（收盘后运行，供 Agent 研究）")
    sp.add_argument("--date", help="YYYY-MM-DD，默认最近交易日")
    sp.add_argument("--force", action="store_true", help="忽略当天缓存重新生成")
    sp.set_defaults(fn=cmd_pool)
    sp = sub.add_parser("resume", help="系统暂停后人工恢复")
    sp.add_argument("account")
    sp.set_defaults(fn=cmd_resume)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
