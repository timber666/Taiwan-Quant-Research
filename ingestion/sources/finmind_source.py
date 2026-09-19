"""
FinMind 資料來源實作。

這裡只負責「如實回報 FinMind 給了什麼」：taiwan_stock_daily 出原始日線、
taiwan_stock_dividend 出除權息事件，兩者各自獨立回傳，不在這一層做還原計算。

（免費會員本來就拿不到 taiwan_stock_daily_adj，見 docs/data_source.md 的已知限制；
但就算拿得到，在這裡算還原價也是錯的——資料來源一次只看得到被查詢的那個區間，
增量更新時算出來的還原價會失真。還原改由 database/data_access.py 在讀取時，
拿 dividend_events 的完整事件歷史重算，詳見 database/schema.sql 裡 prices 的註解。）
"""

import time
from datetime import date, timedelta

import pandas as pd
from FinMind.data import DataLoader

from .base import PriceSource, RateLimitError

# 呼叫之間固定小睡一下，降低打到免費額度上限的機率
CALL_DELAY_SECONDS = 0.3
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2

# 查股利時要把 end 往後放寬幾天（理由見 fetch_dividends 的註解）。
# 實測 SAMPLE_UNIVERSE 2015 年至今 464 筆事件，「基準日 − 除權息交易日」
# 最小 6 天、中位數 6 天、最大 10 天，且沒有任何一筆是負的（基準日一定晚於
# 交易日），所以只需要放寬 end、不用動 start。取 30 天約為實測最大值的 3 倍，
# 放寬不會增加 API 呼叫次數（同樣是一次查詢），寧可多抓再自己濾掉。
DIVIDEND_LOOKAHEAD_DAYS = 30


# 重試救不了的兩類錯誤，靠比對錯誤訊息辨識。
# FinMind 套件本身沒有定義例外型別，也沒有把 HTTP status code 帶出來——它是在
# FinMind/data/finmind_api.py 的 _extract_data() 裡把伺服器回的 msg 包成一般
# Exception 丟出來，所以只能用字串比對。下面的關鍵字是依這條路徑推測的，
# 沒辦法在不真的打爆額度的情況下驗證，之後若看到漏接的訊息就往清單裡加。
PERMISSION_MARKERS = ("your level is",)          # 免費會員權限不足（已實測，見 docs/data_source.md）
RATE_LIMIT_MARKERS = ("upper limit", "too many requests", "rate limit", "request limit")


def _call_with_retry(func, *args, **kwargs):
    """呼叫 FinMind API，失敗時重試（指數退避）。

    兩類錯誤不重試，因為重試救不了、只是白等：
    - 權限不足（免費會員呼叫付費資料集）：重試幾次都一樣會被拒絕。
    - 額度用完：FinMind 免費會員是**每小時** 600 次，1→2→4 秒的退避對一個
      以小時為單位的額度毫無意義，等於假裝重試過。改成丟 RateLimitError，
      讓上層知道該中止整批而不是繼續抓下一檔（理由見 base.RateLimitError）。
    """
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            result = func(*args, **kwargs)
            time.sleep(CALL_DELAY_SECONDS)
            return result
        except Exception as e:
            last_err = e
            message = str(e).lower()
            if any(m in message for m in PERMISSION_MARKERS):
                raise
            if any(m in message for m in RATE_LIMIT_MARKERS):
                raise RateLimitError(str(e)) from e
            time.sleep(BACKOFF_BASE_SECONDS ** attempt)
    raise last_err


class FinMindSource(PriceSource):
    def __init__(self, api_token: str):
        self.dl = DataLoader()
        self.dl.login_by_token(api_token=api_token)

    def fetch_stock_list(self) -> pd.DataFrame:
        info = _call_with_retry(self.dl.taiwan_stock_info)

        info = info[info["type"].isin(["twse", "tpex"])]         # 排除興櫃
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

        # taiwan_stock_dividend 的 start_date/end_date 過濾的是 date 欄位，而那是
        # 「權利分派基準日」，不是我們拿來跟股價對齊的「除權息交易日」，基準日一定
        # 比交易日晚（實測 6〜10 天）。直接拿 end 去查，會漏掉「除權息日在 end 之前、
        # 但基準日落在 end 之後」的事件——實測 6669 緯穎除權日 2026-09-02、基準日
        # 2026-09-08，用 end=2026-09-05 查會查不到，於是 7800 → 2610 這個 66% 的
        # 除權跳空完全沒被還原。增量更新每次的區間都很短、邊界特別多，更容易中招。
        # 解法：查詢時把 end 往後放寬，抓回來之後再用 ex_date 自己濾回 [start, end]。
        query_end = (date.fromisoformat(end) + timedelta(days=DIVIDEND_LOOKAHEAD_DAYS)).isoformat()

        div_raw = _call_with_retry(
            self.dl.taiwan_stock_dividend, stock_id=stock_id, start_date=start, end_date=query_end
        )
        if div_raw.empty:
            return pd.DataFrame(columns=columns)

        div_raw = div_raw.copy()
        div_raw["ex_date"] = div_raw["CashExDividendTradingDate"].where(
            div_raw["CashExDividendTradingDate"] != "", div_raw["StockExDividendTradingDate"]
        )
        div_raw = div_raw[div_raw["ex_date"] != ""]
        # 濾回呼叫端真正要的區間：放寬 end 是為了不漏抓，不是為了多回傳。日期都是
        # "YYYY-MM-DD" 字串，字典序比較等同時間序，可以直接比大小。
        div_raw = div_raw[(div_raw["ex_date"] >= start) & (div_raw["ex_date"] <= end)]
        if div_raw.empty:
            return pd.DataFrame(columns=columns)

        # 現金股利與股票股利各自都拆成「盈餘配發」與「公積配發」兩個欄位，
        # 證交所的除權息參考價公式用的是兩者「總和」，只取 Earnings 那欄會少算。
        # 實測 SAMPLE_UNIVERSE 2015 年至今 464 筆事件，有 27 筆公積不為零，例如
        # 2454 聯發科 2020-07-06（盈餘 5.00 + 公積 5.50，少算一半）、
        # 2409 友達 2023/2024/2025 三次除息全部只配公積（盈餘為 0，少算 100%）。
        # fillna(0)：兩欄只要有一邊是 NaN，相加就會整個變 NaN，反而把原本算得出來的
        # 股利弄丟，所以相加前先補 0。
        div = pd.DataFrame({
            "ex_date": div_raw["ex_date"],
            "cash_div": (
                div_raw["CashEarningsDistribution"].fillna(0)
                + div_raw["CashStatutorySurplus"].fillna(0)
            ),
            "stock_div": (
                div_raw["StockEarningsDistribution"].fillna(0)
                + div_raw["StockStatutorySurplus"].fillna(0)
            ),
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
        """原始日線，不含還原價（理由見模組 docstring 與 base.PriceSource）。"""
        columns = ["date", "open", "high", "low", "close", "volume", "turnover"]

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
        })
        # FinMind 目前就是照日期升冪回傳，但介面契約寫了「依日期升冪」就自己確保一次，
        # 不要把正確性押在上游不會改行為上。日期是 "YYYY-MM-DD" 字串，字典序等同時間序。
        raw = raw.sort_values("date")

        return raw[columns].reset_index(drop=True)
