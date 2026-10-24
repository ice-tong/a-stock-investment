"""绩效指标与每日 HTML 报告（单文件、无外部依赖，浏览器直接打开）。"""
import html
import json
import math

from . import config
from .ledger import all_accounts

LABELS = {"main": "主策略", "quant": "纯量化组", "random": "随机选股组", "etf": "只持底仓 ETF"}
COLORS = ["#2563eb", "#d97706", "#7c3aed", "#059669", "#6b7280"]


def metrics(acct) -> dict:
    nav = acct.nav()
    eq = [float(r["equity"]) for r in nav]
    m = {"days": len(eq)}
    if not eq:
        return m
    init = acct.state["initial_cash"]
    series = [init] + eq
    rets = [b / a - 1 for a, b in zip(series, series[1:])]
    m["equity"] = eq[-1]
    m["total"] = eq[-1] / init - 1
    m["ann"] = (1 + m["total"]) ** (250 / len(eq)) - 1 if len(eq) >= 20 else None
    peak, mdd = init, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    m["mdd"] = mdd
    m["calmar"] = m["ann"] / mdd if m["ann"] is not None and mdd > 0 else None
    if len(rets) >= 20:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        down = [min(r, 0) for r in rets]
        dsd = math.sqrt(sum(d * d for d in down) / len(down))
        m["vol"] = sd * math.sqrt(250)
        m["sharpe"] = mean / sd * math.sqrt(250) if sd > 0 else None
        m["sortino"] = mean / dsd * math.sqrt(250) if dsd > 0 else None
    trades = acct.trades()
    sells = [float(t["realized_pnl"]) for t in trades if t["side"] == "sell" and t["realized_pnl"] not in ("", None)]
    wins, losses = [x for x in sells if x > 0], [x for x in sells if x <= 0]
    m["sells"] = len(sells)
    m["win_rate"] = len(wins) / len(sells) if sells else None
    m["payoff"] = (sum(wins) / len(wins)) / abs(sum(losses) / len(losses)) if wins and losses and sum(losses) else None
    m["costs"] = sum(float(t[k] or 0) for t in trades if t["side"] in ("buy", "sell")
                     for k in ("commission", "stamp_tax", "transfer_fee", "slippage_cost"))
    buys = sum(float(t["amount"] or 0) for t in trades if t["side"] == "buy")
    m["turnover"] = buys / (sum(eq) / len(eq))
    return m


def _pct(x, digits=1):
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def _num(x, digits=2):
    return "—" if x is None else f"{x:.{digits}f}"


def _svg_lines(series: dict, height=220, fmt=lambda v: f"{v:.2f}", zero=None) -> str:
    """series: 名称 -> [(日期, 值)]。生成带坐标轴标签的简单折线图。"""
    pts = [v for s in series.values() for _, v in s]
    dates = sorted({d for s in series.values() for d, _ in s})
    if len(dates) < 2:
        return '<p class="muted">数据不足两天，暂无曲线。</p>'
    lo, hi = min(pts), max(pts)
    if zero is not None:
        lo, hi = min(lo, zero), max(hi, zero)
    pad = (hi - lo) * 0.08 or 0.01
    lo, hi = lo - pad, hi + pad
    W, H, L, B = 760, height, 56, 24
    x = {d: L + (W - L - 8) * i / (len(dates) - 1) for i, d in enumerate(dates)}
    y = lambda v: 8 + (H - B - 8) * (hi - v) / (hi - lo)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" class="chart">']
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        parts.append(f'<line x1="{L}" x2="{W - 8}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                     f'<text x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end" class="axis">{fmt(v)}</text>')
    for d in (dates[0], dates[len(dates) // 2], dates[-1]):
        parts.append(f'<text x="{x[d]:.1f}" y="{H - 6}" text-anchor="middle" class="axis">{d[5:]}</text>')
    for (name, s), color in zip(series.items(), COLORS):
        path = " ".join(f"{x[d]:.1f},{y(v):.1f}" for d, v in s)
        parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
    parts.append("</svg>")
    legend = "".join(f'<span><i style="background:{c}"></i>{html.escape(n)}</span>'
                     for n, c in zip(series, COLORS))
    return "".join(parts) + f'<div class="legend">{legend}</div>'


def build(date: str, market, bcfg: dict) -> str:
    accts = all_accounts()
    rows, curves, dds, details = [], {}, {}, []
    all_dates = set()
    for acct in accts:
        m = metrics(acct)
        label = LABELS.get(acct.name, acct.name)
        nav = acct.nav()
        init = acct.state["initial_cash"]
        curves[label] = [(r["date"], float(r["equity"]) / init) for r in nav]
        dds[label] = [(r["date"], -float(r["drawdown"])) for r in nav]
        all_dates |= {r["date"] for r in nav}
        last = nav[-1] if nav else {}
        rows.append(f"<tr><td>{label}</td><td>{_num(m.get('equity'), 0)}</td><td>{_pct(m.get('total'))}</td>"
                    f"<td>{_pct(m.get('ann'))}</td><td>{_pct(m.get('mdd'))}</td><td>{_num(m.get('calmar'))}</td>"
                    f"<td>{_num(m.get('sharpe'))}</td><td>{_pct(m.get('win_rate'), 0)}</td>"
                    f"<td>{_num(m.get('payoff'))}</td><td>{m.get('sells', 0)}</td><td>{_num(m.get('costs'), 0)}</td>"
                    f"<td>{_pct(last.get('stock_weight') and float(last['stock_weight']), 0)}</td>"
                    f"<td>{html.escape(last.get('risk_state', '—'))}</td></tr>")
        details.append(_account_detail(acct, label, date))

    bench = bcfg["benchmark"]
    df = market.daily(bench)
    if df is not None and all_dates:
        ds = sorted(d for d in all_dates if d in df.index)
        if ds:
            base = market.prev_close(bench, ds[0]) or float(df.loc[ds[0], "close"])
            curves["沪深300 指数"] = [(d, float(df.loc[d, "close"]) / base) for d in ds]

    regime = next((a.nav()[-1]["regime"] for a in accts if a.nav()), "—")
    page = TEMPLATE.format(
        date=date, regime=html.escape(regime), start=min((a.state["start_date"] for a in accts), default="—"),
        rows="".join(rows),
        equity_chart=_svg_lines(curves, fmt=lambda v: f"{v:.3f}"),
        dd_chart=_svg_lines(dds, height=160, fmt=lambda v: f"{v * 100:.0f}%", zero=0.0),
        details="".join(details))
    out = config.reports_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{date}.html").write_text(page, encoding="utf-8")
    (out / "latest.html").write_text(page, encoding="utf-8")
    return str(out / "latest.html")


def _account_detail(acct, label, date) -> str:
    st = acct.state
    pos_rows = "".join(
        f"<tr><td>{html.escape(p['name'])}<span class='muted'> {c}</span></td><td>{p['shares']:.0f}</td>"
        f"<td>{p['cost'] / p['shares']:.3f}</td><td>{p['last_price']}</td>"
        f"<td class='{'up' if p['last_price'] * p['shares'] >= p['cost'] else 'down'}'>"
        f"{_pct(p['last_price'] * p['shares'] / p['cost'] - 1)}</td><td>{p.get('stop_price') or '—'}</td>"
        f"<td>{p['first_buy_date']}</td></tr>"
        for c, p in st["positions"].items()) or "<tr><td colspan=7 class='muted'>空仓</td></tr>"
    trades = [t for t in acct.trades() if t["date"] == date]
    trade_rows = "".join(
        f"<tr><td>{t['side']}</td><td>{html.escape(t['name'])}</td><td>{t['shares']}</td><td>{t['price']}</td>"
        f"<td>{t['amount']}</td><td>{t['realized_pnl']}</td><td>{html.escape(t['reason_type'])}</td>"
        f"<td>{html.escape(t['fill_source'])} {html.escape(t['note'])}</td></tr>" for t in trades
    ) or "<tr><td colspan=8 class='muted'>当日无成交</td></tr>"
    nxt = _next_orders(acct.name, date)
    pending = "、".join(f"{st['positions'][c]['name']}（{r}）" for c, r in st["pending_exits"].items()) or "无"
    return f"""
<section class="card"><h3>{label} <span class="muted">现金 {st['cash']:,.0f}</span></h3>
<p class="muted">待强制卖出：{html.escape(pending)}　熔断至：{st['breaker_until'] or '—'}　暂停：{'是' if st['halted'] else '否'}</p>
<div class="scroll"><table><tr><th>持仓</th><th>股数</th><th>成本价</th><th>现价</th><th>浮动盈亏</th><th>止损价</th><th>首次买入</th></tr>{pos_rows}</table></div>
<h4>{date} 成交</h4>
<div class="scroll"><table><tr><th>方向</th><th>名称</th><th>股数</th><th>价格</th><th>金额</th><th>已实现盈亏</th><th>原因</th><th>来源/备注</th></tr>{trade_rows}</table></div>
{nxt}</section>"""


def _next_orders(account, date) -> str:
    from . import calendar as cal
    try:
        nd = cal.next_day(date)
        doc = json.loads((config.orders_dir() / nd / f"{account}.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    rows = "".join(
        f"<tr><td>{html.escape(o['status'])}</td><td>{html.escape(o.get('name', ''))}</td>"
        f"<td>{o['target_weight']:.1%}</td><td>{html.escape(o.get('reason', ''))}</td>"
        f"<td>{html.escape('；'.join(o.get('risk_notes', [])))}</td></tr>" for o in doc["orders"]
    ) or "<tr><td colspan=5 class='muted'>无订单</td></tr>"
    return (f"<h4>{nd} 已冻结订单</h4><div class='scroll'><table><tr><th>风控</th><th>名称</th><th>目标仓位</th>"
            f"<th>理由</th><th>风控说明</th></tr>{rows}</table></div>")


TEMPLATE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>模拟交易日报</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--text:#1f2328;--muted:#6b7280;--line:#e5e7eb;--up:#dc2626;--down:#16a34a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111316;--card:#1a1d21;--text:#e6e6e6;--muted:#9aa0a6;--line:#2c3036}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,"PingFang SC",sans-serif}}
main{{max-width:960px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} h3{{margin:0 0 6px}} h4{{margin:16px 0 6px;font-size:14px}}
.muted{{color:var(--muted);font-weight:normal}} .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:16px 0}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}} th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}} th{{color:var(--muted);font-weight:500}}
.scroll{{overflow-x:auto}} .up{{color:var(--up)}} .down{{color:var(--down)}}
.chart{{width:100%;height:auto}} .grid{{stroke:var(--line)}} .axis{{fill:var(--muted);font-size:11px}}
.legend{{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--muted)}} .legend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}}
</style></head><body><main>
<h1>模拟交易日报 · {date}</h1>
<p class="muted">市场状态：{regime}　开始日期：{start}　仅为模拟，不构成投资建议</p>
<section class="card"><h3>绩效对比</h3><div class="scroll"><table>
<tr><th>账户</th><th>总资产</th><th>累计收益</th><th>年化</th><th>最大回撤</th><th>收益回撤比</th><th>Sharpe</th><th>胜率</th><th>盈亏比</th><th>卖出笔数</th><th>累计成本</th><th>个股仓位</th><th>风控</th></tr>
{rows}</table></div><p class="muted">年化、收益回撤比、Sharpe 需满 20 个交易日才计算。</p></section>
<section class="card"><h3>净值曲线</h3>{equity_chart}</section>
<section class="card"><h3>回撤</h3>{dd_chart}</section>
{details}
</main></body></html>"""
