"""模拟交易命令行。在项目根目录运行：.venv/bin/python -m sim <命令>

日常只需要两个定时任务：
  每个交易日 15:10 之后   python -m sim execute     按冻结订单、以 14:45 价格成交
  每个交易日 20:00 之后   python -m sim evening     结算 → 初筛 → 生成并冻结次日订单 → 报告
非交易日运行会自动跳过；同一天重复运行不会重复结算或下单。
"""
import argparse
import datetime as dt
import sys

from . import attrib, broker, calendar as cal, config, dashboard, orders, pool, pool_v2, report, risk, screen, settle, strategies
from . import etf as etf_mod
from .ledger import Account, all_accounts
from .market import Market
from .market import kind as code_kind


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def resolve_date(arg: str | None) -> str:
    return arg or cal.today()


def cmd_init(a):
    bcfg = config.load("broker")
    start = resolve_date(a.date)
    main_state = config.accounts_dir() / "main" / "state.json"
    for name, strategy in bcfg["accounts"].items():
        # 对照组要和 main 同起点才有可比性：etf_band 以 main 的起始日开户
        s = main_state if strategy == "etf_band" and main_state.exists() else None
        this_start = Account("main").state["start_date"] if s else start
        try:
            Account.create(name, strategy, bcfg["initial_cash"], this_start)
            log(f"创建账户 {name}（{strategy}），起始日 {this_start}，初始资金 {bcfg['initial_cash']:,}")
        except FileExistsError:
            log(f"账户 {name} 已存在，跳过")


def do_settle(date, market, rcfg, bcfg):
    index = rcfg["regime"]["index"]
    if not settle.data_fresh(market, index, date):
        raise settle.StaleData(f"基准指数 {index} 的日线还没更新到 {date}，当天不结算、不冻结")
    df = market.daily(index, date)
    if df is None or len(df) < 125:
        log(f"警告：{index} 只有 {0 if df is None else len(df)} 行日线，算不出均线，按防守档处理")
    reg = settle.regime(market, index, date)
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


def do_plan(date, market, rcfg, bcfg, scfg, reg) -> int:
    index = rcfg["regime"]["index"]
    if not settle.data_fresh(market, index, date):
        raise settle.StaleData(f"基准指数 {index} 的日线还没更新到 {date}，不生成订单")
    trade_date = cal.next_day(date)
    today = cal.today()
    latest = today if cal.is_trading_day(today) else cal.prev_day(today)  # 周末/节假日的快照就是上一交易日收盘
    if date != latest:
        log(f"警告：初筛使用的是 {latest} 的全市场快照，不是 {date} 的数据")
    max_lot = bcfg["initial_cash"] * rcfg["position"]["max_single"]
    sc = screen.run(date, market, rcfg, scfg, max_lot, log)
    info = screen.info_map(sc)
    etf = bcfg["base_etf"]
    failed = []
    for acct in all_accounts():
        if orders.exists(trade_date, acct.name):
            log(f"[{acct.name}] {trade_date} 订单已冻结，跳过")
            continue
        if acct.state["last_settled"] != date:
            log(f"[{acct.name}] 尚未完成 {date} 结算，不生成订单")
            continue
        # 单个账户出错不能连累其他账户：账户按字母序处理，main 失败时后面几个也要照常冻结
        try:
            # 估值价：持仓用结算价，候选股用初筛收盘价，草稿里的其他股票和底仓 ETF 现取收盘价
            prices = {c: p["last_price"] for c, p in acct.positions.items()}
            if len(sc):
                prices.update(dict(zip(sc["code"], sc["close"].astype(float))))
            prices.setdefault(etf["code"], market.close(etf["code"], date))
            _, _, equity = acct.values(prices)
            state = settle.risk_state(acct.state, trade_date)
            band = risk.band_for(rcfg, reg, state)
            drafts, draft_meta, draft_errors = strategies.drafts(acct, trade_date, date, sc, band, scfg)
            decided = str(draft_meta.get("decided_on") or "")
            if drafts and decided and decided < date:
                draft_errors.append(f"草稿基于 {decided} 的数据，早于结算日 {date}，整份丢弃（不拿旧数据下单）")
                drafts = []
            for d in drafts:  # Agent 草稿里可能有初筛之外的股票/ETF
                if d["code"] not in prices and (px := market.close(d["code"], date)) is not None:
                    prices[d["code"]] = px
                if (code_kind(d["code"]) == "stock" and d["code"] not in info
                        and (row := screen.info_for(d["code"], date, market))):
                    info[d["code"]] = row
            strategy = acct.state["strategy"]
            reviewed, band = risk.review(acct, drafts, {
                "trade_date": trade_date, "settle_date": date, "regime": reg, "risk_state": state, "rcfg": rcfg,
                "prices": prices, "info": info, "equity": equity, "base_etf": etf, "market": market,
                # etf 固定 98%；etf_band 跟当日仓位区间上沿，用来把"仓位控制"从"选股"里拆出来
                "fixed_etf_weight": 0.98 if strategy == "etf" else (band["max"] if strategy == "etf_band" else None)})
            for i, o in enumerate(reviewed, 1):
                o["id"] = f"{trade_date.replace('-', '')}-{acct.name}-{i:02d}"
            doc = {"account": acct.name, "strategy": strategy, "strategy_version": scfg.get("version"),
                   "trade_date": trade_date,
                   "decided_on": date, "frozen_at": dt.datetime.now().isoformat(timespec="seconds"),
                   "regime": reg, "risk_state": state, "band": band, "equity_estimate": round(equity, 2),
                   "meta": draft_meta, "draft_errors": draft_errors, "orders": reviewed}
            digest = orders.freeze(trade_date, acct.name, doc)
            ok = sum(o["status"] != "rejected" for o in reviewed)
            log(f"[{acct.name}] {trade_date} 订单已冻结：{ok} 笔有效 / {len(reviewed)} 笔，sha256 {digest[:12]}")
            for e in draft_errors:
                log(f"[{acct.name}]   草稿问题：{e}")
            for o in reviewed:
                log(f"[{acct.name}]   {o['status']:8} {o.get('name', '')}({o['code']}) 目标 {o['target_weight']:.1%}"
                    + (f" | {'；'.join(o['risk_notes'])}" if o.get("risk_notes") else ""))
        except Exception as e:  # 一个账户出错，其他账户照常冻结
            failed.append(acct.name)
            log(f"[{acct.name}] 生成订单失败：{type(e).__name__}: {e}")
    committed = orders.git_commit(f"sim: {date} 结算，冻结 {trade_date} 订单")
    if failed:
        log(f"今日未能冻结的账户：{', '.join(failed)}")
    return 1 if failed or not committed else 0


def cmd_settle(a):
    date = resolve_date(a.date)
    if not cal.is_trading_day(date):
        return log(f"{date} 不是交易日，跳过")
    try:
        do_settle(date, Market(), config.load("risk"), config.load("broker"))
    except settle.StaleData as e:
        log(f"中止：{e}")
        return 1


def cmd_plan(a):
    date = resolve_date(a.date)
    if not cal.is_trading_day(date):
        return log(f"{date} 不是交易日，跳过")
    market, rcfg = Market(), config.load("risk")
    try:
        reg = settle.regime(market, rcfg["regime"]["index"], date)
        return do_plan(date, market, rcfg, config.load("broker"), config.load("strategy"), reg)
    except settle.StaleData as e:
        log(f"中止：{e}")
        return 1


def cmd_evening(a):
    date = resolve_date(a.date)
    if not cal.is_trading_day(date):
        return log(f"{date} 不是交易日，跳过")
    market, rcfg, bcfg, scfg = Market(config.load("broker")["exec_time"]), config.load("risk"), \
        config.load("broker"), config.load("strategy")
    try:
        # 先补执行再结算：execute 漏跑或晚跑时，成交必须先入账，否则当天净值漏掉成交
        catch_up_execute(date, market, bcfg, rcfg)
        reg = do_settle(date, market, rcfg, bcfg)
        rc = do_plan(date, market, rcfg, bcfg, scfg, reg)
    except settle.StaleData as e:
        log(f"中止：{e}")
        return 1
    log(f"报告：{report.build(date, market, bcfg)}")
    log(f"资金看板：{dashboard.build(date, market, bcfg)}")
    return rc


def run_orders(date, market, bcfg, rcfg, only_pending=False) -> list:
    """执行当日冻结订单，返回失败的账户名。only_pending=True 时只补跑还没执行过的账户。"""
    failed = []
    for acct in all_accounts():
        try:
            if not orders.exists(date, acct.name):
                if not only_pending:
                    log(f"[{acct.name}] {date} 没有冻结订单，不交易")
                continue
            if only_pending and date in acct.state["executed"]:
                continue
            if acct.state["last_settled"] and acct.state["last_settled"] >= date:
                log(f"[{acct.name}] {date} 已结算，不能补执行，跳过")
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
        except Exception as e:
            failed.append(acct.name)
            log(f"[{acct.name}] 执行失败：{type(e).__name__}: {e}")
    return failed


def catch_up_execute(date, market, bcfg, rcfg) -> int:
    """结算前补执行当日还没跑过的冻结订单，避免成交不进当天净值。"""
    pending = [a.name for a in all_accounts()
               if orders.exists(date, a.name) and date not in a.state["executed"]]
    if not pending:
        return 0
    log(f"{date} 有 {len(pending)} 个账户的冻结订单尚未执行，先补执行：{', '.join(pending)}")
    failed = run_orders(date, market, bcfg, rcfg, only_pending=True)
    orders.git_commit(f"sim: {date} 补执行订单")
    return len(failed)


def cmd_execute(a):
    date = resolve_date(a.date)
    if not cal.is_trading_day(date):
        return log(f"{date} 不是交易日，跳过")
    now = dt.datetime.now()
    bcfg, rcfg = config.load("broker"), config.load("risk")
    if date == cal.today() and now.strftime("%H:%M") <= bcfg["exec_time"]:
        return log(f"还没到执行时点 {bcfg['exec_time']}，稍后再运行")
    market = Market(bcfg["exec_time"])
    failed = run_orders(date, market, bcfg, rcfg)
    committed = orders.git_commit(f"sim: {date} 执行订单")
    return 1 if failed or not committed else 0


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
    market = Market(bcfg["exec_time"])
    regime = settle.regime(market, rcfg["regime"]["index"], date)
    out = pool.run(date, market, rcfg, config.load("strategy")["pool"],
                   bcfg["initial_cash"] * rcfg["position"]["max_single"], holdings, log,
                   force=a.force, regime=regime)
    log(f"候选池 {len(out)} 只（分诊排序，市场状态 {regime}）：{pool.pool_dir() / (date + '.md')}")
    try:
        edf = etf_mod.build_pool(date, market, log)
        log(f"ETF 候选池 {len(edf)} 只：{pool.pool_dir() / (date + '_etf.md')}")
    except Exception as e:  # ETF 池失败不影响个股候选池与下单链路
        log(f"ETF 候选池生成失败（不影响主流程）：{type(e).__name__}: {e}")


def cmd_pool2(a):
    """影子候选池 v2：只读影子，不进 main 下单链路。复用当天 features 缓存，不额外取数。"""
    import pandas as pd
    today = cal.today()
    latest = today if cal.is_trading_day(today) else cal.prev_day(today)
    date = a.date or latest
    if date != latest:
        log(f"警告：全市场快照是 {latest} 的，不是 {date} 的；影子池只能生成最近交易日")
        return
    bcfg, rcfg, scfg = config.load("broker"), config.load("risk"), config.load("strategy")
    market = Market(bcfg["exec_time"])
    regime = settle.regime(market, rcfg["regime"]["index"], date)
    cache = config.cache_dir() / "pool" / f"{date}_features.csv"
    holdings = [c for n, s in bcfg["accounts"].items() if s == "agent"
                for c in Account(n).stock_codes()] if config.accounts_dir().exists() else []
    if not cache.exists():
        log("未找到当天 features 缓存，先生成 v1 候选池（会联网取数）")
        pool.run(date, market, rcfg, scfg["pool"], bcfg["initial_cash"] * rcfg["position"]["max_single"],
                 holdings, log, regime=regime)
    feat = pd.read_csv(cache, dtype={"code": str})
    chans = pool.channels(feat, scfg["pool"])
    out = pool_v2.run(date, feat, chans, regime, holdings, pool.load_watchlist(),
                      scfg.get("pool_v2", {}), log, force=a.force)
    log(f"影子候选池 v2：{len(out)} 只（市场状态 {regime}）：{pool_v2.pool2_dir() / (date + '.md')}")


def cmd_attrib(a):
    date = resolve_date(a.date)
    log(f"归因报告：{attrib.run(date)}")


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
                            ("report", cmd_report, "生成日报和资金看板"),
                            ("attrib", cmd_attrib, "策略归因（给策略进化官）：账户对照 + 按通道/置信度/版本分组"), ("status", cmd_status, "查看账户")]:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--date", help="YYYY-MM-DD，默认今天")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("pool", help="主策略候选池：全市场多路召回 + 分诊（收盘后运行，供 Agent 研究）")
    sp.add_argument("--date", help="YYYY-MM-DD，默认最近交易日")
    sp.add_argument("--force", action="store_true", help="忽略当天缓存重新生成")
    sp.set_defaults(fn=cmd_pool)
    sp = sub.add_parser("pool2", help="影子候选池 v2：多风格 + regime 配额（只读影子，不进 main 下单链路）")
    sp.add_argument("--date", help="YYYY-MM-DD，默认最近交易日")
    sp.add_argument("--force", action="store_true", help="忽略当天缓存重新生成")
    sp.set_defaults(fn=cmd_pool2)
    sp = sub.add_parser("resume", help="系统暂停后人工恢复")
    sp.add_argument("account")
    sp.set_defaults(fn=cmd_resume)
    a = p.parse_args()
    rc = a.fn(a)
    return 0 if rc is None else rc


if __name__ == "__main__":
    sys.exit(main())
