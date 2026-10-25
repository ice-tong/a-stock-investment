#!/usr/bin/env python3
"""黄金做T条件提醒（518880 + 招商银行积存金参考）。

只做条件判断，不给买卖建议。输出以 ALERT 开头表示触发，NO_ALERT 表示不触发。

用法：
  .venv/bin/python tools/gold_alert.py              # 正常检查
  .venv/bin/python tools/gold_alert.py --self-test  # 离线自测条件逻辑
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, time
from zoneinfo import ZoneInfo

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(PROJECT, "data", "gold_alert_state.json")
TZ = ZoneInfo("Asia/Shanghai")
COOLDOWN_SEC = 2 * 60 * 60  # 同一信号 2 小时内不重复

# 行情源都在国内，绕过系统代理，避免代理端口失效时 urllib 全部失败（同 sim/__init__.py）
os.environ.setdefault("NO_PROXY", "*")

# 518880 价位（元/份），由人民币金价按 864 元/克 ↔ 8.199 折算
ETF = {
    "resistance": (8.45, 8.52),   # 890-898 元/克
    "support": (8.09, 8.16),      # 852-860 元/克
    "deep_support": (7.98, 8.03), # 841 元/克附近
    "breakdown": 7.98,            # 841 元/克
    "reclaim": 8.71,              # 918 元/克
}

# 人民币金价（元/克）
RMB = {
    "resistance": (890, 898),
    "support": (852, 860),
    "breakdown": 841,
    "reclaim": 918,
}

OZ = 31.1035
PREMIUM = 1.0049  # SGE 对国际金价约 +0.5% 溢价

# 招商银行积存金：跟随上金所 Au99.99，买卖各 2.5 元/克点差（2026-06-29 起，合计 5）
# 招行未公开报价接口，此处为按上金所行情折算的参考价；点差调整时改这里
CMB_SPREAD = 2.5


def fetch_text(url, encoding="gbk"):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode(encoding, errors="replace")


def sina_payload(code):
    txt = fetch_text(f"https://hq.sinajs.cn/list={code}")
    if '="' not in txt:
        raise ValueError(f"bad sina payload for {code}")
    return txt.split('="', 1)[1].rsplit('"', 1)[0]


def get_etf():
    f = sina_payload("sh518880").split(",")
    if len(f) < 10 or not f[3]:
        raise ValueError("empty 518880 quote")
    return {
        "name": f[0],
        "open": float(f[1]),
        "prev": float(f[2]),
        "price": float(f[3]),
        "high": float(f[4]),
        "low": float(f[5]),
        "volume": float(f[8]),
        "date": f[30] if len(f) > 30 else "",
        "time": f[31] if len(f) > 31 else "",
    }


def get_comex():
    f = sina_payload("hf_GC").split(",")
    return {
        "price": float(f[0]),
        "close": float(f[2]),
        "open": float(f[3]),
        "high": float(f[4]),
        "low": float(f[5]),
        "time": f[6],
        "prev": float(f[7]),
    }


def get_fx():
    f = sina_payload("fx_susdcny").split(",")
    # 第 4 个字段为最新价（前三个为时间/买/卖）
    return float(f[3])


def fetch_sge(instid="Au99.99"):
    data = urllib.parse.urlencode({"instid": instid}).encode()
    req = urllib.request.Request(
        "https://www.sge.com.cn/graph/quotations",
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.sge.com.cn/",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _hhmm(t):
    h, m = str(t).split(":")[:2]
    return int(h) * 60 + int(m)


def parse_sge(payload, now):
    """从上金所分钟序列取当前时段最新价。

    序列按交易日排列（前一晚 20:00 → 次日 15:30），跨零点处时分回绕，这里统一
    换成交易日内的分钟序号再比较。若序列里存在晚于此刻的点，说明拿到的是已收市或
    前一交易日的整日数据，判为不实时。
    """
    times, values = payload.get("times") or [], payload.get("data") or []
    pts = []
    for t, v in zip(times, values):
        try:
            pts.append((_hhmm(t), str(t), float(v)))
        except (TypeError, ValueError):
            continue
    if not pts:
        return None

    wrap = next((i for i in range(1, len(pts)) if pts[i][0] < pts[i - 1][0]), None)
    if wrap is None:  # 序列不含跨零点，只有单一时段
        keyed = [(tod, t, v) for tod, t, v in pts]
    else:
        keyed = [
            (tod if i < wrap else tod + 1440, t, v)
            for i, (tod, t, v) in enumerate(pts)
        ]

    now_tod = now.hour * 60 + now.minute
    if wrap is None:
        now_key = now_tod
    else:
        now_key = now_tod if now_tod >= 20 * 60 else now_tod + 1440

    if max(k for k, _, _ in keyed) > now_key + 5:
        return None

    live = [x for x in keyed if x[0] <= now_key]
    if not live:
        return None
    last_key, last_t, price = max(live, key=lambda x: x[0])
    if now_key - last_key > 10:
        return None

    window = [v for k, _, v in live if last_key - k <= 30]
    return {
        "price": price,
        "time": last_t,
        "high": max(window),
        "low": min(window),
    }


def sge_quote(now):
    return parse_sge(fetch_sge(), now)


def cmb_range(sge_price):
    """招行积存金参考价区间（低到高），含买卖双边点差。"""
    return sge_price - CMB_SPREAD, sge_price + CMB_SPREAD


def get_bars():
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol=sh518880&scale=5&ma=no&datalen=48"
    )
    return json.loads(fetch_text(url, encoding="utf-8"))


def rmb_from_usd(usd, fx):
    return usd * fx / OZ * PREMIUM


def in_session(now):
    """交易时段：A股 9:30-11:30/13:00-15:00；上金所夜盘 20:00-次日 02:30。

    上金所一个交易日从 20:00 起算，周五夜盘的尾巴落在周六凌晨，要算作交易时段；
    反过来周一凌晨属于周日夜间，没有行情。
    """
    t = now.time()
    wd = now.weekday()  # 0=周一
    if t <= time(2, 30):
        if wd in (0, 6):
            return None
        return "US"
    if wd >= 5:
        return None
    if time(9, 30) <= t <= time(11, 30):
        return "A"
    if time(13, 0) <= t <= time(15, 0):
        return "A"
    if t >= time(20, 0):
        return "US"
    return None


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def fresh_signal(state, key, now):
    last = state.get(key)
    if last and now.timestamp() - float(last) < COOLDOWN_SEC:
        return False
    state[key] = now.timestamp()
    save_state(state)
    return True


def eval_etf(bars, price):
    """用最近 30 分钟（6 根 5 分钟 K）判断触及后回落/收回。"""
    signals = []
    recent = bars[-6:]
    highs = [float(b["high"]) for b in recent]
    lows = [float(b["low"]) for b in recent]
    closes = [float(b["close"]) for b in recent]
    max_high, min_low, last = max(highs), min(lows), closes[-1]

    lo, hi = ETF["resistance"]
    if max_high >= lo and last <= hi - 0.02 and last <= max_high - 0.02:
        signals.append(
            ("RESISTANCE_REJECT", f"触及 {max_high:.3f} 后回落到 {last:.3f}")
        )

    lo, hi = ETF["support"]
    if min_low <= hi and last >= lo + 0.01 and last >= min_low + 0.02:
        signals.append(
            ("SUPPORT_HOLD", f"下探 {min_low:.3f} 后收回 {last:.3f}")
        )

    lo, hi = ETF["deep_support"]
    if min_low <= hi and last >= lo + 0.02 and last >= min_low + 0.02:
        signals.append(
            ("SUPPORT_HOLD_DEEP", f"下探 {min_low:.3f} 后收回 {last:.3f}")
        )

    if last < ETF["breakdown"]:
        signals.append(("BREAKDOWN", f"收在 {last:.3f}，跌破 {ETF['breakdown']}"))

    if last > ETF["reclaim"]:
        signals.append(("RECLAIM", f"收在 {last:.3f}，站上 {ETF['reclaim']}"))

    return signals


def eval_rmb_signals(price, high, low):
    """按人民币金价（元/克）做粗粒度条件判断。"""
    signals = []

    lo, hi = RMB["resistance"]
    if high >= lo and price <= hi - 2 and price <= high - 2:
        signals.append(("RESISTANCE_REJECT", f"触及 {high:.0f} 后回落到 {price:.0f}"))

    lo, hi = RMB["support"]
    if low <= hi and price >= lo + 1 and price >= low + 2:
        signals.append(("SUPPORT_HOLD", f"下探 {low:.0f} 后收回 {price:.0f}"))

    if price < RMB["breakdown"]:
        signals.append(("BREAKDOWN", f"现价 {price:.0f}，跌破 {RMB['breakdown']}"))

    if price > RMB["reclaim"]:
        signals.append(("RECLAIM", f"现价 {price:.0f}，站上 {RMB['reclaim']}"))

    return signals


def eval_rmb(comex, fx):
    """美盘时段用 COMEX 折算人民币金价，做粗粒度条件判断。"""
    price = rmb_from_usd(comex["price"], fx)
    high = rmb_from_usd(comex["high"], fx)
    low = rmb_from_usd(comex["low"], fx)
    return eval_rmb_signals(price, high, low), price, high, low


RULES_ETF = {
    "RESISTANCE_REJECT": "890-898 减仓/反向T窗口；参考止损 8.65（4230 美元），目标 8.09/7.98",
    "SUPPORT_HOLD": "852-860 接回窗口；参考止损 8.01（3915 美元），目标 8.45-8.51",
    "SUPPORT_HOLD_DEEP": "841 附近接回窗口；跌破 7.98 停止接回",
    "BREAKDOWN": "跌破 841，停止接回；下一支撑 830/800",
    "RECLAIM": "站上 918，趋势修复，停止做空思路",
}

RULES_RMB = {
    "RESISTANCE_REJECT": "890-898 减仓窗口；积存金注意点差，以招行 App 报价为准",
    "SUPPORT_HOLD": "852-860 接回窗口；跌破 841 停止接回",
    "BREAKDOWN": "跌破 841，停止接回",
    "RECLAIM": "站上 918，趋势修复",
}


def cmb_note(sge):
    if not sge:
        return "招行积存金：上金所报价未更新，本次不判断"
    lo, hi = cmb_range(sge["price"])
    return (
        f"招行积存金参考 {lo:.1f}-{hi:.1f} 元/克"
        f"（上金所 Au99.99 {sge['price']:.1f}，买卖各 {CMB_SPREAD:g} 元/克点差）"
    )


def signal_lines(state, now, signals, prefix, rules, tag=""):
    out = []
    for name, detail in signals:
        if not fresh_signal(state, f"{prefix}{name}", now):
            continue
        out.append(f"信号: {name}{tag} — {detail}")
        out.append(f"规则: {rules[name]}")
    return out


def self_test():
    def bars(seq):
        return [
            {"open": str(o), "high": str(h), "low": str(l), "close": str(c)}
            for o, h, l, c in seq
        ]

    # 压力位冲高回落
    b = bars([(8.40, 8.46, 8.39, 8.45), (8.45, 8.50, 8.42, 8.43)])
    assert any(s[0] == "RESISTANCE_REJECT" for s in eval_etf(b, 8.43)), b

    # 支撑位下探收回
    b = bars([(8.20, 8.21, 8.10, 8.13), (8.13, 8.18, 8.11, 8.17)])
    assert any(s[0] == "SUPPORT_HOLD" for s in eval_etf(b, 8.17)), b

    # 破位
    b = bars([(8.05, 8.06, 7.96, 7.97)])
    assert any(s[0] == "BREAKDOWN" for s in eval_etf(b, 7.97)), b

    # 收复
    b = bars([(8.66, 8.73, 8.65, 8.72)])
    assert any(s[0] == "RECLAIM" for s in eval_etf(b, 8.72)), b

    # 不触发
    b = bars([(8.30, 8.33, 8.28, 8.31)])
    assert not eval_etf(b, 8.31), b

    # 积存金：压力位冲高回落
    assert any(s[0] == "RESISTANCE_REJECT" for s in eval_rmb_signals(892, 895, 885))
    # 积存金：支撑下探收回
    assert any(s[0] == "SUPPORT_HOLD" for s in eval_rmb_signals(856, 862, 850))
    # 积存金区间含双边点差
    assert cmb_range(900) == (897.5, 902.5)

    def at(h, m):
        return datetime(2026, 10, 23, h, m, tzinfo=TZ)

    # 夜盘实时数据
    q = parse_sge(
        {"times": ["20:58", "20:59", "21:00"], "data": ["860.1", "860.4", "860.6"]},
        at(21, 0),
    )
    assert q and q["price"] == 860.6 and q["low"] == 860.1 and q["high"] == 860.6, q
    # 序列里有点晚于此刻 = 拿到已收市/前一交易日的整日数据
    assert parse_sge({"times": ["09:30", "10:00"], "data": ["850", "851"]}, at(9, 31)) is None
    # 最新点落后超过 10 分钟 = 未更新
    assert parse_sge({"times": ["09:00", "09:01"], "data": ["850", "851"]}, at(9, 31)) is None
    # 空值跳过
    q = parse_sge(
        {"times": ["09:00", "09:01", "09:02"], "data": ["850", "", None]}, at(9, 2)
    )
    assert q and q["price"] == 850, q
    # 跨零点序列：此刻之后还有点位 = 前一交易日的整日数据
    cross = {
        "times": ["23:59", "00:00", "00:01", "09:00", "09:30"],
        "data": ["861", "862", "863", "864", "865"],
    }
    assert parse_sge(cross, at(0, 1)) is None
    q = parse_sge(cross, at(9, 30))
    assert q and q["price"] == 865 and q["low"] == 864, q

    def dow(d, h, m):
        return datetime(2026, 10, d, h, m, tzinfo=TZ)

    assert in_session(dow(23, 10, 0)) == "A"      # 周五上午
    assert in_session(dow(23, 22, 0)) == "US"     # 周五夜盘
    assert in_session(dow(24, 1, 0)) == "US"      # 周六凌晨属周五夜盘
    assert in_session(dow(26, 1, 0)) is None      # 周一凌晨（周日夜间无交易）
    assert in_session(dow(25, 22, 0)) is None     # 周日夜盘
    assert in_session(dow(23, 16, 0)) is None     # 收盘后

    print("SELF_TEST_OK")


def main():
    if "--self-test" in sys.argv:
        self_test()
        return

    now = datetime.now(TZ)
    sess = in_session(now)
    if not sess:
        print(f"NO_ALERT {now:%Y-%m-%d %H:%M} 非交易时段（A股 9:30-11:30/13:00-15:00，上金所夜盘 20:00-次日02:30）")
        return

    try:
        etf = get_etf()
        comex = get_comex()
        fx = get_fx()
    except Exception as exc:
        print(f"NO_ALERT {now:%Y-%m-%d %H:%M} 取数失败：{exc}")
        return

    try:
        sge = sge_quote(now)
    except Exception:
        sge = None
    cmb_signals = (
        eval_rmb_signals(sge["price"], sge["high"], sge["low"]) if sge else []
    )
    state = load_state()

    if sess == "A":
        etf_signals, etf_note = [], ""
        try:
            bars = get_bars()
            if bars and bars[-1].get("day", "")[:10] == now.strftime("%Y-%m-%d"):
                etf_signals = eval_etf(bars, etf["price"])
            else:
                etf_note = "518880 无今日K线（休市或数据未更新），本次不判断 ETF 信号"
        except Exception as exc:
            etf_note = f"518880 K线取数失败（{exc}），本次不判断 ETF 信号"

        if not etf_signals and not cmb_signals:
            note = f"；{etf_note}" if etf_note else ""
            print(
                f"NO_ALERT {now:%Y-%m-%d %H:%M} 518880 {etf['price']:.3f}；"
                f"{cmb_note(sge)}{note}；"
                f"距压力 {ETF['resistance'][0]:.2f} {((ETF['resistance'][0]/etf['price']-1)*100):+.1f}%，"
                f"距支撑 {ETF['support'][1]:.2f} {((ETF['support'][1]/etf['price']-1)*100):+.1f}%"
            )
            return

        lines = [
            f"ALERT {now:%Y-%m-%d %H:%M} 518880 {etf['price']:.3f}"
            f"（高 {etf['high']:.3f} / 低 {etf['low']:.3f}）"
        ]
        lines.append(cmb_note(sge))
        if etf_note:
            lines.append(etf_note)
        base = len(lines)
        lines += signal_lines(state, now, etf_signals, "ETF_", RULES_ETF)
        lines += signal_lines(
            state, now, cmb_signals, "CMB_", RULES_RMB, tag="（积存金）"
        )
        if len(lines) == base:
            print(f"NO_ALERT {now:%Y-%m-%d %H:%M} 信号在冷却期内，不重复提醒")
            return
        lines.append("说明: 条件提醒，不是买卖建议；积存金实际报价以招行 App 为准。")
        print("\n".join(lines))
        return

    # 美盘：ETF 已收盘。优先上金所夜盘（与积存金同源），取不到再用 COMEX 折算
    if sge:
        signals, prefix = cmb_signals, "CMB_"
        price, high, low = sge["price"], sge["high"], sge["low"]
        lines = [
            f"ALERT {now:%Y-%m-%d %H:%M} 上金所 Au99.99 {price:.1f} 元/克"
            f"（高 {high:.0f} / 低 {low:.0f}）",
            cmb_note(sge),
        ]
    else:
        signals, price, high, low = eval_rmb(comex, fx)
        prefix = "RMB_"
        lines = [
            f"ALERT {now:%Y-%m-%d %H:%M} 美盘折算人民币金价 {price:.1f} 元/克"
            f"（高 {high:.0f} / 低 {low:.0f}）",
            "招行积存金：上金所报价未更新，按 COMEX 折算参考",
        ]

    if not signals:
        print(
            f"NO_ALERT {now:%Y-%m-%d %H:%M} 人民币金价 {price:.1f} 元/克"
            f"（高 {high:.0f} / 低 {low:.0f}）；{cmb_note(sge)}"
        )
        return

    lines += signal_lines(
        state,
        now,
        signals,
        prefix,
        RULES_RMB,
        tag="（积存金）" if prefix == "CMB_" else "（COMEX 折算）",
    )
    if len(lines) == 2:
        print(f"NO_ALERT {now:%Y-%m-%d %H:%M} 信号在冷却期内，不重复提醒")
        return
    lines.append("说明: 条件提醒，不是买卖建议；积存金实际报价以招行 App 为准。")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
