"""
資料來源抽象介面。

只定義「任何資料來源都要提供什麼」，不包含任何實際呼叫 API 的邏輯。
downloader.py / cleaner.py 之後只跟這個介面打交道，不直接依賴 FinMind 或
TWSE 的細節，換來源或加備援時不需要改動它們。
"""

from abc import ABC, abstractmethod

import pandas as pd


class PriceSource(ABC):
    """所有資料來源（FinMind、TWSE OpenAPI...）都要繼承這個類別並實作以下方法。"""

    @abstractmethod
    def fetch_stock_list(self) -> pd.DataFrame:
        """抓取股票基本資料清單。

        回傳欄位：stock_id, name, market, industry, listing_date
        對齊 database/schema.sql 的 stocks 表。
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_prices(self, stock_id: str, start: str, end: str) -> pd.DataFrame:
        """抓取單一股票在區間內的日線股價，包含還原收盤價。

        回傳欄位：date, open, high, low, close, adj_close, volume, turnover
        對齊 database/schema.sql 的 prices 表（不含 stock_id/source/ingested_at，
        這些由呼叫端補上）。

        adj_close 不一定是資料源直接提供的（例如 FinMind 免費會員無法取得
        taiwan_stock_daily_adj），實作內部可以自己用 fetch_dividends() 的結果
        計算還原價，只要回傳的 DataFrame 有這個欄位即可。
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_dividends(self, stock_id: str, start: str, end: str) -> pd.DataFrame:
        """抓取單一股票在區間內的除權息事件。

        回傳欄位：ex_date, cash_div, stock_div, reference_price
        對齊 database/schema.sql 的 dividend_events 表（不含 stock_id/source/
        ingested_at，這些由呼叫端補上）。

        - cash_div：現金股利（每股）
        - stock_div：股票股利（每 10 股配股數，不是每股配股數）
        - reference_price：官方除權息參考價，沒有的話可以填 None，
          僅用來交叉驗證還原價計算是否正確
        """
        raise NotImplementedError
