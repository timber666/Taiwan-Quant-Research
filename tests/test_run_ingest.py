"""
run_ingest.py 的整合測試。

其他測試各自驗一個模組，這支驗的是「接線對不對」——模組之間的呼叫順序、
交易邊界、以及失敗時的處置。這些都不是單一模組看得出來的問題。

一樣不連網：注入 FakeSource，並把 get_connection 換成暫存資料庫，
不會動到正式的 quant.db。
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from datetime import date, timedelta

import pandas as pd
import pytest

from config.settings import BASE_DIR
from database.connection import get_connection
from ingestion import run_ingest
from ingestion.sources.base import PriceSource, RateLimitError

SCHEMA_SQL = (BASE_DIR / "database" / "schema.sql").read_text(encoding="utf-8")

RAW_PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume", "turnover"]
DIVIDEND_COLUMNS = ["ex_date", "cash_div", "stock_div", "reference_price"]
ACTION_COLUMNS = ["ex_date", "action_type", "before_price", "after_price", "note"]


def prices(*rows):
    """(日期, 收盤) 的序列 -> 資料來源格式的原始日線"""
    return pd.DataFrame(
        [{"date": d, "open": c, "high": c, "low": c, "close": c,
          "volume": 1000, "turnover": c * 1000} for d, c in rows],
        columns=RAW_PRICE_COLUMNS,
    )


def dividends(*rows):
    """(除息日, 現金股利) 的序列"""
    return pd.DataFrame(
        [{"ex_date": d, "cash_div": cash, "stock_div": 0.0, "reference_price": None}
         for d, cash in rows],
        columns=DIVIDEND_COLUMNS,
    )


def actions(*rows):
    """(事件日, 事件前價, 事件後價) 的序列"""
    return pd.DataFrame(
        [{"ex_date": d, "action_type": "capital_reduction",
          "before_price": before, "after_price": after, "note": "test"}
         for d, before, after in rows],
        columns=ACTION_COLUMNS,
    )


class FakeSource(PriceSource):
    """可以指定每檔股票回什麼、或指定某檔要拋什麼例外的假來源。"""

    def __init__(self, stock_ids, price_map=None, dividend_map=None,
                 action_map=None, fail=None):
        self._stock_ids = list(stock_ids)
        self._prices = price_map or {}
        self._dividends = dividend_map or {}
        self._actions = action_map or {}
        self._fail = fail or {}
        self.price_calls = []       # 實際打了哪些股票，用來驗證「有沒有白打 API」

    def fetch_stock_list(self):
        return pd.DataFrame(
            [{"stock_id": s, "name": f"測試{s}", "market": "twse",
              "industry": "測試業", "listing_date": None} for s in self._stock_ids],
            columns=["stock_id", "name", "market", "industry", "listing_date"],
        )

    def fetch_prices(self, stock_id, start, end):
        self.price_calls.append(stock_id)
        if stock_id in self._fail:
            raise self._fail[stock_id]
        return self._prices.get(stock_id, pd.DataFrame(columns=RAW_PRICE_COLUMNS))

    def fetch_dividends(self, stock_id, start, end):
        return self._dividends.get(stock_id, pd.DataFrame(columns=DIVIDEND_COLUMNS))

    def fetch_corporate_actions(self, stock_id, start, end):
        return self._actions.get(stock_id, pd.DataFrame(columns=ACTION_COLUMNS))


@pytest.fixture
def db(tmp_path, monkeypatch):
    """把 run_ingest 的連線導到暫存資料庫，回傳一個「開新連線」的函式供斷言使用。"""
    path = tmp_path / "ingest.db"
    conn = get_connection(path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()

    monkeypatch.setattr(run_ingest, "get_connection", lambda: get_connection(path))
    return lambda: get_connection(path)


def count(open_db, table):
    conn = open_db()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def rows(open_db, sql, params=()):
    conn = open_db()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 整條管線接得起來
# ---------------------------------------------------------------------------

def test_one_run_fills_every_table(db):
    """一次 ingest 要同時寫進 stocks / prices / dividend_events / corporate_actions / ingest_log"""
    source = FakeSource(
        ["1111"],
        price_map={"1111": prices(("2024-03-18", 100.0), ("2024-03-19", 101.0))},
        dividend_map={"1111": dividends(("2024-03-19", 5.0))},
        action_map={"1111": actions(("2024-03-18", 50.0, 55.0))},
    )

    summary = run_ingest.run(only="1111", source=source)

    assert summary.processed == 1
    assert count(db, "stocks") == 1
    assert count(db, "prices") == 2
    assert count(db, "dividend_events") == 1
    assert count(db, "corporate_actions") == 1
    assert count(db, "ingest_log") == 1


def test_ingest_log_matches_what_is_really_in_prices(db):
    """last_date 要等於 prices 裡真的有的最後一天，plan_downloads 才不會跳過沒抓到的資料"""
    source = FakeSource(
        ["1111"],
        price_map={"1111": prices(("2024-03-18", 100.0), ("2024-03-19", 101.0))},
    )

    run_ingest.run(only="1111", source=source)

    assert rows(db, "SELECT last_date, rows_total FROM ingest_log") == [("2024-03-19", 2)]


def test_running_twice_does_not_duplicate_anything(db):
    """整條管線必須冪等——重跑不該讓任何一張表的列數變多"""
    source = FakeSource(
        ["1111"],
        price_map={"1111": prices(("2024-03-18", 100.0), ("2024-03-19", 101.0))},
        dividend_map={"1111": dividends(("2024-03-19", 5.0))},
    )
    run_ingest.run(only="1111", source=source)
    before = [count(db, t) for t in ("prices", "dividend_events", "data_quarantine")]

    run_ingest.run(only="1111", source=source, full=True)

    assert [count(db, t) for t in ("prices", "dividend_events", "data_quarantine")] == before


# ---------------------------------------------------------------------------
# 除權息要先抓，validate 才排除得掉假跳空
# ---------------------------------------------------------------------------

def test_ex_dividend_gap_is_not_quarantined(db):
    """除權息當天的原始價本來就會跳空，不該被記成 price_jump。

    這條驗的是 run_ingest 的呼叫順序：必須先 fetch_dividends 拿到除權息日，
    才能把它當成 ex_dates 傳給 validate。順序寫反的話這個測試會紅。
    """
    source = FakeSource(
        ["1111"],
        price_map={"1111": prices(("2024-03-18", 100.0), ("2024-03-19", 70.0))},
        dividend_map={"1111": dividends(("2024-03-19", 30.0))},
    )

    run_ingest.run(only="1111", source=source)

    assert rows(db, "SELECT COUNT(*) FROM data_quarantine WHERE reason='price_jump'") == [(0,)]
    assert count(db, "prices") == 2


def test_capital_reduction_gap_is_not_quarantined(db):
    """減資造成的跳空同樣不是價格異常，corporate_actions 的日期也要進 ex_dates"""
    source = FakeSource(
        ["1111"],
        price_map={"1111": prices(("2024-03-18", 100.0), ("2024-03-19", 216.0))},
        action_map={"1111": actions(("2024-03-19", 100.0, 216.0))},
    )

    run_ingest.run(only="1111", source=source)

    assert rows(db, "SELECT COUNT(*) FROM data_quarantine WHERE reason='price_jump'") == [(0,)]


def test_unexplained_gap_is_still_quarantined(db):
    """對照組：沒有任何事件可以解釋的大跳空，還是要標記出來給人看"""
    source = FakeSource(
        ["1111"],
        price_map={"1111": prices(("2024-03-18", 100.0), ("2024-03-19", 70.0))},
    )

    run_ingest.run(only="1111", source=source)

    assert rows(db, "SELECT COUNT(*) FROM data_quarantine WHERE reason='price_jump'") == [(1,)]


# ---------------------------------------------------------------------------
# 失敗的處置
# ---------------------------------------------------------------------------

def test_one_stock_failing_does_not_stop_the_others(db, monkeypatch):
    """單檔失敗要記下來並繼續，不能讓整批中斷"""
    monkeypatch.setattr(run_ingest, "SAMPLE_UNIVERSE", ["1111", "2222", "3333"])
    source = FakeSource(
        ["1111", "2222", "3333"],
        price_map={"1111": prices(("2024-03-18", 100.0)),
                   "3333": prices(("2024-03-18", 100.0))},
        fail={"2222": RuntimeError("boom")},
    )

    summary = run_ingest.run(mode="sample", source=source)

    assert summary.processed == 2
    assert "2222" in summary.failed
    assert {r[0] for r in rows(db, "SELECT DISTINCT stock_id FROM prices")} == {"1111", "3333"}


def test_failed_stock_leaves_nothing_half_written(db, monkeypatch):
    """寫到一半才失敗的股票要整檔回退，不能讓殘骸被下一檔的 commit 帶進資料庫。

    這個情境要湊三個條件才重現得出來，少一個都測不到真正的風險：

    1. 失敗必須發生在**已經寫入之後**。除權息是在股價之前寫的，所以這裡讓
       fetch_prices 成功、但回傳缺欄位的資料，等 clean() 拋 ValueError——
       那時 dividend_events 已經寫進去了（還沒 commit）。
    2. 後面必須還有一檔會成功。sqlite3 的交易是連線層級的，沒有 rollback 的話
       殘骸會留在未提交的交易裡，**被下一檔的 conn.commit() 一起提交**。
    3. 只有一檔股票的話，連線關閉時未提交的交易本來就會被丟棄，
       有沒有 rollback 結果都一樣——測了等於沒測。
    """
    monkeypatch.setattr(run_ingest, "SAMPLE_UNIVERSE", ["1111", "2222"])
    malformed = pd.DataFrame([{"date": "2024-03-18", "open": 1.0, "high": 1.0,
                               "low": 1.0, "close": 1.0, "volume": 1}])   # 缺 turnover
    source = FakeSource(
        ["1111", "2222"],
        price_map={"1111": malformed, "2222": prices(("2024-03-18", 100.0))},
        dividend_map={"1111": dividends(("2024-03-19", 5.0))},
    )

    summary = run_ingest.run(mode="sample", source=source)

    assert "1111" in summary.failed
    assert summary.processed == 1                                    # 2222 有跑完
    assert count(db, "prices") == 1                                  # 而且有寫進去
    assert rows(db, "SELECT stock_id FROM dividend_events") == []     # 1111 的殘骸沒被帶進來
    assert rows(db, "SELECT stock_id FROM ingest_log") == [("2222",)]


def test_rate_limit_stops_the_batch_but_keeps_finished_work(db, monkeypatch):
    """額度用完之後每一檔都會失敗，繼續跑只是把同一則錯誤複製 N 份。

    但已經處理完的股票各自 commit 過，必須留著——下次靠 ingest_log 接著跑。
    """
    monkeypatch.setattr(run_ingest, "SAMPLE_UNIVERSE", ["1111", "2222", "3333"])
    source = FakeSource(
        ["1111", "2222", "3333"],
        price_map={"1111": prices(("2024-03-18", 100.0)),
                   "3333": prices(("2024-03-18", 100.0))},
        fail={"2222": RateLimitError("upper limit")},
    )

    summary = run_ingest.run(mode="sample", source=source)

    assert summary.rate_limited == "upper limit"
    assert summary.processed == 1                       # 只有 1111 跑完
    assert source.price_calls == ["1111", "2222"]       # 3333 根本沒被嘗試
    assert count(db, "prices") == 1                     # 1111 的成果留著


def test_rate_limit_makes_the_cli_exit_non_zero(db, monkeypatch):
    """排程要判斷得出「這次沒跑完」"""
    monkeypatch.setattr(run_ingest, "SAMPLE_UNIVERSE", ["1111"])
    source = FakeSource(["1111"], fail={"1111": RateLimitError("upper limit")})
    monkeypatch.setattr(run_ingest, "FinMindSource", lambda token: source)

    assert run_ingest.main(["--mode", "sample"]) == 1


# ---------------------------------------------------------------------------
# 不該打 API 的情況
# ---------------------------------------------------------------------------

def test_delisted_stocks_are_not_fetched(db, monkeypatch):
    """已下市的不會有新資料，每次都去打一次 API 是純浪費"""
    monkeypatch.setattr(run_ingest, "SAMPLE_UNIVERSE", ["1111", "2222"])
    both = FakeSource(["1111", "2222"], price_map={
        "1111": prices(("2024-03-18", 100.0)), "2222": prices(("2024-03-18", 100.0))})
    run_ingest.run(mode="sample", source=both)

    # 2222 從來源清單消失 -> sync_stock_list 會標成 is_active = 0
    gone = FakeSource(["1111"], price_map={"1111": prices(("2024-03-18", 100.0))})
    summary = run_ingest.run(mode="sample", source=gone, full=True)

    assert summary.inactive == ["2222"]
    assert gone.price_calls == ["1111"]


def test_up_to_date_stock_is_skipped_without_calling_the_api(db):
    """ingest_log 已經到今天時，連 API 都不該打"""
    today = date.today().isoformat()
    source = FakeSource(["1111"], price_map={"1111": prices((today, 100.0))})
    run_ingest.run(only="1111", source=source)
    source.price_calls.clear()

    summary = run_ingest.run(only="1111", source=source)

    assert summary.skipped == ["1111"]
    assert source.price_calls == []


def test_full_ignores_ingest_log_and_refetches_history(db):
    """--full 要無視 ingest_log 整段重抓，改了清洗規則才有辦法套用到舊資料"""
    today = date.today().isoformat()
    source = FakeSource(["1111"], price_map={"1111": prices((today, 100.0))})
    run_ingest.run(only="1111", source=source)
    source.price_calls.clear()

    run_ingest.run(only="1111", source=source, full=True)

    assert source.price_calls == ["1111"]      # 沒有被 ingest_log 擋掉


def test_empty_result_is_reported_but_not_a_failure(db):
    """抓回來 0 列不是錯誤，但要在摘要裡跟真的抓到資料區分開"""
    source = FakeSource(["1111"])

    summary = run_ingest.run(only="1111", source=source)

    assert summary.empty == ["1111"]
    assert summary.failed == {}
    assert count(db, "prices") == 0
