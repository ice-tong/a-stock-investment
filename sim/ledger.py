"""账户账本：现金、持仓、成交记录、净值。只有代码能写，Agent 不能直接修改这些文件。

accounts/<名称>/
  state.json   当前状态（现金、持仓、净值高点、熔断状态、待执行的强制卖出）
  trades.csv   全部成交与公司行动记录（只追加）
  nav.csv      每日净值（只追加）
"""
import csv
import json
from pathlib import Path

from . import config

TRADE_FIELDS = ["date", "account", "order_id", "code", "name", "side", "shares", "price", "amount",
                "commission", "stamp_tax", "transfer_fee", "slippage_cost", "realized_pnl",
                "new_position", "closed", "reason_type", "fill_source", "note"]
NAV_FIELDS = ["date", "cash", "stock_value", "etf_value", "equity", "stock_weight", "etf_weight",
              "peak", "drawdown", "regime", "risk_state",
              "active_etf_value", "active_etf_weight"]  # 追加列，旧行缺值读作空串


class Account:
    def __init__(self, name: str):
        self.name = name
        self.dir: Path = config.accounts_dir() / name
        with open(self.dir / "state.json", encoding="utf-8") as f:
            self.state = json.load(f)

    # ---------- 创建与保存 ----------
    @classmethod
    def create(cls, name: str, strategy: str, cash: float, start_date: str) -> "Account":
        d = config.accounts_dir() / name
        if (d / "state.json").exists():
            raise FileExistsError(f"账户 {name} 已存在")
        d.mkdir(parents=True, exist_ok=True)
        state = {"name": name, "strategy": strategy, "initial_cash": cash, "cash": cash,
                 "start_date": start_date, "positions": {}, "peak": cash, "halted": False,
                 "breaker_until": None, "dd_warn": False, "pending_exits": {},
                 "last_settled": None, "executed": []}
        (d / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        for fname, fields in (("trades.csv", TRADE_FIELDS), ("nav.csv", NAV_FIELDS)):
            with open(d / fname, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(fields)
        return cls(name)

    def save(self):
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.dir / "state.json")

    # ---------- 读取 ----------
    @property
    def cash(self) -> float:
        return self.state["cash"]

    @property
    def positions(self) -> dict:
        return self.state["positions"]

    def stock_codes(self) -> list:
        return [c for c, p in self.positions.items() if p["kind"] == "stock"]

    def values(self, prices: dict) -> tuple:
        """按给定价格（缺失时用持仓上次估值价）计算 (个股市值, ETF 市值, 总资产)。"""
        stock = etf = 0.0
        for code, p in self.positions.items():
            v = p["shares"] * prices.get(code, p["last_price"])
            if p["kind"] == "etf":
                etf += v
            else:
                stock += v
        return stock, etf, self.cash + stock + etf

    def weight(self, code: str, prices: dict, equity: float) -> float:
        p = self.positions.get(code)
        return 0.0 if not p or equity <= 0 else p["shares"] * prices.get(code, p["last_price"]) / equity

    def trades(self) -> list:
        with open(self.dir / "trades.csv", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def nav(self) -> list:
        with open(self.dir / "nav.csv", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    # ---------- 写入 ----------
    def append_trade(self, row: dict):
        row = {k: row.get(k, "") for k in TRADE_FIELDS} | {"account": self.name}
        with open(self.dir / "trades.csv", "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, TRADE_FIELDS).writerow(row)

    def append_nav(self, row: dict):
        with open(self.dir / "nav.csv", "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, NAV_FIELDS).writerow({k: row.get(k, "") for k in NAV_FIELDS})


def all_accounts() -> list:
    d = config.accounts_dir()
    if not d.exists():
        return []
    return [Account(p.name) for p in sorted(d.iterdir()) if (p / "state.json").exists()]
