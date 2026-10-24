#!/usr/bin/env python3
"""黄金做T条件提醒（518880 + 人民币金价参考）。

只做条件判断，不给买卖建议。输出以 ALERT 开头表示触发，NO_ALERT 表示不触发。

用法：
  .venv/bin/python tools/gold_alert.py              # 正常检查
  .venv/bin/python tools/gold_alert.py --self-test  # 离线自测条件逻辑
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, time
from zoneinfo import ZoneInfo

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(PROJECT, "data", "gold_alert_state.json")
TZ = ZoneInfo("Asia/Shanghai")
COOLDOWN_SEC = 2 * 60 * 60  # 同一信号 2 小时内不重复

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


def get_bars():
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol=sh518880&scale=5&ma=no&datalen=48"
    )
    return json.loads(fetch_text(url, encoding="utf-8"))


def rmb_from_usd(usd, fx):
    return usd * fx / OZ * PREMIUM


def in_session(now):
    if now.weekday() >= 5:
        return None
    t = now.time()
    if time(9, 30) <= t <= time(11, 30):
        return "A"
    if time(13, 0) <= t <= time(15, 0):
        return "A"
    if time(21, 0) <= t <= time(23, 0):
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


def eval_rmb(comex, fx):
    """美盘时段用 COMEX 折算人民币金价，做粗粒度条件判断。"""
    signals = []
    price = rmb_from_usd(comex["price"], fx)
    high = rmb_from_usd(comex["high"], fx)
    low = rmb_from_usd(comex["low"], fx)

    lo, hi = RMB["resistance"]
    if high >= lo and price <= hi - 2 and price <= high - 2:
        signals.append(("RESISTANCE_REJECT", f"触及 {high:.0f} 后回落到 {price:.0f}"))

    lo, hi = RMB["support"]
    if low <= hi and price >= lo + 1 and price >= low + 2:
        signals.append(("SUPPORT_HOLD", f"下探 {low:.0f} 后收回 {price:.0f}"))

    if price < RMB["breakdown"]:
        signals.append(("BREAKDOWN", f"折算价 {price:.0f}，跌破 {RMB['breakdown']}"))

    if price > RMB["reclaim"]:
        signals.append(("RECLAIM", f"折算价 {price:.0f}，站上 {RMB['reclaim']}"))

    return signals, price, high, low


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

    print("SELF_TEST_OK")


def main():
    if "--self-test" in sys.argv:
        self_test()
        return

    now = datetime.now(TZ)
    sess = in_session(now)
    if not sess:
        print(f"NO_ALERT {now:%Y-%m-%d %H:%M} 非交易时段（A股 9:30-11:30/13:00-15:00，美盘 21:00-23:00）")
        return

    try:
        etf = get_etf()
        comex = get_comex()
        fx = get_fx()
    except Exception as exc:
        print(f"NO_ALERT {now:%Y-%m-%d %H:%M} 取数失败：{exc}")
        return

    rmb = rmb_from_usd(comex["price"], fx)
    state = load_state()

    if sess == "A":
        try:
            bars = get_bars()
        except Exception as exc:
            print(f"NO_ALERT {now:%Y-%m-%d %H:%M} K线取数失败：{exc}")
            return
        if not bars or bars[-1].get("day", "")[:10] != now.strftime("%Y-%m-%d"):
            print(f"NO_ALERT {now:%Y-%m-%d %H:%M} 今日无K线（休市或数据未更新）")
            return

        signals = eval_etf(bars, etf["price"])
        if not signals:
            print(
                f"NO_ALERT {now:%Y-%m-%d %H:%M} 518880 {etf['price']:.3f}；"
                f"参考人民币金价 {rmb:.1f} 元/克；"
                f"距压力 {ETF['resistance'][0]:.2f} {((ETF['resistance'][0]/etf['price']-1)*100):+.1f}%，"
                f"距支撑 {ETF['support'][1]:.2f} {((ETF['support'][1]/etf['price']-1)*100):+.1f}%"
            )
            return

        lines = [f"ALERT {now:%Y-%m-%d %H:%M} 518880 {etf['price']:.3f}（高 {etf['high']:.3f} / 低 {etf['low']:.3f}）"]
        lines.append(f"参考人民币金价 {rmb:.1f} 元/克（COMEX {comex['price']:.1f} × {fx:.4f}）")
        for name, detail in signals:
            if fresh_signal(state, f"ETF_{name}", now):
                rule = {
                    "RESISTANCE_REJECT": "890-898 减仓/反向T窗口；参考止损 8.65（4230 美元），目标 8.09/7.98",
                    "SUPPORT_HOLD": "852-860 接回窗口；参考止损 8.01（3915 美元），目标 8.45-8.51",
                    "SUPPORT_HOLD_DEEP": "841 附近接回窗口；跌破 7.98 停止接回",
                    "BREAKDOWN": "跌破 841，停止接回；下一支撑 830/800",
                    "RECLAIM": "站上 918，趋势修复，停止做空思路",
                }[name]
                lines.append(f"信号: {name} — {detail}")
                lines.append(f"规则: {rule}")
        if len(lines) == 2:
            print(f"NO_ALERT {now:%Y-%m-%d %H:%M} 信号在冷却期内，不重复提醒")
            return
        lines.append("说明: 条件提醒，不是买卖建议；银行积存金以实际报价为准。")
        print("\n".join(lines))
        return

    # 美盘：ETF 已收盘，用 COMEX 折算人民币金价
    signals, price, high, low = eval_rmb(comex, fx)
    if not signals:
        print(f"NO_ALERT {now:%Y-%m-%d %H:%M} 美盘折算人民币金价 {price:.1f} 元/克（高 {high:.0f}/低 {low:.0f}）")
        return

    lines = [f"ALERT {now:%Y-%m-%d %H:%M} 美盘折算人民币金价 {price:.1f} 元/克（高 {high:.0f}/低 {low:.0f}）"]
    for name, detail in signals:
        if fresh_signal(state, f"RMB_{name}", now):
            rule = {
                "RESISTANCE_REJECT": "890-898 减仓窗口；银行积存金注意点差",
                "SUPPORT_HOLD": "852-860 接回窗口；跌破 841 停止接回",
                "BREAKDOWN": "跌破 841，停止接回",
                "RECLAIM": "站上 918，趋势修复",
            }[name]
            lines.append(f"信号: {name} — {detail}")
            lines.append(f"规则: {rule}")
    if len(lines) == 1:
        print(f"NO_ALERT {now:%Y-%m-%d %H:%M} 信号在冷却期内，不重复提醒")
        return
    lines.append("说明: 折算价含约 0.5% 溢价，实际积存金报价以银行为准；不是买卖建议。")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
