"""
downloader.py 的單元測試。

一樣不連網：用 FakeSource 假裝自己是 PriceSource。這裡測的是「排程決策」
——抓誰、抓哪段、抓失敗怎麼辦——不是 FinMind 的資料格式。
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from datetime import date, timedelta

import pandas as pd
import pytest

from config.settings import BASE_DIR, HISTORY_START_DATE
from database.connection import get_connection
from ingestion.downloader import sync_stock_list, plan_downloads, download_all
from ingestion.sources.base import PriceSource

PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume", "turnover"]


class FakeSource(PriceSource):
    """假的資料來源：股票清單和股價都由測試指定，fail_for 裡的股票會丟例外。"""

    def __init__(self, stock_ids=(), prices=None, fail_for=None):
        self._stock_ids = list(stock_ids)
        self._prices = prices or {}
        self._fail_for = fail_for or {}
        self.fetched = []          # 記錄實際打了哪些股票，用來驗證「有沒有白打 API」

    def fetch_stock_list(self):
        return pd.DataFrame([
            {"stock_id": sid, "name": f"測試{sid}", "market": "twse",
             "industry": "測試業", "listing_date": None}
            for sid in self._stock_ids
        ], columns=["stock_id", "name", "market", "industry", "listing_date"])

    def fetch_prices(self, stock_id, start, end):
        self.fetched.append(stock_id)
        if stock_id in self._fail_for:
            raise RuntimeError(self._fail_for[stock_id])
        return self._prices.get(stock_id, pd.DataFrame(columns=PRICE_COLUMNS))

    def fetch_dividends(self, stock_id, start, end):
        return pd.DataFrame(columns=["ex_date", "cash_div", "stock_div", "reference_price"])


def price_frame(dates):
    return pd.DataFrame([
        {"date": d, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
         "volume": 1000, "turnover": 10000.0}
        for d in dates
    ], columns=PRICE_COLUMNS)


@pytest.fixture
def conn(tmp_path):
    """每個測試一個乾淨的暫存資料庫（順便驗證 get_connection 可以指定路徑）"""
    c = get_connection(tmp_path / "test.db")
    c.executescript((BASE_DIR / "database" / "schema.sql").read_text(encoding="utf-8"))
    yield c
    c.close()


# ---------------------------------------------------------------------------
# sync_stock_list
# ---------------------------------------------------------------------------

def test_sync_writes_only_universe_members(conn):
    """來源回傳一堆股票，只有 universe 裡的該進 stocks 表"""
    source = FakeSource(stock_ids=["1111", "2222", "3333"])

    result = sync_stock_list(source, conn, ["1111", "2222"])

    assert result.synced == 2
    stored = {row[0] for row in conn.execute("SELECT stock_id FROM stocks")}
    assert stored == {"1111", "2222"}


def test_sync_marks_stock_missing_from_source_as_inactive(conn):
    """universe 裡的股票從來源清單消失（下市）時，要標記 is_active = 0。

    不標記的話分不出「還在市場上」跟「已經下市」，回測會有存活者偏誤。
    """
    universe = ["1111", "2222"]
    sync_stock_list(FakeSource(stock_ids=["1111", "2222"]), conn, universe)

    result = sync_stock_list(FakeSource(stock_ids=["1111"]), conn, universe)

    assert result.deactivated == ["2222"]
    assert conn.execute("SELECT is_active FROM stocks WHERE stock_id='2222'").fetchone()[0] == 0
    assert conn.execute("SELECT is_active FROM stocks WHERE stock_id='1111'").fetchone()[0] == 1


def test_sync_reports_deactivation_only_once(conn):
    """重跑時不該每次都宣稱同一檔又下市了一次——回傳的是狀態變化，不是現況清單"""
    universe = ["1111", "2222"]
    sync_stock_list(FakeSource(stock_ids=["1111", "2222"]), conn, universe)
    sync_stock_list(FakeSource(stock_ids=["1111"]), conn, universe)

    result = sync_stock_list(FakeSource(stock_ids=["1111"]), conn, universe)

    assert result.deactivated == []
    assert conn.execute("SELECT is_active FROM stocks WHERE stock_id='2222'").fetchone()[0] == 0


def test_sync_reactivates_stock_that_comes_back(conn):
    """暫停交易後恢復的股票，重新出現在來源清單時要變回 is_active = 1"""
    universe = ["1111"]
    sync_stock_list(FakeSource(stock_ids=["1111"]), conn, universe)
    sync_stock_list(FakeSource(stock_ids=[]), conn, universe)

    sync_stock_list(FakeSource(stock_ids=["1111"]), conn, universe)

    assert conn.execute("SELECT is_active FROM stocks WHERE stock_id='1111'").fetchone()[0] == 1


def test_sync_is_idempotent(conn):
    """同一份清單同步兩次，stocks 表不會多出重複的列"""
    source = FakeSource(stock_ids=["1111", "2222"])
    sync_stock_list(source, conn, ["1111", "2222"])

    sync_stock_list(source, conn, ["1111", "2222"])

    assert conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# plan_downloads
# ---------------------------------------------------------------------------

def test_plan_first_run_starts_from_history_start_date(conn):
    """ingest_log 沒有紀錄 = 第一次抓這檔，要從 HISTORY_START_DATE 開始"""
    plan = plan_downloads(conn, ["1111"])

    start, end = plan["1111"]
    assert start == HISTORY_START_DATE
    assert end == date.today().isoformat()


def test_plan_incremental_starts_the_day_after_last_date(conn):
    """有紀錄就只抓新的部分，而且要 +1 天，不然會重抓最後一天白花額度"""
    conn.execute("INSERT INTO stocks (stock_id, name) VALUES ('1111', '測試')")
    conn.execute("INSERT INTO ingest_log (stock_id, last_date) VALUES ('1111', '2024-03-15')")

    plan = plan_downloads(conn, ["1111"])

    assert plan["1111"][0] == "2024-03-16"


def test_plan_produces_invalid_range_when_already_up_to_date(conn):
    """今天已經更新過的話，start 會是明天、比 end 晚。

    plan_downloads 的 docstring 有交代這個契約：它不自己過濾，由 download_all 處理。
    """
    conn.execute("INSERT INTO stocks (stock_id, name) VALUES ('1111', '測試')")
    conn.execute("INSERT INTO ingest_log (stock_id, last_date) VALUES ('1111', ?)",
                 (date.today().isoformat(),))

    start, end = plan_downloads(conn, ["1111"])["1111"]

    assert start > end


def test_plan_returns_empty_for_empty_universe(conn):
    assert plan_downloads(conn, []) == {}


# ---------------------------------------------------------------------------
# download_all
# ---------------------------------------------------------------------------

def test_download_collects_successful_frames():
    source = FakeSource(prices={"1111": price_frame(["2024-03-18", "2024-03-19"])})

    result = download_all(source, {"1111": ("2024-03-18", "2024-03-19")})

    assert list(result.data) == ["1111"]
    assert len(result.data["1111"]) == 2
    assert result.empty == [] and result.skipped == [] and result.failed == {}


def test_download_separates_empty_result_from_success():
    """抓回來 0 列不該算「成功」——混在一起會讓摘要顯示抓到了但其實沒有"""
    source = FakeSource(prices={"1111": price_frame(["2024-03-18"])})

    result = download_all(source, {
        "1111": ("2024-03-18", "2024-03-18"),
        "2222": ("2024-03-18", "2024-03-18"),   # FakeSource 沒給價格 -> 空 DataFrame
    })

    assert list(result.data) == ["1111"]
    assert result.empty == ["2222"]


def test_download_records_failures_instead_of_crashing():
    """單檔失敗要記下錯誤訊息並繼續，不能讓整批下載中斷"""
    source = FakeSource(
        prices={"1111": price_frame(["2024-03-18"]), "3333": price_frame(["2024-03-18"])},
        fail_for={"2222": "API rate limit"},
    )

    result = download_all(source, {
        "1111": ("2024-03-18", "2024-03-18"),
        "2222": ("2024-03-18", "2024-03-18"),
        "3333": ("2024-03-18", "2024-03-18"),
    })

    assert set(result.data) == {"1111", "3333"}          # 失敗不影響前後的股票
    assert result.failed == {"2222": "API rate limit"}


def test_download_skips_invalid_range_without_calling_api():
    """start > end 代表沒有新資料，連 API 都不該打"""
    source = FakeSource()

    result = download_all(source, {"1111": ("2024-03-20", "2024-03-19")})

    assert result.skipped == ["1111"]
    assert source.fetched == []


def test_download_skips_weekend_only_range_without_calling_api():
    """整段都是週末（2026-09-19 六、2026-09-20 日）不可能有股價，不要白打 API"""
    source = FakeSource()

    result = download_all(source, {"1111": ("2026-09-19", "2026-09-20")})

    assert result.skipped == ["1111"]
    assert source.fetched == []


def test_download_does_not_skip_range_containing_a_weekday():
    """區間只要包含一個平日就要照抓（2026-09-18 是週五）"""
    source = FakeSource(prices={"1111": price_frame(["2026-09-18"])})

    result = download_all(source, {"1111": ("2026-09-18", "2026-09-20")})

    assert source.fetched == ["1111"]
    assert list(result.data) == ["1111"]


def test_download_does_not_skip_long_range():
    """跨滿一週以上的區間一定含平日，不逐日檢查也要判定為要抓"""
    source = FakeSource(prices={"1111": price_frame(["2024-03-18"])})
    start = date(2024, 3, 1)

    result = download_all(source, {"1111": (start.isoformat(),
                                            (start + timedelta(days=30)).isoformat())})

    assert source.fetched == ["1111"]
    assert list(result.data) == ["1111"]
