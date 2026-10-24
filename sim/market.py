"""行情数据访问（新浪数据源）。代码统一用新浪格式：sh600519 / sz000001 / sh510300 / sh000300。

所有方法在取不到数据时返回 None，由调用方决定"不交易"，绝不臆造价格。
"""
import datetime as dt
import os
from typing import NamedTuple

os.environ.setdefault("TQDM_DISABLE", "1")

import pandas as pd


class Snapshot(NamedTuple):
    price: float          # 执行时点价格
    volume: float         # 当日截至执行时点的成交量（股）
    prev_close: float
    limit_up: float
    limit_down: float
    source: str           # minute：14:45 分钟线；close_fallback：退回用收盘价


def kind(code: str) -> str:
    if code.startswith("sh000") or code.startswith("sz399"):
        return "index"
    if code.startswith(("sh5", "sz15", "sz16")):
        return "etf"
    return "stock"


def limit_pct(code: str, name: str = "") -> float:
    num = code[2:]
    if kind(code) == "etf":
        return 0.10
    if "ST" in name.upper():
        return 0.05
    if code.startswith("bj"):
        return 0.30
    if num.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def limit_prices(code: str, name: str, prev_close: float) -> tuple:
    pct = limit_pct(code, name)
    digits = 3 if kind(code) == "etf" else 2
    up = round(prev_close * (1 + pct) + 1e-9, digits)
    down = round(prev_close * (1 - pct) + 1e-9, digits)
    return up, down


class Market:
    def __init__(self, exec_time: str = "14:45"):
        self.exec_time = exec_time
        self._daily = {}
        self._minute = {}
        self._factor = {}

    # ---------- 日线 ----------
    def daily(self, code: str, end: str | None = None) -> pd.DataFrame | None:
        """不复权日线，index 为日期字符串，列 open/high/low/close/volume/amount。"""
        if code not in self._daily:
            self._daily[code] = self._fetch_daily(code)
        df = self._daily[code]
        if df is None:
            return None
        return df if end is None else df[df.index <= end]

    def _fetch_daily(self, code):
        import akshare as ak
        try:
            k = kind(code)
            if k == "etf":
                df = ak.fund_etf_hist_sina(symbol=code)
            elif k == "index":
                df = ak.stock_zh_index_daily(symbol=code)
            else:
                start = (dt.date.today() - dt.timedelta(days=400)).strftime("%Y%m%d")
                df = ak.stock_zh_a_daily(symbol=code, start_date=start,
                                         end_date=dt.date.today().strftime("%Y%m%d"), adjust="")
        except Exception:
            return None
        if df is None or len(df) == 0:
            return None
        df = df.copy()
        df["date"] = df["date"].astype(str).str[:10]
        if "amount" not in df.columns:
            df["amount"] = float("nan")
        return df.set_index("date")[["open", "high", "low", "close", "volume", "amount"]].astype(float)

    def bar(self, code: str, date: str):
        df = self.daily(code)
        if df is None or date not in df.index:
            return None
        return df.loc[date]

    def close(self, code: str, date: str) -> float | None:
        b = self.bar(code, date)
        return None if b is None else float(b["close"])

    def last_close(self, code: str, date: str) -> float | None:
        """date 当天或之前最近一个有成交的收盘价（停牌股估值用）。"""
        df = self.daily(code, date)
        return None if df is None or len(df) == 0 else float(df["close"].iloc[-1])

    def prev_close(self, code: str, date: str) -> float | None:
        df = self.daily(code)
        if df is None:
            return None
        before = df[df.index < date]
        return None if len(before) == 0 else float(before["close"].iloc[-1])

    def avg_amount(self, code: str, date: str, n: int = 20) -> float | None:
        df = self.daily(code, date)
        if df is None or len(df) == 0:
            return None
        return float(df["amount"].tail(n).mean())

    # ---------- 分钟线与执行快照 ----------
    def _minutes(self, code):
        if code not in self._minute:
            import akshare as ak
            try:
                df = ak.stock_zh_a_minute(symbol=code, period="1", adjust="")
                df["day"] = df["day"].astype(str)
                df[["close", "volume"]] = df[["close", "volume"]].astype(float)
                self._minute[code] = df
            except Exception:
                self._minute[code] = None
        return self._minute[code]

    def snapshot(self, code: str, date: str, name: str = "") -> Snapshot | None:
        prev = self.prev_close(code, date)
        if prev is None:
            return None
        up, down = limit_prices(code, name, prev)
        m = self._minutes(code)
        if m is not None:
            day = m[m["day"].str.startswith(date)]
            cut = day[day["day"].str[11:16] <= self.exec_time]
            if len(cut) > 0:
                return Snapshot(float(cut["close"].iloc[-1]), float(cut["volume"].sum()), prev, up, down, "minute")
        b = self.bar(code, date)
        if b is None:
            return None  # 停牌或数据缺失
        return Snapshot(float(b["close"]), float(b["volume"]), prev, up, down, "close_fallback")

    # ---------- 复权因子（处理分红送转） ----------
    def hfq_factor(self, code: str, date: str) -> float:
        if kind(code) != "stock":
            return 1.0
        if code not in self._factor:
            import akshare as ak
            try:
                df = ak.stock_zh_a_daily(symbol=code, adjust="hfq-factor")
                df["date"] = df["date"].astype(str).str[:10]
                self._factor[code] = df.sort_values("date")
            except Exception:
                self._factor[code] = None
        df = self._factor[code]
        if df is None:
            return 1.0
        df = df[df["date"] <= date]
        return 1.0 if len(df) == 0 else float(df["hfq_factor"].iloc[-1])

    # ---------- 全市场快照（初筛用） ----------
    def spot(self) -> pd.DataFrame:
        import akshare as ak
        return ak.stock_zh_a_spot()
