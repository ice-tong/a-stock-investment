"""资金看板：单个 HTML 文件（数据内嵌、无外部依赖），每晚随报告一起生成到 reports/sim/dashboard.html。

包含：资产概览、各阶段收益（今日/本周/本月/本季/本年/近 N 日/成立以来）、净值曲线、回撤、
每日盈亏、月度收益表、资产构成、持仓明细、交易统计与最近成交。可切换账户、切换时间范围。
"""
import datetime as dt
import json

from . import config
from .ledger import all_accounts
from .report import LABELS, metrics

ORDER = ["main", "quant", "random", "etf"]
PERIODS = [("today", "今日"), ("week", "本周"), ("month", "本月"), ("quarter", "本季"), ("year", "本年"),
           ("d20", "近1月"), ("d60", "近3月"), ("d120", "近6月"), ("d250", "近1年"), ("all", "成立以来")]


def _base_index(dates: list, key: str) -> int | None:
    """返回作为期初的下标（-1 表示用初始资金/成立日作期初）；历史不够时返回 None。"""
    last = dt.date.fromisoformat(dates[-1])
    if key == "today":
        return len(dates) - 2 if len(dates) >= 2 else -1
    if key.startswith("d"):
        n = int(key[1:])
        return len(dates) - 1 - n if len(dates) - 1 - n >= -1 else None
    if key == "all":
        return -1
    start = {"week": last - dt.timedelta(days=last.weekday()),
             "month": last.replace(day=1),
             "quarter": last.replace(month=(last.month - 1) // 3 * 3 + 1, day=1),
             "year": last.replace(month=1, day=1)}[key].isoformat()
    idx = -1
    for i, d in enumerate(dates):
        if d < start:
            idx = i
    return idx


def period_returns(dates: list, values: list, base_value: float) -> dict:
    out = {}
    for key, _ in PERIODS:
        i = _base_index(dates, key) if dates else None
        if i is None:
            out[key] = None
        else:
            base = base_value if i < 0 else values[i]
            out[key] = values[-1] / base - 1 if base else None
    return out


def monthly_returns(dates: list, values: list, base_value: float) -> dict:
    """{年份: {月份: 收益, 'year': 全年收益}}，按每月最后一个交易日计算。"""
    month_end = {}
    for d, v in zip(dates, values):
        month_end[d[:7]] = v
    out, prev, year_start = {}, base_value, {}
    for ym in sorted(month_end):
        y, m = ym[:4], int(ym[5:])
        year_start.setdefault(y, prev)
        out.setdefault(y, {})[m] = month_end[ym] / prev - 1
        prev = month_end[ym]
        out[y]["year"] = prev / year_start[y] - 1
    return out


def build(date: str, market, bcfg: dict) -> str:
    accts = sorted(all_accounts(), key=lambda a: ORDER.index(a.name) if a.name in ORDER else 99)
    data = {"date": date, "accounts": [], "periods": PERIODS}
    all_dates = set()
    for acct in accts:
        nav = acct.nav()
        dates = [r["date"] for r in nav]
        eq = [float(r["equity"]) for r in nav]
        init = acct.state["initial_cash"]
        all_dates |= set(dates)
        m = metrics(acct)
        trades = acct.trades()
        positions = []
        for code, p in acct.state["positions"].items():
            value = p["shares"] * p["last_price"]
            positions.append({"code": code, "name": p["name"], "kind": p["kind"], "shares": p["shares"],
                              "cost": p["cost"], "value": value, "pnl": value - p["cost"],
                              "first": p["first_buy_date"], "stop": p.get("stop_price")})
        data["accounts"].append({
            "key": acct.name, "label": LABELS.get(acct.name, acct.name), "strategy": acct.state["strategy"],
            "init": init, "start": acct.state["start_date"], "cash": acct.state["cash"],
            "nav": [{"d": r["date"], "eq": float(r["equity"]), "cash": float(r["cash"]),
                     "stock": float(r["stock_value"]), "etf": float(r["etf_value"]), "dd": float(r["drawdown"]),
                     "regime": r["regime"], "risk": r["risk_state"]} for r in nav],
            "periods": period_returns(dates, eq, init),
            "monthly": monthly_returns(dates, eq, init),
            "metrics": {k: m.get(k) for k in ("total", "ann", "mdd", "calmar", "sharpe", "sortino", "vol",
                                              "win_rate", "payoff", "sells", "costs", "turnover")},
            "positions": sorted(positions, key=lambda x: -x["value"]),
            "trades": [{k: t[k] for k in ("date", "side", "name", "code", "shares", "price", "amount",
                                          "realized_pnl", "reason_type")}
                       for t in trades if t["side"] in ("buy", "sell")][-30:][::-1],
            "risk": {"breaker_until": acct.state["breaker_until"], "halted": acct.state["halted"],
                     "pending": acct.state["pending_exits"]},
        })

    # 基准指数：与账户同日期对齐，以首个日期收盘为期初
    bench = {"label": "沪深300", "nav": []}
    df = market.daily(bcfg["benchmark"])
    if df is not None and all_dates:
        ds = sorted(d for d in all_dates if d in df.index)
        if ds:
            closes = [float(df.loc[d, "close"]) for d in ds]
            bench["nav"] = [{"d": d, "eq": c} for d, c in zip(ds, closes)]
            bench["periods"] = period_returns(ds, closes, closes[0])
            bench["monthly"] = monthly_returns(ds, closes, closes[0])
    data["benchmark"] = bench

    out = config.reports_dir()
    out.mkdir(parents=True, exist_ok=True)
    page = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    path = out / "dashboard.html"
    path.write_text(page, encoding="utf-8")
    return str(path)


TEMPLATE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>资金看板</title>
<style>
:root{color-scheme:light;
  --bg:#f5f5f3;--surface:#fcfcfb;--line:#e4e3df;--grid:#ecebe7;
  --text:#0b0b0b;--text2:#52514e;--muted:#7a7974;
  --up:#d6403f;--down:#0f8a3c;--flat:#7a7974;
  --s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--bench:#8a8984;
  --accent:#2a78d6;--hm-up:214,64,63;--hm-down:15,138,60}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;
  --bg:#121211;--surface:#1a1a19;--line:#2e2d2b;--grid:#252523;
  --text:#f0efec;--text2:#c3c2b7;--muted:#8f8e86;
  --up:#ef6b6a;--down:#3fb26a;--flat:#8f8e86;
  --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--bench:#9a9990;
  --accent:#3987e5;--hm-up:239,107,106;--hm-down:63,178,106}}
:root[data-theme="dark"]{color-scheme:dark;
  --bg:#121211;--surface:#1a1a19;--line:#2e2d2b;--grid:#252523;
  --text:#f0efec;--text2:#c3c2b7;--muted:#8f8e86;
  --up:#ef6b6a;--down:#3fb26a;--flat:#8f8e86;
  --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--bench:#9a9990;
  --accent:#3987e5;--hm-up:239,107,106;--hm-down:63,178,106}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 -apple-system,"PingFang SC","Helvetica Neue",sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:1180px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:16px}
h1{font-size:22px;margin:0;letter-spacing:.01em}
.sub{color:var(--muted);font-size:12.5px}
.seg{display:inline-flex;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:2px;gap:2px;flex-wrap:wrap}
.seg button{border:0;background:none;color:var(--text2);font:inherit;font-size:13px;padding:5px 12px;border-radius:6px;cursor:pointer}
.seg button[aria-pressed=true]{background:var(--accent);color:#fff}
.grid{display:grid;gap:12px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(160px,1fr));margin-bottom:12px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
.kpi .label{color:var(--muted);font-size:12px}
.kpi .value{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}
.kpi .note{font-size:12px;color:var(--text2);font-variant-numeric:tabular-nums}
.row2{grid-template-columns:2fr 1fr;margin-bottom:12px}
@media (max-width:860px){.row2{grid-template-columns:1fr}}
h2{font-size:15px;margin:0 0 10px;display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
h2 .sub{font-weight:normal}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:13px}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:500;font-size:12px}
tr.sel td{background:color-mix(in srgb,var(--accent) 8%,transparent)}
tr.sel td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--flat)}
.dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;vertical-align:0}
.chartbox{position:relative}
svg.chart{width:100%;height:auto;display:block;overflow:visible}
.axis{fill:var(--muted);font-size:11px}
.gridline{stroke:var(--grid);stroke-width:1}
.zero{stroke:var(--line);stroke-width:1}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--text2);margin-top:6px}
.legend span{display:inline-flex;align-items:center}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;z-index:2;min-width:150px;font-variant-numeric:tabular-nums}
.tip .t{color:var(--muted);margin-bottom:4px}
.tip div.r{display:flex;justify-content:space-between;gap:12px}
.hm td{text-align:center;min-width:52px;border:2px solid var(--surface);border-radius:4px;color:var(--text)}
.hm th{text-align:center}
.alloc{display:flex;height:14px;border-radius:4px;overflow:hidden;gap:2px;margin:6px 0 10px}
.alloc div{height:100%}
.kv{display:grid;grid-template-columns:1fr auto;gap:4px 12px;font-size:13px;font-variant-numeric:tabular-nums}
.kv span:nth-child(odd){color:var(--text2)}
.badge{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line);color:var(--text2)}
.badge.warn{border-color:var(--up);color:var(--up)}
.empty{color:var(--muted);padding:20px 0;text-align:center}
footer{color:var(--muted);font-size:12px;margin-top:20px}
</style></head>
<body><main>
<header>
  <div><h1>资金看板</h1><div class="sub" id="meta"></div></div>
  <div class="seg" id="acctSeg" role="group" aria-label="切换账户"></div>
</header>

<section class="grid kpis" id="kpis"></section>

<section class="card" style="margin-bottom:12px">
  <h2>阶段收益 <span class="sub">高亮行为当前账户；超额 = 当前账户 − 沪深300</span></h2>
  <div class="scroll"><table id="periodTbl"></table></div>
</section>

<section class="grid row2">
  <div class="card">
    <h2>净值曲线 <span class="seg" id="rangeSeg" role="group" aria-label="时间范围"></span></h2>
    <div class="chartbox" id="navChart"></div>
  </div>
  <div class="card">
    <h2>资产构成</h2>
    <div id="alloc"></div>
  </div>
</section>

<section class="grid row2">
  <div class="card"><h2>回撤 <span class="sub">当前账户与沪深300，相对区间内高点</span></h2><div class="chartbox" id="ddChart"></div></div>
  <div class="card"><h2>交易统计</h2><div id="stats"></div></div>
</section>

<section class="card" style="margin-bottom:12px"><h2>每日盈亏 <span class="sub">当前账户，元</span></h2><div class="chartbox" id="pnlChart"></div></section>

<section class="card" style="margin-bottom:12px"><h2>月度收益 <span class="sub">当前账户；括号内为沪深300</span></h2><div class="scroll" id="monthly"></div></section>

<section class="card" style="margin-bottom:12px"><h2>持仓明细</h2><div class="scroll" id="positions"></div></section>

<section class="card"><h2>最近成交 <span class="sub">最多 30 笔</span></h2><div class="scroll" id="trades"></div></section>

<footer>模拟交易，仅供研究，不构成投资建议。数据由 python -m sim 生成，收益已扣除佣金、印花税、过户费和滑点。</footer>
</main>
<script>
const DATA = __DATA__;
const SERIES = ["--s1","--s2","--s3","--s4"];
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const store = {get(k){try{return localStorage.getItem(k)}catch(e){return null}}, set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
const accts = DATA.accounts;
let cur = store.get("dash.acct"); if(!accts.find(a=>a.key===cur)) cur = accts[0]?.key;
let range = store.get("dash.range") || "all";
const RANGES = [["d20","近1月"],["d60","近3月"],["d120","近6月"],["d250","近1年"],["ytd","今年"],["all","全部"]];

const fmtPct = (x,d=2,sign=true) => x==null||isNaN(x) ? "—" : (sign&&x>0?"+":"")+(x*100).toFixed(d)+"%";
const fmtNum = (x,d=0) => x==null||isNaN(x) ? "—" : x.toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtMoney = (x,sign=false) => x==null||isNaN(x) ? "—" : (sign&&x>0?"+":"")+fmtNum(x,2);
const cls = x => x==null||isNaN(x)||Math.abs(x)<1e-12 ? "flat" : x>0 ? "up" : "down";
const colorOf = key => css(SERIES[Math.max(0,accts.findIndex(a=>a.key===key)) % SERIES.length]);
const el = (id) => document.getElementById(id);
const esc = s => String(s??"").replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function account(){ return accts.find(a=>a.key===cur); }

function renderSeg(){
  el("acctSeg").innerHTML = accts.map(a=>`<button aria-pressed="${a.key===cur}" data-k="${a.key}">${esc(a.label)}</button>`).join("");
  el("acctSeg").onclick = e => { const k=e.target.dataset?.k; if(k){cur=k; store.set("dash.acct",k); renderAll();} };
  el("rangeSeg").innerHTML = RANGES.map(([k,l])=>`<button aria-pressed="${k===range}" data-k="${k}">${l}</button>`).join("");
  el("rangeSeg").onclick = e => { const k=e.target.dataset?.k; if(k){range=k; store.set("dash.range",k); renderSeg(); renderCharts();} };
}

function renderMeta(){
  const a = account(), last = a.nav[a.nav.length-1];
  const regime = {offense:"进攻",neutral:"中性",defense:"防守"}[last?.regime] || "—";
  el("meta").textContent = `数据截至 ${DATA.date}　·　${a.label}自 ${a.start} 起运行 ${a.nav.length} 个交易日　·　市场状态：${regime}`;
}

function renderKpis(){
  const a = account(), n = a.nav, last = n[n.length-1], prev = n[n.length-2];
  if(!last){ el("kpis").innerHTML = `<div class="card empty">还没有净值数据</div>`; return; }
  const dayPnl = prev ? last.eq - prev.eq : last.eq - a.init;
  const total = last.eq - a.init;
  const b = DATA.benchmark.periods?.all;
  const pos = last.stock + last.etf;
  const riskTxt = a.risk.halted ? "系统暂停" : (a.risk.breaker_until && a.risk.breaker_until >= DATA.date) ? `熔断至 ${a.risk.breaker_until}` : ({warn:"回撤预警",normal:"正常"}[last.risk]||last.risk);
  const tiles = [
    ["总资产", fmtMoney(last.eq), `初始 ${fmtNum(a.init)}`],
    ["今日盈亏", `<span class="${cls(dayPnl)}">${fmtMoney(dayPnl,true)}</span>`, `<span class="${cls(a.periods.today)}">${fmtPct(a.periods.today)}</span>`],
    ["累计盈亏", `<span class="${cls(total)}">${fmtMoney(total,true)}</span>`, `<span class="${cls(a.periods.all)}">${fmtPct(a.periods.all)}</span>　年化 ${fmtPct(a.metrics.ann,1)}`],
    ["超额收益（vs 沪深300）", `<span class="${cls(a.periods.all-(b??0))}">${b==null?"—":fmtPct(a.periods.all-b)}</span>`, `沪深300 ${fmtPct(b)}`],
    ["最大回撤", fmtPct(a.metrics.mdd,2,false), `当前回撤 ${fmtPct(last.dd,2,false)}　收益回撤比 ${a.metrics.calmar==null?"—":a.metrics.calmar.toFixed(2)}`],
    ["仓位", fmtPct(pos/last.eq,1,false), `现金 ${fmtNum(last.cash)}　风控 ${riskTxt}`],
  ];
  el("kpis").innerHTML = tiles.map(([l,v,s])=>`<div class="card kpi"><div class="label">${l}</div><div class="value">${v}</div><div class="note">${s}</div></div>`).join("");
}

function renderPeriods(){
  const head = `<tr><th>账户</th>${DATA.periods.map(p=>`<th>${p[1]}</th>`).join("")}<th>年化</th><th>最大回撤</th></tr>`;
  const row = (label, color, p, m, sel) => `<tr class="${sel?"sel":""}"><td><span class="dot" style="background:${color}"></span>${esc(label)}</td>${DATA.periods.map(([k])=>`<td class="${cls(p?.[k])}">${fmtPct(p?.[k])}</td>`).join("")}<td>${fmtPct(m?.ann,1)}</td><td>${fmtPct(m?.mdd,2,false)}</td></tr>`;
  let rows = accts.map(a=>row(a.label, colorOf(a.key), a.periods, a.metrics, a.key===cur)).join("");
  const b = DATA.benchmark;
  if(b.periods){
    rows += row("沪深300（基准）", css("--bench"), b.periods, null, false);
    const a = account(), ex = {}; DATA.periods.forEach(([k])=>{ ex[k] = a.periods[k]==null||b.periods[k]==null ? null : a.periods[k]-b.periods[k]; });
    rows += `<tr><td style="color:var(--text2)">超额（${esc(a.label)}）</td>${DATA.periods.map(([k])=>`<td class="${cls(ex[k])}">${fmtPct(ex[k])}</td>`).join("")}<td></td><td></td></tr>`;
  }
  el("periodTbl").innerHTML = head + rows;
}

function sliceRange(nav){
  if(!nav.length) return nav;
  const last = nav[nav.length-1].d;
  if(range==="all") return nav;
  if(range==="ytd"){ const i = nav.findIndex(r=>r.d.slice(0,4)===last.slice(0,4)); return nav.slice(Math.max(0,i-1)); }
  const n = +range.slice(1); return nav.slice(Math.max(0, nav.length-1-n));
}

// ---------- 通用折线/面积图（单一纵轴，十字准线 + 提示框） ----------
function lineChart(box, series, opt){
  const W=760, H=opt.h||260, L=58, R=12, T=10, B=26;
  const dates = [...new Set(series.flatMap(s=>s.pts.map(p=>p.d)))].sort();
  if(dates.length<2){ box.innerHTML = `<div class="empty">数据不足两天，暂无曲线</div>`; return; }
  const xi = Object.fromEntries(dates.map((d,i)=>[d,i]));
  const vals = series.flatMap(s=>s.pts.map(p=>p.v)); if(opt.zero!=null) vals.push(opt.zero);
  let lo=Math.min(...vals), hi=Math.max(...vals); const pad=(hi-lo)*0.08||0.01; lo-=pad; hi+=pad;
  if(opt.top!=null) hi = opt.top;  // 回撤图：上沿固定在 0，不出现正值刻度
  const x = i => L + (W-L-R)*i/(dates.length-1), y = v => T + (H-T-B)*(hi-v)/(hi-lo);
  let g = "";
  for(let i=0;i<=4;i++){ const v=lo+(hi-lo)*i/4; g+=`<line class="gridline" x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${L-8}" y="${y(v)+4}" text-anchor="end">${opt.fmt(v)}</text>`; }
  const ticks = [0, Math.floor((dates.length-1)/3), Math.floor((dates.length-1)*2/3), dates.length-1];
  [...new Set(ticks)].forEach(i=>{ g+=`<text class="axis" x="${x(i)}" y="${H-6}" text-anchor="${i===0?"start":i===dates.length-1?"end":"middle"}">${dates[i].slice(2)}</text>`; });
  if(opt.zero!=null) g+=`<line class="zero" x1="${L}" x2="${W-R}" y1="${y(opt.zero)}" y2="${y(opt.zero)}"/>`;
  series.forEach(s=>{
    const pts = s.pts.map(p=>`${x(xi[p.d]).toFixed(1)},${y(p.v).toFixed(1)}`);
    if(s.area) g+=`<polygon points="${x(xi[s.pts[0].d])},${y(0)} ${pts.join(" ")} ${x(xi[s.pts[s.pts.length-1].d])},${y(0)}" fill="${s.color}" opacity=".14"/>`;
    g+=`<polyline points="${pts.join(" ")}" fill="none" stroke="${s.color}" stroke-width="${s.bold?2.5:2}" stroke-linejoin="round" ${s.dash?'stroke-dasharray="5 4"':""}/>`;
  });
  g+=`<line id="xh" x1="0" x2="0" y1="${T}" y2="${H-B}" stroke="var(--muted)" stroke-width="1" style="display:none"/><g id="dots"></g>`;
  g+=`<rect x="${L}" y="${T}" width="${W-L-R}" height="${H-T-B}" fill="transparent" id="hit"/>`;
  box.innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opt.title)}">${g}</svg><div class="tip"></div>`
    + (series.length>1 ? `<div class="legend">${series.map(s=>`<span><i class="dot" style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : "");
  const svg = box.querySelector("svg"), tip = box.querySelector(".tip"), xh = svg.querySelector("#xh"), dots = svg.querySelector("#dots");
  const maps = series.map(s=>Object.fromEntries(s.pts.map(p=>[p.d,p.v])));
  const move = ev => {
    const r = svg.getBoundingClientRect(), px = (ev.clientX-r.left)*W/r.width;
    const i = Math.max(0, Math.min(dates.length-1, Math.round((px-L)/(W-L-R)*(dates.length-1)))), d = dates[i];
    xh.setAttribute("x1",x(i)); xh.setAttribute("x2",x(i)); xh.style.display="";
    dots.innerHTML = series.map((s,k)=>maps[k][d]==null?"":`<circle cx="${x(i)}" cy="${y(maps[k][d])}" r="4" fill="${s.color}" stroke="var(--surface)" stroke-width="2"/>`).join("");
    tip.innerHTML = `<div class="t">${d}</div>` + series.map((s,k)=>maps[k][d]==null?"":`<div class="r"><span><i class="dot" style="background:${s.color}"></i>${esc(s.name)}</span><b>${opt.tipFmt(maps[k][d])}</b></div>`).join("");
    tip.style.display="block";
    const bx = box.getBoundingClientRect(), tx = ev.clientX-bx.left;
    tip.style.left = (tx > bx.width/2 ? tx - tip.offsetWidth - 12 : tx + 12) + "px";
    tip.style.top = Math.max(0, ev.clientY-bx.top-20) + "px";
  };
  const leave = () => { tip.style.display="none"; xh.style.display="none"; dots.innerHTML=""; };
  svg.addEventListener("pointermove", move); svg.addEventListener("pointerleave", leave);
}

function renderCharts(){
  // 净值：所有账户 + 基准，在所选区间起点统一归一为 1
  const series = accts.map(a=>{ const s=sliceRange(a.nav); const b=s[0]?.eq||1; return {name:a.label, color:colorOf(a.key), bold:a.key===cur, pts:s.map(r=>({d:r.d, v:r.eq/b}))}; });
  const bn = sliceRange(DATA.benchmark.nav);
  if(bn.length){ const b=bn[0].eq; series.push({name:"沪深300", color:css("--bench"), dash:true, pts:bn.map(r=>({d:r.d, v:r.eq/b}))}); }
  lineChart(el("navChart"), series, {title:"净值曲线", fmt:v=>v.toFixed(3), tipFmt:v=>`${v.toFixed(4)}（${fmtPct(v-1)}）`});

  // 回撤：区间内相对高点
  const ddOf = rows => { let pk=-Infinity; return rows.map(r=>{ pk=Math.max(pk,r.eq); return {d:r.d, v:r.eq/pk-1}; }); };
  const a = account(), ddS = [{name:a.label, color:colorOf(a.key), area:true, pts:ddOf(sliceRange(a.nav))}];
  if(bn.length) ddS.push({name:"沪深300", color:css("--bench"), dash:true, pts:ddOf(bn)});
  lineChart(el("ddChart"), ddS, {title:"回撤", h:200, zero:0, top:0, fmt:v=>(v*100).toFixed(0)+"%", tipFmt:v=>fmtPct(v,2,false)});

  renderPnl();
}

function renderPnl(){
  const a = account(), s = sliceRange(a.nav), box = el("pnlChart");
  const bars = s.slice(1).map((r,i)=>({d:r.d, v:r.eq - s[i].eq}));
  if(bars.length<1){ box.innerHTML = `<div class="empty">数据不足，暂无每日盈亏</div>`; return; }
  const W=760,H=180,L=58,R=12,T=10,B=26, vals=bars.map(b=>b.v);
  let lo=Math.min(0,...vals), hi=Math.max(0,...vals); const pad=(hi-lo)*0.08||1; lo-=pad; hi+=pad;
  const bw=(W-L-R)/bars.length, y=v=>T+(H-T-B)*(hi-v)/(hi-lo);
  let g="";
  const raw=(hi-lo)/4, mag=10**Math.floor(Math.log10(raw)), step=[1,2,5,10].map(k=>k*mag).find(k=>k>=raw);  // 整数刻度
  for(let v=Math.ceil(lo/step)*step; v<=hi; v+=step){ g+=`<line class="gridline" x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${L-8}" y="${y(v)+4}" text-anchor="end">${fmtNum(v)}</text>`; }
  g+=`<line class="zero" x1="${L}" x2="${W-R}" y1="${y(0)}" y2="${y(0)}"/>`;
  bars.forEach((b,i)=>{ const h=Math.abs(y(b.v)-y(0)), w=Math.max(1,bw-2); g+=`<rect x="${L+i*bw+1}" y="${Math.min(y(b.v),y(0))}" width="${w}" height="${Math.max(h,1)}" rx="${Math.min(2,w/2)}" fill="var(${b.v>=0?"--up":"--down"})" data-i="${i}"/>`; });
  [0, bars.length-1].forEach(i=>{ g+=`<text class="axis" x="${L+i*bw+bw/2}" y="${H-6}" text-anchor="${i===0?"start":"end"}">${bars[i].d.slice(2)}</text>`; });
  g+=`<rect x="${L}" y="${T}" width="${W-L-R}" height="${H-T-B}" fill="transparent"/>`;
  box.innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="每日盈亏">${g}</svg><div class="tip"></div>`;
  const svg=box.querySelector("svg"), tip=box.querySelector(".tip");
  svg.addEventListener("pointermove", ev=>{
    const r=svg.getBoundingClientRect(), px=(ev.clientX-r.left)*W/r.width, i=Math.max(0,Math.min(bars.length-1,Math.floor((px-L)/bw))), b=bars[i];
    const prevEq = s[i].eq;
    tip.innerHTML=`<div class="t">${b.d}</div><div class="r"><span>盈亏</span><b class="${cls(b.v)}">${fmtMoney(b.v,true)}</b></div><div class="r"><span>收益率</span><b class="${cls(b.v)}">${fmtPct(b.v/prevEq)}</b></div>`;
    tip.style.display="block"; const bx=box.getBoundingClientRect(), tx=ev.clientX-bx.left;
    tip.style.left=(tx>bx.width/2?tx-tip.offsetWidth-12:tx+12)+"px"; tip.style.top=Math.max(0,ev.clientY-bx.top-20)+"px";
  });
  svg.addEventListener("pointerleave", ()=>{ tip.style.display="none"; });
}

function renderMonthly(){
  const a = account(), m = a.monthly, bm = DATA.benchmark.monthly || {};
  const years = Object.keys(m).sort().reverse();
  if(!years.length){ el("monthly").innerHTML = `<div class="empty">暂无数据</div>`; return; }
  const all = years.flatMap(y=>Object.entries(m[y]).filter(([k])=>k!=="year").map(([,v])=>Math.abs(v)));
  const scale = Math.max(0.02, ...all);
  const cell = (v, bv) => {
    if(v==null) return `<td class="flat">—</td>`;
    const a = Math.min(1, Math.abs(v)/scale)*0.55+0.06, rgb = css(v>=0?"--hm-up":"--hm-down");
    return `<td style="background:rgba(${rgb},${a.toFixed(2)})" title="沪深300 ${fmtPct(bv)}">${fmtPct(v,1)}<div class="sub" style="font-size:10.5px">${bv==null?"":"("+fmtPct(bv,1)+")"}</div></td>`;
  };
  let h = `<table class="hm"><tr><th>年份</th>${[...Array(12)].map((_,i)=>`<th>${i+1}月</th>`).join("")}<th>全年</th></tr>`;
  years.forEach(y=>{ h+=`<tr><td style="text-align:left;border:0">${y}</td>${[...Array(12)].map((_,i)=>cell(m[y][i+1], bm[y]?.[i+1])).join("")}<td class="${cls(m[y].year)}" style="font-weight:600">${fmtPct(m[y].year,1)}<div class="sub" style="font-size:10.5px">${bm[y]?"("+fmtPct(bm[y].year,1)+")":""}</div></td></tr>`; });
  el("monthly").innerHTML = h + `</table>`;
}

function renderAlloc(){
  const a = account(), last = a.nav[a.nav.length-1];
  if(!last){ el("alloc").innerHTML=`<div class="empty">暂无数据</div>`; return; }
  const parts = [["个股", last.stock, "--s1"], ["底仓 ETF", last.etf, "--s4"], ["现金", last.cash, "--bench"]];
  el("alloc").innerHTML = `<div class="alloc">${parts.map(([n,v,c])=>v>0?`<div style="width:${v/last.eq*100}%;background:var(${c})" title="${n} ${fmtPct(v/last.eq,1,false)}"></div>`:"").join("")}</div>`
   + `<div class="kv">${parts.map(([n,v,c])=>`<span><i class="dot" style="background:var(${c})"></i>${n}</span><span>${fmtNum(v)}　${fmtPct(v/last.eq,1,false)}</span>`).join("")}`
   + `<span>持股数量</span><span>${a.positions.filter(p=>p.kind==="stock").length} 只</span>`
   + `<span>待强制卖出</span><span>${Object.keys(a.risk.pending).length ? Object.entries(a.risk.pending).map(([c,r])=>`<span class="badge warn">${esc(c)} ${esc(r)}</span>`).join(" ") : "无"}</span></div>`;
}

function renderStats(){
  const m = account().metrics;
  const kv = [["已平仓卖出笔数", m.sells ?? 0], ["胜率", fmtPct(m.win_rate,0,false)], ["盈亏比", m.payoff==null?"—":m.payoff.toFixed(2)],
    ["年化波动率", fmtPct(m.vol,1,false)], ["Sharpe", m.sharpe==null?"—":m.sharpe.toFixed(2)], ["Sortino", m.sortino==null?"—":m.sortino.toFixed(2)],
    ["收益回撤比", m.calmar==null?"—":m.calmar.toFixed(2)], ["累计交易成本", fmtNum(m.costs,0)+" 元"], ["换手率（累计）", m.turnover==null?"—":m.turnover.toFixed(2)+" 倍"]];
  el("stats").innerHTML = `<div class="kv">${kv.map(([k,v])=>`<span>${k}</span><span>${v}</span>`).join("")}</div><p class="sub" style="margin:10px 0 0">年化、波动率、Sharpe 需满 20 个交易日才计算。</p>`;
}

function renderPositions(){
  const a = account(), last = a.nav[a.nav.length-1];
  if(!a.positions.length){ el("positions").innerHTML = `<div class="empty">空仓</div>`; return; }
  el("positions").innerHTML = `<table><tr><th>名称</th><th>类型</th><th>股数</th><th>成本</th><th>市值</th><th>占比</th><th>浮动盈亏</th><th>盈亏率</th><th>止损价</th><th>首次买入</th></tr>`
   + a.positions.map(p=>`<tr><td>${esc(p.name)} <span class="sub">${esc(p.code)}</span></td><td>${p.kind==="etf"?"ETF":"个股"}</td><td>${fmtNum(p.shares)}</td><td>${fmtNum(p.cost/p.shares,3)}</td><td>${fmtNum(p.value)}</td><td>${fmtPct(p.value/last.eq,1,false)}</td><td class="${cls(p.pnl)}">${fmtMoney(p.pnl,true)}</td><td class="${cls(p.pnl)}">${fmtPct(p.pnl/p.cost)}</td><td>${p.stop??"—"}</td><td>${p.first}</td></tr>`).join("") + `</table>`;
}

function renderTrades(){
  const t = account().trades;
  if(!t.length){ el("trades").innerHTML = `<div class="empty">暂无成交</div>`; return; }
  const side = {buy:"买入", sell:"卖出"};
  el("trades").innerHTML = `<table><tr><th>日期</th><th>方向</th><th>名称</th><th>股数</th><th>价格</th><th>金额</th><th>已实现盈亏</th><th>原因</th></tr>`
   + t.map(r=>{ const p = r.realized_pnl===""?null:+r.realized_pnl; return `<tr><td>${r.date}</td><td class="${r.side==="buy"?"up":"down"}">${side[r.side]}</td><td>${esc(r.name)} <span class="sub">${esc(r.code)}</span></td><td>${fmtNum(+r.shares)}</td><td>${(+r.price).toFixed(3)}</td><td>${fmtNum(+r.amount)}</td><td class="${cls(p)}">${p==null?"—":fmtMoney(p,true)}</td><td>${esc(r.reason_type)}</td></tr>`; }).join("") + `</table>`;
}

function renderAll(){
  if(!accts.length){ document.querySelector("main").innerHTML = `<div class="card empty">还没有账户，先运行 python -m sim init</div>`; return; }
  renderSeg(); renderMeta(); renderKpis(); renderPeriods(); renderCharts(); renderAlloc(); renderStats(); renderMonthly(); renderPositions(); renderTrades();
}
renderAll();
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", renderAll);
</script>
</body></html>"""
