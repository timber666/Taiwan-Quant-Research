"""
FinMindSource 的單元測試。

這些測試完全不連網：用 FakeDataLoader 假裝自己是 FinMind 的 DataLoader，
回傳事先寫死的 DataFrame。好處是跑得快、結果穩定、不消耗 API 額度，
而且可以刻意製造真實世界很難重現的邊界情況。

主要守住兩個已經踩過的坑（詳見 docs/data_source.md）：
1. 股利分成「盈餘配發」與「公積配發」兩欄，只取盈餘那欄會少算，
   遇到整筆都配在公積的股票（例如 2409 友達）甚至完全不還原。
2. FinMind 股利 API 的日期過濾用的是「權利分派基準日」，比除權息交易日
   晚 6〜10 天，直接拿 end 去查會漏掉區間尾端的事件（例如 6669 緯穎）。
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from ingestion.sources import finmind_source
from ingestion.sources.finmind_source import FinMindSource

DIVIDEND_COLUMNS = [
    "date", "stock_id",
    "CashExDividendTradingDate", "StockExDividendTradingDate",
    "CashEarningsDistribution", "CashStatutorySurplus",
    "StockEarningsDistribution", "StockStatutorySurplus",
]


def dividend_row(record_date, cash_ex_date="", stock_ex_date="",
                 cash_earnings=0.0, cash_surplus=0.0,
                 stock_earnings=0.0, stock_surplus=0.0):
    """組一列 FinMind 格式的股利公告。

    record_date 是「權利分派基準日」（FinMind 的 date 欄位），
    cash_ex_date / stock_ex_date 才是除息 / 除權「交易日」——這兩者不同，
    正是坑 2 的來源。
    """
    return {
        "date": record_date,
        "stock_id": "0000",
        "CashExDividendTradingDate": cash_ex_date,
        "StockExDividendTradingDate": stock_ex_date,
        "CashEarningsDistribution": cash_earnings,
        "CashStatutorySurplus": cash_surplus,
        "StockEarningsDistribution": stock_earnings,
        "StockStatutorySurplus": stock_surplus,
    }


def price_row(date, close, volume=1000):
    """組一列 FinMind 格式的日線（注意 FinMind 把最高/最低價叫 max/min）"""
    return {
        "date": date, "stock_id": "0000",
        "open": close, "max": close, "min": close, "close": close,
        "Trading_Volume": volume, "Trading_money": close * volume,
    }


class FakeDataLoader:
    """假的 FinMind DataLoader。

    這個假物件刻意模仿 FinMind 真實的行為——尤其是 `taiwan_stock_dividend`
    的日期過濾是套在「基準日」而不是「除權息交易日」上。如果這裡偷懶改成
    用交易日過濾，坑 2 的測試就會連壞掉的程式碼也一起放行，測試就沒意義了。
    """

    def __init__(self, dividends=None, results=None, prices=None):
        self.dividends = pd.DataFrame(columns=DIVIDEND_COLUMNS) if dividends is None else dividends
        self.results = pd.DataFrame(columns=["date", "reference_price"]) if results is None else results
        self.prices = pd.DataFrame() if prices is None else prices
        self.calls = []   # 記錄每次呼叫的參數，用來驗證「有沒有放寬 end」

    def login_by_token(self, api_token=None):
        pass

    def _filter(self, df, column, start_date, end_date):
        if df.empty:
            return df.copy()
        return df[(df[column] >= start_date) & (df[column] <= end_date)].copy()

    def taiwan_stock_dividend(self, stock_id, start_date, end_date):
        self.calls.append({"api": "dividend", "start_date": start_date, "end_date": end_date})
        # 真實 FinMind 就是用 date（基準日）過濾的，不是用除權息交易日
        return self._filter(self.dividends, "date", start_date, end_date)

    def taiwan_stock_dividend_result(self, stock_id, start_date, end_date):
        self.calls.append({"api": "result", "start_date": start_date, "end_date": end_date})
        return self._filter(self.results, "date", start_date, end_date)

    def taiwan_stock_daily(self, stock_id, start_date, end_date):
        self.calls.append({"api": "daily", "start_date": start_date, "end_date": end_date})
        return self._filter(self.prices, "date", start_date, end_date)


@pytest.fixture
def make_source(monkeypatch):
    """回傳一個工廠函式，用它產生「接上假 DataLoader 的 FinMindSource」。"""
    monkeypatch.setattr(finmind_source, "CALL_DELAY_SECONDS", 0)   # 測試不用真的睡 0.3 秒

    def _make(dividends=None, results=None, prices=None):
        fake = FakeDataLoader(dividends, results, prices)
        monkeypatch.setattr(finmind_source, "DataLoader", lambda *a, **k: fake)
        return FinMindSource("fake-token"), fake

    return _make


# ---------------------------------------------------------------------------
# 坑 1：股利要把「盈餘配發」與「公積配發」相加
# ---------------------------------------------------------------------------

def test_cash_dividend_sums_earnings_and_statutory_surplus(make_source):
    """2454 聯發科 2020-07-06：盈餘 5.00 + 公積 5.50，舊版少算一半"""
    divs = pd.DataFrame([dividend_row("2020-07-16", cash_ex_date="2020-07-06",
                                      cash_earnings=5.000409, cash_surplus=5.500450)])
    source, _ = make_source(dividends=divs)

    out = source.fetch_dividends("2454", "2020-01-01", "2020-12-31")

    assert out.loc[0, "cash_div"] == pytest.approx(10.500859)


def test_dividend_paid_entirely_from_statutory_surplus_is_not_lost(make_source):
    """2409 友達 2023-08-10：盈餘為 0、全部配在公積，舊版會算成完全沒配息"""
    divs = pd.DataFrame([dividend_row("2023-08-16", cash_ex_date="2023-08-10",
                                      cash_earnings=0.0, cash_surplus=0.80)])
    source, _ = make_source(dividends=divs)

    out = source.fetch_dividends("2409", "2023-01-01", "2023-12-31")

    assert len(out) == 1
    assert out.loc[0, "cash_div"] == pytest.approx(0.80)


def test_stock_dividend_sums_earnings_and_statutory_surplus(make_source):
    """股票股利同樣分兩欄，也要相加"""
    divs = pd.DataFrame([dividend_row("2021-08-15", stock_ex_date="2021-08-09",
                                      stock_earnings=1.5, stock_surplus=0.5)])
    source, _ = make_source(dividends=divs)

    out = source.fetch_dividends("2027", "2021-01-01", "2021-12-31")

    assert out.loc[0, "stock_div"] == pytest.approx(2.0)


def test_nan_in_one_component_does_not_wipe_out_the_other(make_source):
    """NaN + 5.0 在 pandas 裡是 NaN，沒有先 fillna(0) 會把算得出來的股利弄丟"""
    divs = pd.DataFrame([dividend_row("2023-07-10", cash_ex_date="2023-07-04",
                                      cash_earnings=5.0, cash_surplus=float("nan"))])
    source, _ = make_source(dividends=divs)

    out = source.fetch_dividends("2317", "2023-01-01", "2023-12-31")

    assert out.loc[0, "cash_div"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 坑 2：股利 API 用「基準日」過濾，比除權息交易日晚 6〜10 天
# ---------------------------------------------------------------------------

def test_event_is_fetched_even_when_record_date_falls_after_end(make_source):
    """6669 緯穎：除權交易日 09-02 在區間內，但基準日 09-08 在 end 之後。

    這是坑 2 的回歸測試——舊版直接拿 end 去查會查不到這筆，
    於是 7800 → 2610 的除權跳空完全沒被還原。
    """
    divs = pd.DataFrame([dividend_row("2026-09-08", stock_ex_date="2026-09-02",
                                      stock_earnings=19.827946)])
    source, _ = make_source(dividends=divs)

    out = source.fetch_dividends("6669", "2026-08-01", "2026-09-05")

    assert len(out) == 1
    assert out.loc[0, "ex_date"] == "2026-09-02"


def test_events_outside_requested_range_are_filtered_out(make_source):
    """放寬 end 是為了不漏抓，不是為了多回傳——多抓到的要自己濾掉。

    第二筆的基準日 04-16 落在放寬後的查詢區間內，但除息日 04-10 已經超出
    呼叫端要的 [start, end]，不該回傳（否則之後會被寫進 dividend_events 表）。
    """
    divs = pd.DataFrame([
        dividend_row("2023-03-22", cash_ex_date="2023-03-16", cash_earnings=2.75),
        dividend_row("2023-04-16", cash_ex_date="2023-04-10", cash_earnings=3.00),
    ])
    source, _ = make_source(dividends=divs)

    out = source.fetch_dividends("2330", "2023-01-01", "2023-03-31")

    assert list(out["ex_date"]) == ["2023-03-16"]


def test_dividend_query_widens_end_but_leaves_start_alone(make_source):
    """基準日一定晚於除權息交易日（實測 464 筆事件沒有例外），所以只放寬 end"""
    source, fake = make_source()

    source.fetch_dividends("2330", "2023-01-01", "2023-03-31")

    call = fake.calls[0]
    assert call["api"] == "dividend"
    assert call["start_date"] == "2023-01-01"
    assert call["end_date"] > "2023-03-31"


# ---------------------------------------------------------------------------
# 既有行為：不要在修坑的時候改壞
# ---------------------------------------------------------------------------

def test_pure_stock_dividend_falls_back_to_stock_ex_date(make_source):
    """純除權（沒有配現金）的公司，除息交易日是空字串，要改抓除權交易日"""
    divs = pd.DataFrame([dividend_row("2018-07-15", cash_ex_date="", stock_ex_date="2018-07-09",
                                      stock_earnings=1.981167)])
    source, _ = make_source(dividends=divs)

    out = source.fetch_dividends("2327", "2018-01-01", "2018-12-31")

    assert out.loc[0, "ex_date"] == "2018-07-09"


def test_announced_dividend_without_ex_date_is_skipped(make_source):
    """已公告但還沒定除權息日的，兩個日期都是空字串，對不到任何一天股價，要跳過"""
    divs = pd.DataFrame([dividend_row("2026-05-20", cash_earnings=3.0)])
    source, _ = make_source(dividends=divs)

    out = source.fetch_dividends("2330", "2026-01-01", "2026-12-31")

    assert out.empty


def test_returns_empty_frame_with_expected_columns_when_no_dividend(make_source):
    """沒有股利時要回傳「欄位正確但沒有列」的 DataFrame，不是 None。

    呼叫端才能無腦寫 df["cash_div"] 而不用先檢查 None。
    """
    source, _ = make_source()

    out = source.fetch_dividends("2330", "2023-01-01", "2023-12-31")

    assert out.empty
    assert list(out.columns) == ["ex_date", "cash_div", "stock_div", "reference_price"]


def test_official_reference_price_is_attached_for_cross_checking(make_source):
    """官方參考價不參與計算，但要帶回來供事後稽核"""
    divs = pd.DataFrame([dividend_row("2023-03-22", cash_ex_date="2023-03-16", cash_earnings=2.75)])
    results = pd.DataFrame([{"date": "2023-03-16", "reference_price": 508.25}])
    source, _ = make_source(dividends=divs, results=results)

    out = source.fetch_dividends("2330", "2023-01-01", "2023-12-31")

    assert out.loc[0, "reference_price"] == pytest.approx(508.25)


# ---------------------------------------------------------------------------
# 端到端：fetch_prices 有沒有真的把還原價算對
# ---------------------------------------------------------------------------

def test_fetch_prices_adjusts_close_using_total_dividend(make_source):
    """串起 fetch_dividends + calculate_adjusted_price，用 2409 友達的真實數字。

    除息日 2023-08-10、現金股利 0.80（全部來自公積）：
      前一日還原價 = (20.40 - 0.80) / 1 = 19.60（= 官方 reference_price）
      除息日當天不打折，還原價維持 18.95
    """
    prices = pd.DataFrame([
        price_row("2023-08-08", 20.40),
        price_row("2023-08-09", 20.40),
        price_row("2023-08-10", 18.95),
        price_row("2023-08-11", 18.50),
    ])
    divs = pd.DataFrame([dividend_row("2023-08-16", cash_ex_date="2023-08-10",
                                      cash_earnings=0.0, cash_surplus=0.80)])
    source, _ = make_source(dividends=divs, prices=prices)

    out = source.fetch_prices("2409", "2023-08-01", "2023-08-31")
    adj = out.set_index("date")["adj_close"]

    assert adj["2023-08-09"] == pytest.approx(19.60)
    assert adj["2023-08-10"] == pytest.approx(18.95)
    assert adj["2023-08-11"] == pytest.approx(18.50)
    assert list(out.columns) == [
        "date", "open", "high", "low", "close", "adj_close", "volume", "turnover"
    ]
