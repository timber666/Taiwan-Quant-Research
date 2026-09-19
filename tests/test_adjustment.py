"""
calculate_adjusted_price 的單元測試。

還原價原本是在 FinMindSource.fetch_prices 裡算的，所以測試也掛在
tests/test_finmind_source.py。階段 D 步驟 16 把還原計算從資料來源層拿掉之後
（理由見 database/schema.sql 裡 prices 的註解），這個函式跟 FinMind 已經完全無關，
測試就跟著搬到這裡，直接對 calculate_adjusted_price 本身驗證。
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from ingestion.adjustment import calculate_adjusted_price


def price_frame(rows):
    """rows 是 (date, close) 的序列；OHLC 都用同一個價格，測試只關心 close。"""
    return pd.DataFrame([
        {"date": d, "open": c, "high": c, "low": c, "close": c} for d, c in rows
    ])


def adjusted_close(df_price, df_div):
    out = calculate_adjusted_price(df_price, df_div)
    return out.assign(date=out["date"].dt.strftime("%Y-%m-%d")).set_index("date")["adj_close"]


def test_adjusts_history_using_total_dividend():
    """2409 友達 2023-08-10 除息 0.80（全部配在公積）的真實數字。

    前一日還原價 = (20.40 - 0.80) / 1 = 19.60，跟官方 reference_price 一致；
    除息日當天不打折，維持 18.95。
    """
    prices = price_frame([
        ("2023-08-08", 20.40),
        ("2023-08-09", 20.40),
        ("2023-08-10", 18.95),
        ("2023-08-11", 18.50),
    ])
    divs = pd.DataFrame([{"date": "2023-08-10", "cash_div": 0.80, "stock_div": 0.0}])

    adj = adjusted_close(prices, divs)

    assert adj["2023-08-08"] == pytest.approx(19.60)
    assert adj["2023-08-09"] == pytest.approx(19.60)
    assert adj["2023-08-10"] == pytest.approx(18.95)
    assert adj["2023-08-11"] == pytest.approx(18.50)
