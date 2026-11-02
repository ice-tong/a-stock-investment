#!/usr/bin/env python
"""生成项目状态看板（主策略视角）：web/index.html 与 dist/index.html。

数据源（只读）：accounts/*/state.json|nav.csv|trades.csv、orders/<最新交易日>/、
reports/sim/runlog.json、config/risk.yaml|broker.yaml、docs/进展与交接.md。
由定时任务在每次成功运行后调用，也可手动运行。
"""
import csv
import datetime
import html
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACCOUNTS = [("main", "main · 主策略"), ("quant", "quant · 量化初筛"),
            ("random", "random · 随机对照"), ("etf_band", "etf_band · 仓位对照"),
            ("etf", "etf · 底仓对照")]
# 挑战者：独立纸面实验（accounts/challenger/*，不进 all_accounts），看板只读展示
CHALLENGERS = [
    ("challenger/s002", "S-002 · 小市值合成分 50 只", "data/s002",
     "第四轮 F4：流通市值最小 20% 内按 SUE/EP/低异常换手/低应计合成分取 50 只，月末信号、次日开盘等权换仓。评估：满 12 期年化 ≥11% 且累计跑赢 K3。"),
    ("challenger/s003", "S-003 · 排雷后集中 10 只（观察级）", "data/s003",
     "第五轮 R5：S-002 池子剔除退市风险与解禁减持、增持回购加分，取前 10 只、前 30 名缓冲。回测 t=2.19 未过存档门槛，只作观察。"),
    ("challenger/pead", "PEAD · 财报反应日隔夜", "data/pead",
     "财报反应日涨幅 >5% 收盘买入，次日 +3% 限价、未成交收盘卖。kill：满 100 笔均值 ≤0。"),
]
CONTROL_NOTE = {"main": "本看板主角", "quant": "对照组", "random": "对照组",
                "etf_band": "对照组", "etf": "对照组"}
REASON_CN = {"entry": "新开仓", "exit": "调仓卖出", "park": "底仓停泊",
             "stop_loss": "止损卖出", "time_stop": "时间止损", "breaker": "熔断卖出", "halt": "停机卖出"}
REASON_TAG = {"entry": "tag-entry", "park": "tag-park", "exit": "tag-skipped",
              "stop_loss": "tag-rejected", "time_stop": "tag-rejected",
              "breaker": "tag-rejected", "halt": "tag-rejected"}
WATCH_CN = {"watching": "观察中", "researched": "已研究", "rejected": "已否决"}

CSS = """
  :root {
    --bg: #0b0f17; --panel: #121826; --border: #1f2a3d; --text: #dce4f1; --muted: #7d8aa0;
    --accent: #4f8cff; --up: #f0524f; --down: #2fbf71; --warn: #e8b34b; --chip: #1a2436;
    font-size: 15px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); line-height: 1.55; padding-bottom: 48px;
         font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif; }
  .num { font-family: "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace; font-variant-numeric: tabular-nums; }
  .wrap { max-width: 1120px; margin: 0 auto; padding: 0 20px; }
  header { border-bottom: 1px solid var(--border); background: linear-gradient(180deg, #101828 0%, var(--bg) 100%);
           padding: 26px 0 20px; margin-bottom: 24px; }
  .head-row { display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px; }
  h1 { font-size: 1.4rem; font-weight: 700; letter-spacing: .5px; }
  .sub { color: var(--muted); font-size: .85rem; }
  .badges { margin-left: auto; display: flex; gap: 8px; flex-wrap: wrap; }
  .badge { display: inline-flex; align-items: center; gap: 6px; background: var(--chip); border: 1px solid var(--border);
           border-radius: 999px; padding: 3px 12px; font-size: .78rem; }
  .badge .dot { width: 8px; height: 8px; border-radius: 50%; }
  .dot-defense { background: var(--warn); } .dot-offense { background: var(--up); }
  .dot-neutral { background: var(--accent); } .dot-normal { background: var(--down); }
  section { margin-bottom: 26px; }
  h2 { font-size: .95rem; color: var(--muted); font-weight: 600; letter-spacing: 1.5px; margin-bottom: 12px;
       display: flex; align-items: center; gap: 10px; }
  h2::after { content: ""; flex: 1; height: 1px; background: var(--border); }
  .grid { display: grid; gap: 14px; }
  .grid-4 { grid-template-columns: repeat(4, 1fr); }
  .grid-2 { grid-template-columns: repeat(2, 1fr); }
  @media (max-width: 860px) { .grid-4 { grid-template-columns: repeat(2, 1fr); } .grid-2 { grid-template-columns: 1fr; } }
  @media (max-width: 520px) { .grid-4 { grid-template-columns: 1fr; } }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }
  .card .label { color: var(--muted); font-size: .78rem; margin-bottom: 4px; }
  .card .value { font-size: 1.45rem; font-weight: 700; }
  .card .foot { font-size: .75rem; color: var(--muted); margin-top: 6px; }
  .hero { border-color: rgba(79,140,255,.35); }
  table { width: 100%; border-collapse: collapse; font-size: .88rem; }
  th { text-align: left; color: var(--muted); font-weight: 500; font-size: .78rem; padding: 8px 12px;
       border-bottom: 1px solid var(--border); }
  td { padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  .table-card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; overflow-x: auto; }
  .tag { display: inline-block; padding: 1px 9px; border-radius: 6px; font-size: .75rem;
         border: 1px solid var(--border); background: var(--chip); }
  .tag-ok, .tag-approved { color: var(--down); border-color: rgba(47,191,113,.35); background: rgba(47,191,113,.08); }
  .tag-rejected, .tag-failed { color: var(--up); border-color: rgba(240,82,79,.35); background: rgba(240,82,79,.08); }
  .tag-skipped { color: var(--muted); }
  .tag-alert { color: var(--warn); border-color: rgba(232,179,75,.35); background: rgba(232,179,75,.08); }
  .tag-park { color: var(--accent); border-color: rgba(79,140,255,.35); background: rgba(79,140,255,.08); }
  .tag-entry { color: var(--warn); border-color: rgba(232,179,75,.35); background: rgba(232,179,75,.08); }
  .tag-pending { color: var(--warn); border-color: rgba(232,179,75,.55); background: rgba(232,179,75,.12); }
  .tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
  .tab { appearance: none; background: var(--panel); border: 1px solid var(--border); color: var(--muted);
         padding: 7px 16px; border-radius: 999px; font: inherit; font-size: .86rem; cursor: pointer; }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--text); border-color: rgba(79,140,255,.55); background: rgba(79,140,255,.12); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  .subhead { font-size: .9rem; color: var(--muted); font-weight: 600; letter-spacing: .5px; margin: 22px 0 8px; }
  pre.journal { white-space: pre-wrap; font-size: .8rem; line-height: 1.6; color: var(--text); background: var(--chip);
                border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-top: 8px;
                max-height: 420px; overflow: auto; font-family: inherit; }
  .up { color: var(--up); } .down { color: var(--down); }
  .muted { color: var(--muted); } .small { font-size: .78rem; }
  .spark { width: 100%; height: auto; display: block; margin-top: 10px; }
  details { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 12px 18px; }
  details + details { margin-top: 12px; }
  summary { cursor: pointer; font-weight: 600; font-size: .9rem; color: var(--text); }
  details[open] summary { margin-bottom: 10px; }
  .kv { display: grid; grid-template-columns: auto 1fr; gap: 4px 16px; font-size: .84rem; }
  .kv dt { color: var(--muted); } .kv dd { text-align: right; }
  footer { color: var(--muted); font-size: .75rem; border-top: 1px solid var(--border); padding-top: 16px; }
"""


def esc(s) -> str:
    return html.escape(str(s))


def money(v) -> str:
    return f"{v:,.2f}"


def pct(v, signed=True) -> str:
    return f"{v * 100:+.2f}%" if signed else f"{v * 100:.2f}%"


def cls(v) -> str:
    return "up" if v > 0 else ("down" if v < 0 else "muted")


def read_csv(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_account(name):
    state = json.loads((ROOT / "accounts" / name / "state.json").read_text(encoding="utf-8"))
    nav = read_csv(ROOT / "accounts" / name / "nav.csv")
    trades = read_csv(ROOT / "accounts" / name / "trades.csv")
    last = nav[-1] if nav else None
    prev = nav[-2] if len(nav) > 1 else None
    equity = float(last["equity"]) if last else float(state["initial_cash"])
    initial = float(state["initial_cash"])
    return {
        "name": name, "state": state, "nav": nav, "trades": trades,
        "equity": equity, "cash": float(state["cash"]),
        "pnl": equity - initial, "pnl_pct": equity / initial - 1,
        "day_pnl": equity - float(prev["equity"]) if prev else 0.0,
        "drawdown": float(last["drawdown"]) if last else 0.0,
        "stock_w": float(last["stock_weight"]) if last else 0.0,
        "etf_w": float(last["etf_weight"]) if last else 0.0,
        "active_w": float(last.get("active_etf_weight") or 0) if last else 0.0,
        "last_settled": state.get("last_settled", ""),
        "positions": state.get("positions", {}),
    }


def sparkline(values, initial, label="净值曲线") -> str:
    if len(values) < 2:
        return ('<p class="small muted">净值曲线自第 2 个结算日起显示（当前仅 1 个数据点）。</p>')
    w, h, pad = 600, 150, 12
    lo, hi = min(values + [initial]), max(values + [initial])
    if hi - lo < 1e-9:
        hi, lo = hi + 1, lo - 1
    def xy(i, v):
        x = pad + i * (w - 2 * pad) / (len(values) - 1)
        y = h - pad - (v - lo) / (hi - lo) * (h - 2 * pad)
        return x, y
    pts = " ".join(f"{xy(i, v)[0]:.1f},{xy(i, v)[1]:.1f}" for i, v in enumerate(values))
    bx, by = xy(0, initial)
    lx, ly = xy(len(values) - 1, values[-1])
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">'
            f'<line x1="{pad}" y1="{by:.1f}" x2="{w - pad}" y2="{by:.1f}" stroke="#39445a" stroke-width="1" stroke-dasharray="4 4"/>'
            f'<polyline points="{pts}" fill="none" stroke="#4f8cff" stroke-width="2"/>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" fill="#4f8cff"/>'
            f'<text x="{lx:.1f}" y="{ly - 8:.1f}" fill="#dce4f1" font-size="12" text-anchor="end">{money(values[-1])}</text>'
            f'<text x="{pad}" y="{by - 5:.1f}" fill="#7d8aa0" font-size="11">初始 {money(initial)}</text></svg>')


def positions_table(account) -> str:
    rows = []
    for code, p in account["positions"].items():
        value = p["shares"] * p.get("last_price", 0)
        pp = value / p["cost"] - 1 if p["cost"] else 0
        rows.append(
            f"<tr><td>{esc(p['name'])} <span class='muted small num'>{esc(code)}</span></td>"
            f"<td class='num'>{p['shares']:.0f}</td><td class='num'>{p['cost'] / p['shares']:.3f}</td>"
            f"<td class='num'>{p.get('last_price', 0)}</td><td class='num'>{money(value)}</td>"
            f"<td class='num {cls(pp)}'>{pct(pp)}</td><td class='num'>{p.get('stop_price') or '—'}</td>"
            f"<td class='num'>{esc(p.get('first_buy_date', ''))}</td></tr>")
    return "".join(rows) or "<tr><td colspan=8 class='muted'>空仓</td></tr>"


def trades_table(account, limit=8) -> str:
    rows = []
    for t in account["trades"][-limit:][::-1]:
        fees = sum(float(t[k] or 0) for k in ("commission", "stamp_tax", "transfer_fee"))
        side = "买入" if t["side"] == "buy" else "卖出"
        rows.append(
            f"<tr><td class='num'>{esc(t['date'])}</td><td>{side}</td>"
            f"<td>{esc(t['name'])} <span class='muted small num'>{esc(t['code'])}</span></td>"
            f"<td class='num'>{float(t['shares']):.0f}</td><td class='num'>{float(t['price']):.3f}</td>"
            f"<td class='num'>{money(float(t['amount']))}</td><td class='num'>{fees:.2f}</td>"
            f"<td class='small muted'>{esc(REASON_CN.get(t.get('reason_type', ''), t.get('reason_type', '')))}</td></tr>")
    return "".join(rows) or "<tr><td colspan=8 class='muted'>尚无成交</td></tr>"


def load_challengers():
    out = []
    for name, label, data, note in CHALLENGERS:
        if not (ROOT / "accounts" / name / "state.json").exists():
            continue
        a = load_account(name)
        a.update(label=label, note=note)
        periods = read_csv(ROOT / data / "periods.csv")
        def cum(col):
            v = 1.0
            for r in periods:
                v *= 1 + float(r.get(col) or 0)
            return v - 1
        a["n_periods"] = len(periods)
        a["bench"] = ({"规则理想": cum("f4_ideal"), "K3 纯小市值": cum("k3"), "等权": cum("ew")}
                      if periods and "k3" in periods[0] else {})
        sig = sorted((ROOT / data / "orders").glob("*.json")) if (ROOT / data / "orders").exists() else []
        a["last_signal"] = sig[-1].stem if sig else ""
        out.append(a)
    return out


def load_orders():
    """最近一个真正含冻结订单的目录（忽略只有 main.draft.json 的草稿目录）。"""
    orders_dir = ROOT / "orders"
    if not orders_dir.exists():
        return None, {}
    dirs = sorted((d for d in orders_dir.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
    for d in dirs:
        out = {}
        for name, _ in ACCOUNTS:
            f = d / f"{name}.json"
            if f.exists():
                out[name] = json.loads(f.read_text(encoding="utf-8"))
        if out:
            return d.name, out
    return None, {}


def load_draft():
    """最近一份尚未冻结的 main.draft.json（下一交易日计划草稿）。"""
    orders_dir = ROOT / "orders"
    if not orders_dir.exists():
        return None, None
    dirs = sorted((d for d in orders_dir.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
    for d in dirs:
        f = d / "main.draft.json"
        if not f.exists() or (d / "main.json").exists():
            continue
        try:
            return d.name, json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return d.name, None
    return None, None


def load_runlog():
    p = ROOT / "reports" / "sim" / "runlog.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def load_worklog():
    p = ROOT / "docs" / "进展与交接.md"
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("| 20"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) >= 3:
                    rows.append(cells[:3])
    return rows[-5:][::-1]


def load_config():
    try:
        import yaml
        risk = yaml.safe_load((ROOT / "config" / "risk.yaml").read_text(encoding="utf-8"))
        broker = yaml.safe_load((ROOT / "config" / "broker.yaml").read_text(encoding="utf-8"))
        strategy = yaml.safe_load((ROOT / "config" / "strategy.yaml").read_text(encoding="utf-8"))
        return risk, broker, strategy
    except Exception:
        return {}, {}, {}


def order_rows(orders_json, with_account=False) -> str:
    rows = []
    for o in orders_json.get("orders", []):
        st = "tag-approved" if o.get("status") == "approved" else "tag-rejected"
        stx = "批准" if o.get("status") == "approved" else "拒绝"
        rt = o.get("reason_type", "")
        ty = REASON_TAG.get(rt, "tag-skipped")
        notes = o.get("risk_notes") or []
        basis = esc(o.get("reason", ""))
        if notes and o.get("status") != "approved":
            basis += f"<br><span class='muted small'>风控：{esc('; '.join(notes))}</span>"
        acct = f"<td>{esc(orders_json.get('account', ''))}</td>" if with_account else ""
        rows.append(f"<tr>{acct}<td>{esc(o.get('name'))} <span class='muted small num'>{esc(o.get('code'))}</span></td>"
                    f"<td class='num'>{o.get('target_weight', 0) * 100:.0f}%</td>"
                    f"<td><span class='tag {ty}'>{esc(REASON_CN.get(rt, rt))}</span></td>"
                    f"<td><span class='tag {st}'>{stx}</span></td><td class='small'>{basis}</td></tr>")
    return "".join(rows) or f"<tr><td colspan={6 if with_account else 5} class='muted'>无订单</td></tr>"


def draft_rows(draft_json) -> str:
    rows = []
    for o in draft_json.get("orders", []):
        rt = o.get("reason_type", "entry")
        ty = REASON_TAG.get(rt, "tag-skipped")
        rows.append(f"<tr><td>{esc(o.get('name'))} <span class='muted small num'>{esc(o.get('code'))}</span></td>"
                    f"<td class='num'>{o.get('target_weight', 0) * 100:.0f}%</td>"
                    f"<td><span class='tag {ty}'>{esc(REASON_CN.get(rt, rt))}</span></td>"
                    f"<td class='small'>{esc(o.get('reason', ''))}</td></tr>")
    return "".join(rows) or "<tr><td colspan=4 class='muted'>空草稿：没有个股订单，evening 只会做底仓停泊</td></tr>"


def load_screen():
    d = ROOT / "data" / "screen"
    if not d.exists():
        return None, []
    files = sorted(f.name for f in d.iterdir() if f.suffix == ".csv")
    if not files:
        return None, []
    date = files[-1][:-4]
    return date, read_csv(d / files[-1])


def load_pool():
    d = ROOT / "data" / "pool"
    if not d.exists():
        return None, []
    files = sorted(f.name for f in d.glob("*.csv"))
    if not files:
        return None, []
    return files[-1][:-4], read_csv(d / files[-1])


def load_watchlist():
    p = ROOT / "data" / "watchlist.json"
    if not p.exists():
        return []
    try:
        items = json.loads(p.read_text(encoding="utf-8")).get("items", [])
        return items if isinstance(items, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def main() -> int:
    accts = {name: load_account(name) for name, _ in ACCOUNTS}
    m = accts["main"]
    regime = m["nav"][-1]["regime"] if m["nav"] else "unknown"
    risk_state = m["nav"][-1]["risk_state"] if m["nav"] else "unknown"
    now = datetime.datetime.now().astimezone()
    weekday = now.weekday()
    market = "休市（周末）" if weekday >= 5 else "交易日"
    order_date, orders = load_orders()
    draft_date, draft = load_draft()
    pool_date, pool_rows = load_pool()
    watch_items = load_watchlist()
    runlog = load_runlog()
    worklog = load_worklog()
    risk, broker, strategy = load_config()
    qcfg = (strategy or {}).get("quant", {})
    rcfg = (strategy or {}).get("random", {})
    band = (risk.get("bands") or {}).get(regime, {})

    # ---- 各策略收益对照 ----
    cmp_rows = []
    for name, label in ACCOUNTS:
        a = accts[name]
        gap = a["pnl_pct"] - m["pnl_pct"]
        cmp_rows.append(
            f"<tr><td><b>{esc(label)}</b></td><td class='num'>{money(a['equity'])}</td>"
            f"<td class='num {cls(a['pnl_pct'])}'>{pct(a['pnl_pct'])}</td>"
            f"<td class='num {cls(a['day_pnl'])}'>{money(a['day_pnl'])}</td>"
            f"<td class='num'>{pct(gap) if name != 'main' else '—'}</td>"
            f"<td class='num'>{pct(a['drawdown'], signed=False)}</td>"
            f"<td class='small muted'>{CONTROL_NOTE[name]}</td></tr>")

    # ---- 复盘（main 每天 19:00 生成） ----
    journal_html = ""
    journal_dir = ROOT / "journal" / "daily"
    if journal_dir.exists():
        for jf in sorted(journal_dir.glob("*.md"), reverse=True)[:2]:
            try:
                txt = jf.read_text(encoding="utf-8")
            except OSError:
                continue
            journal_html += (f"<details style='margin-top:10px'><summary>{esc(jf.stem)} 复盘</summary>"
                             f"<pre class='journal'>{esc(txt)}</pre></details>")
    journal_html = journal_html or "<p class='small muted'>暂无复盘记录（每天 19:00 生成）。</p>"

    strategy_notes = {
        "main": "agent 主策略：每天 19:00 复盘 + sim pool 全市场多路召回 → 轻量点评 → 深度研究 → 订单草稿；交易日 20:00 风控冻结。无草稿时只做底仓停泊与强制止损。",
        "quant": f"量化初筛：收盘 > 20 日线 > 60 日线；得分 = 20 日收益 ÷ 20 日波动，买前 {qcfg.get('n_stocks', 4)} 名；跌出前 {qcfg.get('keep_rank', 12)} 才卖。",
        "random": f"随机对照：从初筛候选池随机抽取，持有满 {rcfg.get('rotate_days', 20)} 个交易日轮换。",
        "etf_band": "仓位对照：不买个股，ETF 仓位跟当日仓位区间上沿（防守 50% / 中性 80% / 进攻 95%）。与 etf（固定 98%）配对后，main − etf_band 是选股净贡献，etf_band − etf 是仓位控制贡献。",
        "etf": "底仓对照：只持有沪深300ETF（510300），不选股、不择时。",
    }

    def summary_cards(a) -> str:
        return f"""<div class="grid grid-4">
      <div class="card hero"><div class="label">净值</div><div class="value num">{money(a['equity'])}</div>
        <div class="foot">现金 {money(a['cash'])} · 结算至 {esc(a['last_settled'] or '未结算')}</div></div>
      <div class="card hero"><div class="label">累计盈亏</div><div class="value num {cls(a['pnl'])}">{money(a['pnl'])}</div>
        <div class="foot {cls(a['pnl_pct'])}">{pct(a['pnl_pct'])}（初始 {money(a['state']['initial_cash'])}）</div></div>
      <div class="card hero"><div class="label">当日盈亏</div><div class="value num {cls(a['day_pnl'])}">{money(a['day_pnl'])}</div>
        <div class="foot">相对上一结算日</div></div>
      <div class="card hero"><div class="label">回撤 / 仓位</div><div class="value num">{pct(a['drawdown'], signed=False)}</div>
        <div class="foot">个股 {pct(a['stock_w'], signed=False)} · 主动ETF {pct(a['active_w'], signed=False)} · 底仓ETF {pct(max(a['etf_w'] - a['active_w'], 0), signed=False)} · 现金</div></div>
    </div>"""

    def plan_block(name) -> str:
        frozen = orders.get(name)
        parts = []
        if frozen:
            parts.append(f"""<div class="table-card"><table>
        <thead><tr><th>标的</th><th>目标仓位</th><th>类型</th><th>状态</th><th>依据 / 风控备注</th></tr></thead>
        <tbody>{order_rows(frozen)}</tbody></table></div>
        <p class="small muted" style="margin-top:8px">{esc(order_date)} 冻结订单（SHA256 校验，交易日 14:45 价格）。</p>""")
        else:
            parts.append("<div class='card'><p class='muted'>暂无冻结订单（等待下一次 evening 冻结）。</p></div>")
        if name == "main" and draft_date and draft:
            parts.append(f"""<div class="card" style="margin-top:14px;border-color:rgba(232,179,75,.35)">
        <div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:8px">
          <b>{esc(draft_date)} 订单草稿</b><span class="tag tag-pending">待冻结</span>
          <span class="small muted">来源：每晚 19:00 研究任务</span></div>
        <table><thead><tr><th>标的</th><th>目标仓位</th><th>类型</th><th>理由 / 置信度</th></tr></thead>
        <tbody>{draft_rows(draft)}</tbody></table></div>
        <p class="small muted" style="margin-top:8px">草稿不是订单：20:00 evening 逐条风控校验，被拒或缩量以冻结订单为准。</p>""")
        return "".join(parts)

    def pool_block() -> str:
        if not pool_date or not pool_rows:
            return "<p class='small muted'>暂无候选池（下次 19:00 研究任务生成）。</p>"
        has_triage = any("action" in r for r in pool_rows)
        if has_triage:
            ready = sorted([r for r in pool_rows if str(r.get("action")) == "ready"],
                           key=lambda r: -float(r.get("priority") or 0))[:8]
            rows = ""
            for r in ready:
                ind = r.get("industry") if r.get("industry") and str(r.get("industry")) != "nan" else "—"
                style = {"lowvol": "低波", "pullback": "回调", "breakout": "突破", "trend": "趋势",
                         "sector": "行业", "momentum": "动量", "holding": "持仓", "watchlist": "观察"}.get(
                             str(r.get("style")), str(r.get("style") or "—"))
                rows += (f"<tr><td>{esc(r['name'])} <span class='muted small num'>{esc(r['code'])}</span></td>"
                         f"<td>{style}</td><td>{esc(ind)}</td>"
                         f"<td class='num'>{float(r['ret_20']) * 100:+.1f}%</td>"
                         f"<td class='small'>{esc(str(r.get('trigger') or ''))}</td>"
                         f"<td class='num'>{float(r.get('stop_prob') or 0) * 100:.0f}%</td></tr>")
            rows = rows or "<tr><td colspan=6 class='muted'>今日没有 action=ready 的标的（等触发/防守储备见下）</td></tr>"
            n_ready = sum(1 for r in pool_rows if str(r.get("action")) == "ready")
            title = "今日可行动（v1.1 分诊）"
            headers = "<tr><th>标的</th><th>风格</th><th>行业</th><th>20日</th><th>触发位</th><th>触损概率</th></tr>"
            note = (f"<p class='small muted'>data/pool/{esc(pool_date)}.csv · 召回 {len(pool_rows)} 只"
                    f"（可行动 {n_ready} 只），按 regime 配额 + 可行动性排序；不再按多通道共振优先。</p>")
        else:
            multi = sorted([r for r in pool_rows if int(float(r.get("n_channels") or 0)) >= 2],
                           key=lambda r: (-int(float(r["n_channels"])), -float(r["ret_20"])))[:8]
            rows = ""
            for r in multi:
                ind = r.get("industry") if r.get("industry") and str(r.get("industry")) != "nan" else "—"
                rows += (f"<tr><td>{esc(r['name'])} <span class='muted small num'>{esc(r['code'])}</span></td>"
                         f"<td>{esc(ind)}</td><td class='small'>{esc(str(r['channels']).replace('|', ' / '))}</td>"
                         f"<td class='num'>{float(r['ret_20']) * 100:+.1f}%</td>"
                         f"<td class='num'>{float(r['ret_120']) * 100:+.1f}%</td>"
                         f"<td class='num'>{float(r['avg_amount']) / 1e8:.1f}</td></tr>")
            rows = rows or "<tr><td colspan=6 class='muted'>本日没有多通道共振标的</td></tr>"
            n_multi = sum(1 for r in pool_rows if int(float(r.get("n_channels") or 0)) >= 2)
            title = "多通道共振标的（旧格式）"
            headers = "<tr><th>标的</th><th>行业</th><th>通道</th><th>20日</th><th>120日</th><th>日均成交亿</th></tr>"
            note = (f"<p class='small muted'>data/pool/{esc(pool_date)}.csv · 召回 {len(pool_rows)} 只"
                    f"（多通道 {n_multi} 只）；这是旧格式，下次 19:00 会更新为 v1.1 分诊。</p>")
        active = [i for i in watch_items if i.get("status", "watching") in ("watching", "researched")]
        if active:
            items = "".join(
                f"<tr><td>{esc(i.get('name', ''))} <span class='muted small num'>{esc(i.get('code', ''))}</span></td>"
                f"<td>{esc(WATCH_CN.get(i.get('status'), i.get('status', '')))}</td>"
                f"<td class='small'>{esc(i.get('trigger', ''))}</td></tr>" for i in active[:12])
            watch_html = (f"<details style='margin-top:10px'><summary>观察名单（{len(active)} 只）</summary>"
                          f"<div class='table-card' style='margin-top:8px'><table>"
                          f"<thead><tr><th>标的</th><th>状态</th><th>触发条件</th></tr></thead>"
                          f"<tbody>{items}</tbody></table></div></details>")
        else:
            watch_html = "<p class='small muted'>观察名单为空。</p>"
        return (note + f"<h3 class='subhead'>{title}</h3>"
                f"<div class='table-card' style='margin-top:8px'><table><thead>{headers}</thead>"
                f"<tbody>{rows}</tbody></table></div>{watch_html}")

    tab_buttons, tab_panels = [], []
    for name, label in ACCOUNTS:
        a = accts[name]
        active = " active" if name == "main" else ""
        vals = [float(r["equity"]) for r in a["nav"]]
        spark = sparkline(vals, a["state"]["initial_cash"], f"{label} 净值曲线")
        review = journal_html if name == "main" else "<p class='small muted'>对照组不单独写复盘；每日复盘见 main 标签页。</p>"
        tab_buttons.append(f"<button class='tab{active}' data-tab='{name}'>{esc(label)}</button>")
        tab_panels.append(f"""
    <div class="tab-panel{active}" id="tab-{name}">
      <p class="small muted" style="margin-bottom:12px">{esc(strategy_notes[name])}</p>
      {summary_cards(a)}
      <div class="card" style="margin-top:14px">{spark}</div>
      <div class="grid grid-2" style="margin-top:14px">
        <div class="table-card"><table><thead><tr><th>持仓</th><th>股数</th><th>成本价</th><th>现价</th><th>市值</th><th>盈亏</th><th>止损价</th><th>建仓日</th></tr></thead>
        <tbody>{positions_table(a)}</tbody></table></div>
        <div class="table-card"><table><thead><tr><th>日期</th><th>方向</th><th>标的</th><th>股数</th><th>价格</th><th>金额</th><th>费用</th><th>类型</th></tr></thead>
        <tbody>{trades_table(a, 8)}</tbody></table></div>
      </div>
      <p class="small muted" style="margin-top:8px">左：当前持仓；右：最近成交（含佣金/印花税/过户费）。</p>
      <h3 class="subhead">交易计划</h3>
      {plan_block(name)}
      {('<h3 class="subhead">候选池</h3>' + pool_block()) if name == "main" else ''}
      <h3 class="subhead">复盘</h3>
      {review}
    </div>""")
    # ---- 挑战者 ----
    challengers = load_challengers()
    ch_rows, ch_details = [], []
    for a in challengers:
        bench = (" · ".join(f"{k} {pct(v)}" for k, v in a["bench"].items())
                 or ("满一期后显示" if a["name"] != "challenger/pead" else "逐笔统计见 data/pead/report.md"))
        ch_rows.append(
            f"<tr><td><b>{esc(a['label'])}</b></td><td class='num'>{money(a['equity'])}</td>"
            f"<td class='num {cls(a['pnl_pct'])}'>{pct(a['pnl_pct'])}</td>"
            f"<td class='num {cls(a['day_pnl'])}'>{money(a['day_pnl'])}</td>"
            f"<td class='num'>{pct(a['drawdown'], signed=False)}</td>"
            f"<td class='num'>{len(a['positions'])}</td><td class='num'>{esc(a['last_settled'] or '未结算')}</td>"
            f"<td class='small'>{esc(bench)}</td></tr>")
        vals = [float(r["equity"]) for r in a["nav"]]
        sig = f" · 最近订单 {esc(a['last_signal'])}" if a["last_signal"] else ""
        ch_details.append(f"""<details style="margin-top:10px"><summary>{esc(a['label'])}（持仓 {len(a['positions'])} 只 · 现金 {money(a['cash'])}{sig}）</summary>
      <p class="small muted" style="margin:8px 0">{esc(a['note'])}</p>
      <div class="card">{sparkline(vals, a['state']['initial_cash'], a['label'] + ' 净值曲线')}</div>
      <div class="grid grid-2" style="margin-top:10px">
        <div class="table-card"><table><thead><tr><th>持仓</th><th>股数</th><th>成本价</th><th>现价</th><th>市值</th><th>盈亏</th><th>止损价</th><th>建仓日</th></tr></thead>
        <tbody>{positions_table(a)}</tbody></table></div>
        <div class="table-card"><table><thead><tr><th>日期</th><th>方向</th><th>标的</th><th>股数</th><th>价格</th><th>金额</th><th>费用</th><th>类型</th></tr></thead>
        <tbody>{trades_table(a, 10)}</tbody></table></div>
      </div></details>""")
    challenger_block = (f"""<div class="table-card"><table>
      <thead><tr><th>账户</th><th>净值</th><th>累计盈亏</th><th>当日盈亏</th><th>回撤</th><th>持仓</th><th>结算至</th><th>同期对照（累计）</th></tr></thead>
      <tbody>{''.join(ch_rows)}</tbody></table></div>{''.join(ch_details)}""" if challengers
                        else "<p class='muted'>暂无挑战者账户。</p>")

    # ---- 执行日志 ----
    log_rows = []
    for e in runlog[::-1][:30]:
        ts = e.get("ts", "")[:16].replace("T", " ")
        st = e.get("status", "")
        tag = {"ok": "tag-ok", "skipped": "tag-skipped", "failed": "tag-failed", "alert": "tag-alert"}.get(st, "")
        lines = "<br>".join(esc(x) for x in e.get("lines", [])) or "<span class='muted'>—</span>"
        log_rows.append(f"<tr><td class='num'>{esc(ts)}</td><td>{esc(e.get('task'))}</td>"
                        f"<td><span class='tag {tag}'>{esc(st)}</span></td><td>{lines}</td></tr>")
    log_table = "".join(log_rows) or (
        "<tr><td colspan=4 class='muted'>暂无执行记录：日志自 2026-10-25 启用，之后每次定时任务运行（含非交易日跳过）"
        "都会自动追加一行。</td></tr>")

    # ---- 初筛快照 ----
    screen_date, screen_rows = load_screen()
    picked = {}
    for name, _ in ACCOUNTS:
        for o in (orders.get(name) or {}).get("orders", []):
            if o.get("reason_type") == "entry":
                picked.setdefault(o.get("code"), []).append(f"{name}{'✓' if o.get('status') == 'approved' else '✗'}")
    for o in (draft or {}).get("orders", []):
        if o.get("reason_type") == "entry":
            picked.setdefault(o.get("code"), []).append("草稿")
    top = sorted([r for r in screen_rows if r.get("rank")], key=lambda r: int(r["rank"]))[:10]
    screen_table = ""
    for r in top:
        tags = "".join(f"<span class='tag tag-entry' style='margin-right:4px'>{esc(x)}</span>" for x in picked.get(r["code"], []))
        screen_table += (f"<tr><td class='num'>{esc(r['rank'])}</td>"
                         f"<td>{esc(r['name'])} <span class='muted small num'>{esc(r['code'])}</span></td>"
                         f"<td class='num'>{float(r['close']):.2f}</td>"
                         f"<td class='num'>{float(r['ret_20']) * 100:+.1f}%</td>"
                         f"<td class='num'>{float(r['vol_20']) * 100:.1f}%</td>"
                         f"<td class='num'>{float(r['score']):.2f}</td>"
                         f"<td>{tags or '<span class=muted>—</span>'}</td></tr>")
    screen_block = (f"<div class='table-card' style='margin-top:14px'><table>"
                    f"<thead><tr><th>排名</th><th>标的</th><th>收盘</th><th>20日收益</th><th>20日波动</th><th>动量得分</th><th>被选</th></tr></thead>"
                    f"<tbody>{screen_table}</tbody></table></div>"
                    f"<p class='small muted' style='margin-top:8px'>初筛快照 {esc(screen_date)}（data/screen/）：得分 = 20 日收益 ÷ 20 日波动率；"
                    f"被选列中 ✓ 表示批准买入、✗ 表示被风控拒绝。</p>") if top else ""

    # ---- 风控参数 ----
    fees = (broker or {}).get("fees", {})
    pos_cfg = (risk or {}).get("position", {})
    dd = (risk or {}).get("drawdown", {})
    kv_risk = f"""<dl class="kv">
      <dt>当前状态仓位区间（{esc(regime)}）</dt><dd class="num">{band.get('min', 0) * 100:.0f}% – {band.get('max', 0) * 100:.0f}%</dd>
      <dt>个股仓位上限</dt><dd class="num">{pos_cfg.get('max_single', 0) * 100:.0f}%</dd>
      <dt>最多同时持股</dt><dd class="num">{pos_cfg.get('max_stocks', 0)} 只</dd>
      <dt>每周新开仓上限</dt><dd class="num">{pos_cfg.get('max_new_per_week', 0)} 只</dd>
      <dt>默认止损 / 时间止损</dt><dd class="num">-{pos_cfg.get('stop_loss_pct', 0) * 100:.0f}% / {pos_cfg.get('time_stop_days', 0)} 交易日</dd>
      <dt>回撤警戒 / 熔断 / 停机</dt><dd class="num">{dd.get('warn', 0) * 100:.0f}% / {dd.get('breaker', 0) * 100:.0f}% / {dd.get('halt', 0) * 100:.0f}%</dd>
      <dt>佣金 / 印花税 / 过户费</dt><dd class="num">万{fees.get('commission_rate', 0) * 10000:.1f} / {fees.get('stamp_tax_sell', 0) * 100:.2f}%卖 / {fees.get('transfer_fee', 0) * 100:.3f}%</dd>
    </dl>"""

    worklog_rows = "".join(
        f"<tr><td class='num'>{esc(r[0])}</td><td>{esc(r[1])}</td><td>{esc(r[2])}</td></tr>" for r in worklog)

    body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A 股模拟交易 · 项目状态看板</title>
<meta name="description" content="A 股模拟交易系统状态看板：四策略收益对照，按标签查看各策略持仓、成交、交易计划与复盘，以及定时任务执行日志">
<style>{CSS}</style>
</head>
<body>
<header>
  <div class="wrap">
    <div class="head-row">
      <div>
        <h1>A 股模拟交易 · 项目状态看板</h1>
        <div class="sub">四策略收益对照 · 点击标签查看各自的持仓、成交、交易计划与复盘</div>
      </div>
      <div class="badges">
        <span class="badge"><span class="dot dot-{esc(regime)}"></span>市场：{esc(regime)}</span>
        <span class="badge"><span class="dot dot-normal"></span>风控：{esc(risk_state)}</span>
        <span class="badge">{esc(market)}</span>
      </div>
    </div>
    <div class="sub" style="margin-top:10px">账本结算至 <b class="num">{esc(m['last_settled'])}</b> ·
      本页由定时任务在每次运行后自动重建并发布 · 生成于 <span class="num">{now:%Y-%m-%d %H:%M}</span></div>
  </div>
</header>
<div class="wrap">

  <section>
    <h2>各策略收益对照</h2>
    <div class="table-card"><table>
      <thead><tr><th>账户</th><th>净值</th><th>累计盈亏</th><th>当日盈亏</th><th>与 main 差</th><th>回撤</th><th>角色</th></tr></thead>
      <tbody>{''.join(cmp_rows)}</tbody></table></div>
    <p class="small muted" style="margin-top:8px">净值曲线、持仓、成交、交易计划与复盘，在下方按策略切换查看。</p>
  </section>

  <section>
    <h2>策略详情</h2>
    <div class="tabs" role="tablist">{''.join(tab_buttons)}</div>
    {''.join(tab_panels)}
  </section>

  <section>
    <h2>挑战者账户（独立实验）</h2>
    {challenger_block}
    <p class="small muted" style="margin-top:8px">挑战者是各自 10 万的独立纸面账户（accounts/challenger/），不进主流程的风控、结算与上方收益对照；由各自工具结算（tools/s002.py、tools/pead.py）。同期对照来自 data/&lt;账户&gt;/periods.csv，按已完成的换仓期复利累计。</p>
  </section>

  <section>
    <h2>量化初筛（quant / random 对照组）</h2>
    {screen_block or "<p class='muted'>暂无初筛数据。</p>"}
    <p class="small muted" style="margin-top:8px">main 的候选池见上方 main 标签页（sim pool 全市场多路召回）；这里只展示 quant / random 对照组使用的初筛快照。所有订单冻结前都要过风控代码一票否决（仓位区间、单票上限、每周新开仓上限等，见 config/risk.yaml）。</p>
  </section>

  <section>
    <h2>定时任务执行日志</h2>
    <div class="table-card"><table>
      <thead><tr><th>时间</th><th>任务</th><th>状态</th><th>汇报</th></tr></thead>
      <tbody>{log_table}</tbody></table></div>
    <p class="small muted" style="margin-top:8px">模拟盘 pipeline：execute（交易日 15:10）/ research（每天 19:00，复盘+草稿）/ evening（交易日 20:00）；非交易日 execute/evening 记为 skipped。黄金提醒为独立功能，仅触发或失败时记录，与模拟盘无关。</p>
  </section>

  <section>
    <h2>更多</h2>
    <details>
      <summary>风控与成本参数（config/risk.yaml · broker.yaml）</summary>
      {kv_risk}
    </details>
    <details>
      <summary>项目阶段</summary>
      <div class="small" style="margin-top:8px">
        <p>1 确定性内核 — 已完成（日历/撮合/账本/风控/初筛/对照/日报/看板，16 项测试）。</p>
        <p>正式模拟运行期 — 进行中（2026-10-23 起跑，四账户各 10 万）。</p>
        <p>2 Agent 订单草稿 — 已接入（2026-10-25；每晚 19:00 复盘+草稿 → 20:00 风控冻结）。</p>
        <p>3 定时任务深化 — 进行中（execute / research / evening 由 Qoder cron 承担，看板自动同步）。</p>
        <p>4 策略进化 — 未开始。</p>
      </div>
    </details>
    <details>
      <summary>近期工作日志（docs/进展与交接.md）</summary>
      <div class="table-card" style="margin-top:8px"><table>
        <thead><tr><th>日期</th><th>工具</th><th>内容</th></tr></thead><tbody>{worklog_rows}</tbody></table></div>
    </details>
  </section>

  <footer>
    <p>本页为静态快照，由定时任务（execute / research / evening）在每次成功运行后自动重建并发布；非交易日仅研究任务更新。最新账本以本机 reports/sim/dashboard.html 为准。</p>
    <p style="margin-top:6px">本看板由 AI 生成，仅供研究参考，不构成投资建议。股市有风险，投资需谨慎。</p>
  </footer>
</div>
<script>
  document.querySelectorAll('.tab').forEach(function (btn) {{
    btn.addEventListener('click', function () {{
      document.querySelectorAll('.tab').forEach(function (b) {{ b.classList.toggle('active', b === btn); }});
      document.querySelectorAll('.tab-panel').forEach(function (p) {{
        p.classList.toggle('active', p.id === 'tab-' + btn.dataset.tab);
      }});
    }});
  }});
</script>
</body>
</html>
"""
    (ROOT / "web").mkdir(exist_ok=True)
    (ROOT / "dist").mkdir(exist_ok=True)
    (ROOT / "web" / "index.html").write_text(body, encoding="utf-8")
    (ROOT / "dist" / "index.html").write_text(body, encoding="utf-8")
    print(f"dashboard: 已生成 web/index.html 与 dist/index.html（结算至 {m['last_settled']}，runlog {len(runlog)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
