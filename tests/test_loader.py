"""
loader.py 的單元測試，重點在**冪等**。

整條管線必須可以重跑：重跑一次不該讓資料變兩份，也不該因為主鍵衝突整批失敗。
所以每張表都有一組「跑兩次，列數不變」的測試。
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import sqlite3

import pandas as pd
import pytest

from config.settings import BASE_DIR
from database.connection import get_connection
from ingestion.cleaner import PRICE_COLUMNS, clean
from ingestion.loader import (
    quarantine, update_ingest_log, upsert_corporate_actions,
    upsert_dividends, upsert_prices,
)
from ingestion.validator import validate


@pytest.fixture
def conn(tmp_path):
    """每個測試一個乾淨的暫存資料庫，並先放一檔股票。

    prices / ingest_log / dividend_events 都有 FOREIGN KEY 指向 stocks，
    而 get_connection 強制開啟外鍵檢查，所以 stocks 一定要先有資料
    ——這正是 run_ingest 必須先呼叫 sync_stock_list 的原因。
    """
    c = get_connection(tmp_path / "test.db")
    c.executescript((BASE_DIR / "database" / "schema.sql").read_text(encoding="utf-8"))
    c.execute("INSERT INTO stocks (stock_id, name) VALUES ('2330', '台積電')")
    yield c
    c.close()


def price_row(date, close=100.0, **overrides):
    base = {
        "stock_id": "2330", "date": date,
        "open": close, "high": close, "low": close, "close": close,
        "volume": 1000, "turnover": 100000.0, "source": "FinMind",
    }
    base.update(overrides)
    return base


def price_frame(rows):
    return pd.DataFrame(rows, columns=PRICE_COLUMNS).astype({"volume": "Int64"})


def dividend_frame(rows):
    return pd.DataFrame(rows, columns=["ex_date", "cash_div", "stock_div", "reference_price"])


# ---------------------------------------------------------------------------
# upsert_prices
# ---------------------------------------------------------------------------

def test_prices_are_written(conn):
    result = upsert_prices(conn, price_frame([price_row("2024-03-18"), price_row("2024-03-19")]))

    assert result == (2, 0)
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 2


def test_upserting_the_same_frame_twice_does_not_add_rows(conn):
    """冪等的核心：重跑不該讓資料變兩份，也不該撞主鍵整批失敗"""
    df = price_frame([price_row("2024-03-18"), price_row("2024-03-19")])
    upsert_prices(conn, df)

    result = upsert_prices(conn, df)

    assert result == (0, 2)          # 全部是更新，沒有新增
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 2


def test_upsert_overwrites_changed_values(conn):
    """同一天的資料被更正時要蓋過去，不是留著舊的。

    資料源事後修正歷史資料是真的會發生的事，重跑就該把新值帶進來。
    """
    upsert_prices(conn, price_frame([price_row("2024-03-18", close=100.0)]))

    upsert_prices(conn, price_frame([price_row("2024-03-18", close=123.0)]))

    assert conn.execute("SELECT close FROM prices").fetchone()[0] == pytest.approx(123.0)


def test_upsert_refreshes_ingested_at(conn):
    """重跑要更新 ingested_at，才看得出這筆資料最後一次被確認是什麼時候。

    先把 ingested_at 手動塞成一個很舊的值再 upsert，而不是連跑兩次比較時間戳
    ——datetime('now') 的解析度是秒，同一秒內跑兩次會得到一樣的值，那種測試會偶爾紅。
    """
    df = price_frame([price_row("2024-03-18")])
    upsert_prices(conn, df)
    conn.execute("UPDATE prices SET ingested_at = '2000-01-01 00:00:00'")

    upsert_prices(conn, df)

    assert conn.execute("SELECT ingested_at FROM prices").fetchone()[0] != "2000-01-01 00:00:00"


def test_missing_volume_is_stored_as_null(conn):
    """cleaner 的 volume 是 Int64，缺值是 pd.NA。

    不轉成 None 的話 sqlite3 會丟 "type 'NAType' is not supported"，
    而且是寫到一半才失敗——前面的列已經進去了，後面的沒有。
    """
    df = price_frame([price_row("2024-03-18", volume=pd.NA)])

    upsert_prices(conn, df)

    assert conn.execute("SELECT volume FROM prices").fetchone()[0] is None


def test_volume_is_stored_as_an_integer_not_a_blob(conn):
    """Int64 欄位取出來是 numpy.int64，直接丟進 sqlite3 會被當成 BLOB 存。

    欄位型別看起來對、值卻是 b'\\x01\\x00\\x00...' 這種東西，
    而且要到之後做算術時才會發現。
    """
    upsert_prices(conn, price_frame([price_row("2024-03-18", volume=1234)]))

    stored = conn.execute("SELECT volume FROM prices").fetchone()[0]
    assert isinstance(stored, int) and stored == 1234


def test_empty_frame_writes_nothing(conn):
    assert upsert_prices(conn, price_frame([])) == (0, 0)
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0


def test_price_for_unknown_stock_is_rejected_by_foreign_key(conn):
    """stocks 裡沒有的股票不該寫得進 prices。

    這也是為什麼 run_ingest 必須先 sync_stock_list 再寫價格。
    """
    df = price_frame([price_row("2024-03-18", stock_id="9999")])

    with pytest.raises(sqlite3.IntegrityError):
        upsert_prices(conn, df)


# ---------------------------------------------------------------------------
# upsert_dividends
# ---------------------------------------------------------------------------

def test_dividends_are_written(conn):
    """dividend_events 從階段 B 建好到現在一直是空的，這是第一次真的有人寫它。

    步驟 24 讀取時要算還原價，唯一的資料來源就是這張表。
    """
    df = dividend_frame([{"ex_date": "2023-08-10", "cash_div": 0.80,
                          "stock_div": 0.0, "reference_price": 19.60}])

    result = upsert_dividends(conn, "2330", df)

    assert result == (1, 0)
    row = conn.execute("SELECT stock_id, ex_date, cash_div, source FROM dividend_events").fetchone()
    assert row == ("2330", "2023-08-10", pytest.approx(0.80), "FinMind")


def test_upserting_the_same_dividends_twice_does_not_add_rows(conn):
    df = dividend_frame([{"ex_date": "2023-08-10", "cash_div": 0.80,
                          "stock_div": 0.0, "reference_price": 19.60}])
    upsert_dividends(conn, "2330", df)

    result = upsert_dividends(conn, "2330", df)

    assert result == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM dividend_events").fetchone()[0] == 1


def test_missing_reference_price_is_stored_as_null(conn):
    """官方參考價拿不到時是缺值，不該讓整批寫入失敗——它只用來事後交叉驗證"""
    df = dividend_frame([{"ex_date": "2023-08-10", "cash_div": 0.80,
                          "stock_div": 0.0, "reference_price": None}])

    upsert_dividends(conn, "2330", df)

    assert conn.execute("SELECT reference_price FROM dividend_events").fetchone()[0] is None


def test_empty_dividend_frame_writes_nothing(conn):
    """大多數股票在大多數區間裡都沒有除權息，這是最常見的路徑"""
    assert upsert_dividends(conn, "2330", dividend_frame([])) == (0, 0)


# ---------------------------------------------------------------------------
# upsert_corporate_actions
# ---------------------------------------------------------------------------

def action_frame(rows):
    return pd.DataFrame(
        rows, columns=["ex_date", "action_type", "before_price", "after_price", "note"]
    )


def test_corporate_actions_are_written(conn):
    df = action_frame([{"ex_date": "2017-05-04", "action_type": "capital_reduction",
                        "before_price": 6.15, "after_price": 13.16, "note": "Making up losses"}])

    result = upsert_corporate_actions(conn, "2330", df)

    assert result == (1, 0)
    row = conn.execute(
        "SELECT stock_id, ex_date, action_type, before_price, after_price FROM corporate_actions"
    ).fetchone()
    assert row == ("2330", "2017-05-04", "capital_reduction",
                   pytest.approx(6.15), pytest.approx(13.16))


def test_corporate_actions_are_idempotent(conn):
    df = action_frame([{"ex_date": "2017-05-04", "action_type": "capital_reduction",
                        "before_price": 6.15, "after_price": 13.16, "note": None}])
    upsert_corporate_actions(conn, "2330", df)

    assert upsert_corporate_actions(conn, "2330", df) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 1


def test_reduction_and_split_on_the_same_day_are_separate_rows(conn):
    """主鍵含 action_type：同一天發生兩種公司行動時兩筆都要留著，不能互相覆蓋"""
    df = action_frame([
        {"ex_date": "2024-03-12", "action_type": "capital_reduction",
         "before_price": 100.0, "after_price": 200.0, "note": None},
        {"ex_date": "2024-03-12", "action_type": "split",
         "before_price": 200.0, "after_price": 100.0, "note": None},
    ])

    assert upsert_corporate_actions(conn, "2330", df) == (2, 0)


# ---------------------------------------------------------------------------
# quarantine
# ---------------------------------------------------------------------------

def test_quarantine_records_each_reason(conn):
    """一列違反幾條規則就記幾筆，步驟 26 才能直接 GROUP BY reason 看分布"""
    bad = validate(price_frame([price_row("2024-03-18", high=95.0, low=99.0)]))[1]

    written = quarantine(conn, bad)

    assert written == 3      # high_lt_low / high_lt_open / high_lt_close
    reasons = {r[0] for r in conn.execute("SELECT reason FROM data_quarantine")}
    assert reasons == {"high_lt_low", "high_lt_open", "high_lt_close"}


def test_quarantining_twice_does_not_add_rows(conn):
    """重跑時隔離筆數不該愈疊愈高，否則看不出這次真的新抓到幾筆問題"""
    bad = validate(price_frame([price_row("2024-03-18", high=95.0, low=99.0)]))[1]
    quarantine(conn, bad)

    quarantine(conn, bad)

    assert conn.execute("SELECT COUNT(*) FROM data_quarantine").fetchone()[0] == 3


def test_quarantine_raw_json_is_valid_json_with_nulls(conn):
    """raw_json 要是合法 JSON：缺值必須是 null，不能是 Python 的 NaN。

    json.dumps 遇到 NaN 會吐出 {"x": NaN}，那不是合法 JSON，
    之後想用任何標準工具讀 raw_json 都會失敗。
    """
    import json

    bad = validate(price_frame([price_row("2024-03-18", close=float("nan"))]))[1]
    quarantine(conn, bad)

    raw = conn.execute("SELECT raw_json FROM data_quarantine").fetchone()[0]
    parsed = json.loads(raw)          # 非法 JSON 會在這裡炸掉
    assert parsed["close"] is None
    assert parsed["stock_id"] == "2330"
    assert "reason" not in parsed     # reason 已是獨立欄位，不重複塞進 JSON


def test_quarantine_accepts_rows_for_unknown_stocks(conn):
    """隔離區刻意沒有外鍵：stock_id 本身就錯的資料，正是最需要被收容的那種。

    如果連進隔離區都要先通過外鍵檢查，最可疑的資料會被擋在門外。
    """
    bad = validate(price_frame([price_row("2024-03-18", stock_id="9999", high=95.0, low=99.0)]))[1]

    assert quarantine(conn, bad) == 3


# ---------------------------------------------------------------------------
# update_ingest_log
# ---------------------------------------------------------------------------

def test_ingest_log_records_last_date_and_row_count(conn):
    upsert_prices(conn, price_frame([price_row("2024-03-18"), price_row("2024-03-19")]))

    update_ingest_log(conn, "2330")

    row = conn.execute("SELECT last_date, rows_total FROM ingest_log").fetchone()
    assert row == ("2024-03-19", 2)


def test_ingest_log_is_derived_from_prices_not_from_the_batch(conn):
    """last_date 要反映「資料庫裡真的有的最後一天」，不是「這批抓了什麼」。

    plan_downloads 拿 last_date 決定下次從哪天開始抓。如果某天的資料全部被
    validator 擋掉，last_date 就不該前進——下次會再抓一次那段，這是對的，
    我們確實還沒拿到那天的資料。
    """
    good, _ = validate(price_frame([
        price_row("2024-03-18"),
        price_row("2024-03-19", high=95.0, low=99.0),    # 致命，不會進 prices
    ]))
    upsert_prices(conn, good)

    update_ingest_log(conn, "2330")

    assert conn.execute("SELECT last_date FROM ingest_log").fetchone()[0] == "2024-03-18"


def test_ingest_log_counts_only_this_stock(conn):
    """統計要限定在這檔股票——這是 WHERE stock_id = ? 那個條件的回歸測試。

    漏掉條件的話，每檔股票的 last_date 都會變成「全市場最新的那一天」、
    rows_total 變成全表列數。plan_downloads 拿 last_date 決定下次從哪天開始抓，
    所以資料較少的股票會被判定成「已經是最新的」而永遠不再補抓，
    而且完全不會報錯。只有一檔股票的測試分辨不出這件事。
    """
    conn.execute("INSERT INTO stocks (stock_id, name) VALUES ('2317', '鴻海')")
    upsert_prices(conn, price_frame([price_row("2024-03-18"), price_row("2024-03-19")]))
    upsert_prices(conn, price_frame([price_row("2024-03-20", stock_id="2317")]))

    update_ingest_log(conn, "2330")
    update_ingest_log(conn, "2317")

    logs = dict(
        (row[0], (row[1], row[2]))
        for row in conn.execute("SELECT stock_id, last_date, rows_total FROM ingest_log")
    )
    assert logs["2330"] == ("2024-03-19", 2)      # 不該被 2317 的 03-20 帶到
    assert logs["2317"] == ("2024-03-20", 1)


def test_ingest_log_is_idempotent(conn):
    upsert_prices(conn, price_frame([price_row("2024-03-18")]))
    update_ingest_log(conn, "2330")

    update_ingest_log(conn, "2330")

    assert conn.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0] == 1


def test_ingest_log_for_a_stock_with_no_prices(conn):
    """一列都沒抓到時 last_date 是 NULL，plan_downloads 會從 HISTORY_START_DATE 重來"""
    update_ingest_log(conn, "2330")

    assert conn.execute("SELECT last_date, rows_total FROM ingest_log").fetchone() == (None, 0)


# ---------------------------------------------------------------------------
# 串起來：cleaner -> validator -> loader
# ---------------------------------------------------------------------------

def test_full_pipeline_is_idempotent(conn):
    """把整條線跑兩次，三張表的列數都不變。

    這是步驟 22 真正的完成標準——個別函式冪等不代表串起來也冪等。
    """
    raw = pd.DataFrame([
        {"date": "2024-03-18", "open": 100, "high": 101, "low": 99,
         "close": 100, "volume": 1000, "turnover": 1e5},
        {"date": "2024-03-19", "open": 100, "high": 95, "low": 99,
         "close": 100, "volume": 1000, "turnover": 1e5},      # 壞列
    ])
    divs = dividend_frame([{"ex_date": "2024-03-18", "cash_div": 2.0,
                            "stock_div": 0.0, "reference_price": 98.0}])

    def run():
        good, bad = validate(clean(raw, "2330"))
        upsert_prices(conn, good)
        upsert_dividends(conn, "2330", divs)
        quarantine(conn, bad)
        update_ingest_log(conn, "2330")
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("prices", "dividend_events", "data_quarantine", "ingest_log")
        )

    first = run()

    assert run() == first
    assert first == (1, 1, 3, 1)


def test_nothing_is_committed_by_the_loader(conn, tmp_path):
    """loader 不自己 commit——交易由 run_ingest 控制，一檔股票的四張表要一起成功。

    分開 commit 的話，中途掛掉會留下「價格寫了但 ingest_log 沒更新」這種狀態。
    """
    upsert_prices(conn, price_frame([price_row("2024-03-18")]))
    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0
