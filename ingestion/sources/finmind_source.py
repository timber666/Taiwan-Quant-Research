"""
FinMind 資料來源實作。

還原股價（adj_close）不是直接跟 FinMind 拿的——免費會員拿不到
taiwan_stock_daily_adj（見 docs/data_source.md 已知限制），這裡改成抓
taiwan_stock_daily（原始價）+ taiwan_stock_dividend（除權息公告），
自己用 ingestion/adjustment.py 算還原價。
"""

import time

import pandas as pd
from FinMind.data import DataLoader

from .base import PriceSource
from ..adjustment import calculate_adjusted_price

# 呼叫之間固定小睡一下，降低打到免費額度上限的機率
CALL_DELAY_SECONDS = 0.3
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2


def _call_with_retry(func, *args, **kwargs):
    """呼叫 FinMind API，失敗時重試（指數退避），但方案限制類的錯誤不重試。"""
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            result = func(*args, **kwargs)
            time.sleep(CALL_DELAY_SECONDS)
            return result
        except Exception as e:
            last_err = e
            # 免費會員權限不足（例如呼叫 taiwan_stock_daily_adj）重試沒有意義，直接往外丟
            if "Your level is" in str(e):
                raise
            time.sleep(BACKOFF_BASE_SECONDS ** attempt)
    raise last_err


class FinMindSource(PriceSource):
    def __init__(self, api_token: str):
        self.dl = DataLoader()
        self.dl.login_by_token(api_token=api_token)

    def fetch_stock_list(self) -> pd.DataFrame:
        info = _call_with_retry(self.dl.taiwan_stock_info)

        info = info[info["type"].isin(["twse", "tpex"])]
        info = info[info["stock_id"].str.match(r"^\d{4}$")]      # 排除權證/特別股等代碼較長或含字母的
        info = info[info["industry_category"] != "ETF"]
        # 同一檔股票在 FinMind 可能有多筆紀錄：
        # (1) 廣義 + 細分類產業各一筆（同一天）
        # (2) 轉上市/轉上櫃的股票會留一筆舊的 market 紀錄（例如 6446 藥華藥
        #     從 tpex 轉 twse，舊的 tpex 那筆 date 比較舊）
        # 沒有先照 date 排序就去重的話，keep="first" 撿到的可能是過期資料，
        # 所以要先由新到舊排序，確保留下來的是最新狀態
        info = info.sort_values("date", ascending=False)
        info = info.drop_duplicates(subset="stock_id", keep="first")

        info = info.rename(columns={
            "stock_name": "name",
            "type": "market",
            "industry_category": "industry",
        })
        # FinMind 的 date 欄位是「資料更新日」不是「上市日」，沒有可靠的上市日來源，先留空
        info["listing_date"] = None

        return info[["stock_id", "name", "market", "industry", "listing_date"]].reset_index(drop=True)

    def fetch_dividends(self, stock_id: str, start: str, end: str) -> pd.DataFrame:
        columns = ["ex_date", "cash_div", "stock_div", "reference_price"]

        div_raw = _call_with_retry(
            self.dl.taiwan_stock_dividend, stock_id=stock_id, start_date=start, end_date=end
        )
        if div_raw.empty:
            return pd.DataFrame(columns=columns)

        div_raw = div_raw.copy()
        div_raw["ex_date"] = div_raw["CashExDividendTradingDate"].where(
            div_raw["CashExDividendTradingDate"] != "", div_raw["StockExDividendTradingDate"]
        )
        div_raw = div_raw[div_raw["ex_date"] != ""]
        if div_raw.empty:
            return pd.DataFrame(columns=columns)

        div = div_raw[["ex_date", "CashEarningsDistribution", "StockEarningsDistribution"]].rename(columns={
            "CashEarningsDistribution": "cash_div",
            "StockEarningsDistribution": "stock_div",
        })

        # 官方參考價只用來交叉驗證，拿不到也不影響還原價計算，所以失敗時不擋主流程
        result = _call_with_retry(
            self.dl.taiwan_stock_dividend_result, stock_id=stock_id, start_date=start, end_date=end
        )
        if not result.empty:
            ref = result[["date", "reference_price"]].rename(columns={"date": "ex_date"})
            div = div.merge(ref, on="ex_date", how="left")
        else:
            div["reference_price"] = None

        return div[columns].reset_index(drop=True)

    def fetch_prices(self, stock_id: str, start: str, end: str) -> pd.DataFrame:
        columns = ["date", "open", "high", "low", "close", "adj_close", "volume", "turnover"]

        raw = _call_with_retry(
            self.dl.taiwan_stock_daily, stock_id=stock_id, start_date=start, end_date=end
        )
        if raw.empty:
            return pd.DataFrame(columns=columns)

        raw = raw.rename(columns={
            "max": "high",
            "min": "low",
            "Trading_Volume": "volume",
            "Trading_money": "turnover",
        })[["date", "open", "high", "low", "close", "volume", "turnover"]]

        div = self.fetch_dividends(stock_id, start, end)
        if div.empty:
            raw["adj_close"] = raw["close"]
            return raw[columns].reset_index(drop=True)

        div_for_adjust = div.rename(columns={"ex_date": "date"})
        adjusted = calculate_adjusted_price(raw, div_for_adjust)
        adjusted["date"] = adjusted["date"].dt.strftime("%Y-%m-%d")

        return adjusted[columns].reset_index(drop=True)
