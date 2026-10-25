"""候选池 v1.1 分诊与 v2 影子引擎的单元测试（不联网）。"""
import math

import pandas as pd
import pytest

from sim import pool, pool_v2


def _row(**kw):
    base = dict(code="sh600000", name="甲", industry="银行", pe=10.0, close=10.0,
                ma20=10.0, ma60=10.0, ma120=9.0, hi60=10.5, vol_60=0.20, vol_20=0.20,
                ret_20=0.05, ret_60=0.08, ret_120=0.10, chg_5d=-0.01, dd_60=-0.03,
                near_high_60=False, amt_ratio=1.0, avg_amount=2e8, one_lot_value=1000.0,
                n_channels=1, eligible=True, channels="lowvol")
    base.update(kw)
    return base


def test_stop_prob_formula_and_monotonic():
    p20, p40 = pool.stop_prob(0.20), pool.stop_prob(0.40)
    assert p20 == pytest.approx(0.157, abs=0.01)
    assert p40 == pytest.approx(0.482, abs=0.02)
    assert p20 < p40
    assert math.isnan(pool.stop_prob(0)) and math.isnan(pool.stop_prob(None))


def test_triage_actions_and_regime_order():
    feat = pd.DataFrame([
        _row(code="a", channels="defensive", close=10.0, ma60=10.0, vol_60=0.20),          # 低波 ready
        _row(code="b", channels="momentum", close=12.0, ma20=10.0, vol_60=0.30, ret_20=0.10),  # 距 MA20 远
        _row(code="c", channels="momentum", close=11.0, ma20=10.0, vol_60=0.30, ret_20=0.50),  # 极端 -> avoid
        _row(code="d", channels="holding", eligible=False, close=10.0),                    # 持仓
    ])
    out = pool.triage(feat, "defense")
    acts = out.set_index("code")["action"].to_dict()
    assert acts["a"] == "ready" and acts["c"] == "avoid" and acts["d"] == "holding"
    assert acts["b"] in ("wait_pullback", "watch")
    # ready 在 wait 之前，avoid 在最后
    order = out["code"].tolist()
    assert order.index("a") < order.index("b") < order.index("c")


def test_markdown_has_triage_sections_and_legacy_multi_channel():
    feat = pd.DataFrame([
        _row(code="a", channels="defensive", close=10.0, ma60=10.0),
        _row(code="b", channels="momentum|trend", close=11.0, ma20=10.0),
        _row(code="d", channels="holding", eligible=False),
    ])
    tri = pool.triage(feat, "defense")
    md = pool.to_markdown("2026-10-23", tri, {"momentum": ["b"], "trend": ["b"]},
                          {"spot": 3, "base": 3, "pre": 3, "feat": 3, "eligible": 2},
                          regime="defense", watch_items=[])
    assert "今日可行动" in md and "多通道共振" in md and "（不合规）" in md
    assert "观察名单为空" in md


def test_watch_status_structured_and_unstructured():
    feat = pd.DataFrame([_row(code="sh600001", close=10.0)])
    items = [
        {"code": "sh600001", "name": "甲", "status": "watching", "trigger_type": "price_above",
         "trigger_level": 10.0, "trigger": "站上 10.0"},
        {"code": "sh600001", "name": "甲", "status": "researched", "trigger_type": "price_below",
         "trigger_level": 9.0, "trigger": "回踩 9.0"},
        {"code": "sh600001", "name": "甲", "status": "watching", "trigger": "三季报后毛利率企稳"},
    ]
    ws = pool.watch_status(items, feat)
    assert ws[0]["hit"] is True and ws[1]["hit"] is False
    assert "人工复核" in ws[2]["note"]


def _v2_feat(n=20):
    rows = []
    for i in range(n):
        ind = f"行业{i % 4}"
        close = 10.0 + i * 0.1
        rows.append(_row(code=f"sh{600000 + i}", name=f"股{i}", industry=ind, close=close,
                         ma20=close * 0.99, ma60=close * 0.96, ma120=close * 0.90,
                         hi60=close * 1.02, vol_60=0.25 + (i % 5) * 0.05,
                         ret_20=0.02 + (i % 6) * 0.02, chg_5d=-0.01, dd_60=-0.03,
                         near_high_60=(i % 3 == 0), amt_ratio=1.8 if i % 3 == 0 else 0.7,
                         pe=10.0 + i, one_lot_value=close * 100,
                         channels="defensive" if i % 2 == 0 else "pullback"))
    return pd.DataFrame(rows)


def _v2_cfg(total=12):
    return {"total": total, "per_industry": 3, "min_industries": 4, "stop_prob_max": 0.45,
            "vol_max": 0.60, "ret20_max": 0.30, "max_lot_value": 5000,
            "weights": {"defense": {"lowvol": 35, "pullback": 25, "squeeze": 20, "breakout": 10, "reversal": 10},
                        "neutral": {"lowvol": 20, "squeeze": 20, "pullback": 25, "breakout": 20, "reversal": 15},
                        "offense": {"breakout": 30, "squeeze": 20, "pullback": 20, "lowvol": 15, "reversal": 15}}}


def test_pool_v2_quotas_sum_to_total():
    cfg = _v2_cfg(total=56)
    for regime in ("defense", "neutral", "offense"):
        q = pool_v2.quotas(cfg, regime)
        assert sum(q.values()) == 56
        assert set(q) == set(pool_v2.STYLES)


def test_pool_v2_build_respects_risk_and_industry_cap():
    feat = _v2_feat(20)
    cfg = _v2_cfg(total=12)
    rows = pool_v2.build(feat, {}, [], [], cfg, "defense")
    assert 0 < len(rows) <= 12
    assert set(rows["style"]) <= set(pool_v2.STYLES) | {"other", "holding", "watchlist"}
    # 行业上限 3（持仓/观察不占普通配额，但本测试没有它们）
    assert rows.groupby("industry").size().max() <= 3
    # 极端/高风险不进入可行动
    risky = rows[rows["action"] == "ready"]
    assert (risky["ret_20"] <= 0.30 + 1e-9).all()
    assert (risky["stop_prob"] <= 0.45 + 1e-9).all()
    assert len(rows["industry"].unique()) >= 4


def test_pool_v2_includes_holding_and_markdown():
    feat = _v2_feat(20)
    cfg = _v2_cfg(total=8)
    rows = pool_v2.build(feat, {}, ["sh699999"], [], cfg, "defense")
    assert "sh699999" not in set(rows["code"])  # 不在特征表里的持仓不会凭空出现
    feat2 = pd.concat([feat, pd.DataFrame([_row(code="sh699999", name="持仓", channels="holding",
                                                eligible=False, industry="行业0")])], ignore_index=True)
    rows = pool_v2.build(feat2, {}, ["sh699999"], [], cfg, "defense")
    assert "sh699999" in set(rows["code"])
    assert rows.set_index("code").loc["sh699999", "style"] == "holding"
    md = pool_v2.to_markdown("2026-10-23", rows, cfg, "defense", {"base": 21})
    assert "影子候选池 v2" in md and "实验属性" in md
