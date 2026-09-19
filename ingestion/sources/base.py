"""
資料來源抽象介面。

只定義「任何資料來源都要提供什麼」，不包含任何實際呼叫 API 的邏輯。
downloader.py / cleaner.py 之後只跟這個介面打交道，不直接依賴 FinMind 或
TWSE 的細節，換來源或加備援時不需要改動它們。
"""

from abc import ABC, abstractmethod

import pandas as pd


class RateLimitError(Exception):
    """資料來源的呼叫額度用完了。

    刻意獨立成一個型別，而不是混在一般的 Exception 裡，因為它的正確處置方式
    跟其他錯誤完全不同：單一檔股票抓失敗可以跳過、繼續抓下一檔；但額度用完之後
    「下一檔」也一定會失敗，繼續跑只是把同一則錯誤訊息複製 N 份，還白白吃掉
    重試的等待時間。上層（run_ingest）看到這個型別應該直接中止整批，
    把已經抓到的部分收好，等額度回復再跑增量更新。
    """


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
        """抓取單一股票在區間內的**原始**日線股價，依日期升冪排序。

        回傳欄位：date, open, high, low, close, volume, turnover
        對齊 database/schema.sql 的 prices 表（不含 stock_id/source/ingested_at，
        這些由呼叫端補上）。

        **不要回傳還原價。** 還原價是後還原，調整因子取決於「這一天之後發生過哪些
        除權息」，而資料來源一次只看得到被查詢的那個區間——增量更新時區間只有幾天，
        算出來的還原價是錯的（詳見 database/schema.sql 裡 prices 的註解）。
        資料來源的職責只到「如實回報那幾天市場上的原始成交價」為止；
        還原價由 database/data_access.py 在讀取時，用 dividend_events 的完整事件
        歷史重新計算。
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
