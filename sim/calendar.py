"""A 股交易日历（新浪数据源，含节假日与调休），本地缓存。日期统一用 'YYYY-MM-DD' 字符串。"""
import bisect
import datetime as dt
import functools

import pandas as pd

from . import config


@functools.lru_cache(maxsize=1)
def trade_dates() -> tuple:
    path = config.cache_dir() / "trade_dates.csv"
    # 缓存要覆盖到今天之后至少 30 天，否则年末算"下一交易日"会越界（12-31 晚上要冻结次年首个交易日的订单）
    horizon = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    cached = []
    if path.exists():
        cached = pd.read_csv(path)["trade_date"].astype(str).tolist()
        if cached and cached[-1] >= horizon:
            return tuple(cached)
    import akshare as ak
    try:
        df = ak.tool_trade_date_hist_sina()
    except Exception:
        if cached:  # 取不到新日历时沿用缓存；真的越界时由调用方报错，绝不臆造交易日
            return tuple(cached)
        raise
    dates = [str(d)[:10] for d in df["trade_date"]]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"trade_date": dates}).to_csv(path, index=False)
    return tuple(dates)


def today() -> str:
    return dt.date.today().isoformat()


def is_trading_day(d: str) -> bool:
    dates = trade_dates()
    i = bisect.bisect_left(dates, d)
    return i < len(dates) and dates[i] == d


def next_day(d: str) -> str:
    dates = trade_dates()
    return dates[bisect.bisect_right(dates, d)]


def prev_day(d: str) -> str:
    dates = trade_dates()
    return dates[bisect.bisect_left(dates, d) - 1]


def evening_before(today: str) -> str | None:
    """今天若是某个交易日的前一自然日（前夜），返回那个交易日；否则 None。

    前夜是日历概念：周日晚是周一的前夜，周五晚不是周一的前夜；节假日后首个交易日的
    前夜可能是假期最后一晚。冻结时点用它，保证周末/节假日的信息能进订单。
    """
    tomorrow = (dt.date.fromisoformat(today) + dt.timedelta(days=1)).isoformat()
    return tomorrow if is_trading_day(tomorrow) else None


def offset(d: str, n: int) -> str:
    """d 之后第 n 个交易日（d 本身不计）。"""
    dates = trade_dates()
    return dates[bisect.bisect_right(dates, d) + n - 1]


def count_between(start: str, end: str) -> int:
    """start 之后到 end（含）之间的交易日数。start 当天买入、end 当天，即持有了这么多个交易日。"""
    dates = trade_dates()
    return bisect.bisect_right(dates, end) - bisect.bisect_right(dates, start)
