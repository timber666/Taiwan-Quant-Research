"""
資料庫結構的測試。

**結構類的測試一律對「從 schema.sql 現建的暫存資料庫」驗證，不要去看 quant.db。**
理由是實際踩到的：`init_db()` 用的是 `CREATE TABLE IF NOT EXISTS`，它不會修改
已經存在的表。所以如果拿舊的 quant.db 來驗 schema，就算把欄位改錯、
把主鍵拿掉，測試也一樣全綠——測的其實是「這台機器上那顆檔案長什麼樣」，
不是「schema.sql 寫了什麼」。
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import sqlite3
import pytest
from config.settings import BASE_DIR
from database.connection import get_connection
from database.init_db import init_db

EXPECTED_TABLES = {"stocks", "prices", "ingest_log", "data_quarantine", "dividend_events"}
SCHEMA_SQL = (BASE_DIR / "database" / "schema.sql").read_text(encoding="utf-8")


@pytest.fixture(scope="module", autouse=True)
def ensure_schema():
    """跑測試前先確保正式資料庫存在，測試不依賴外部先手動執行過 init_db.py"""
    init_db()


@pytest.fixture(scope="module")
def schema_conn(tmp_path_factory):
    """從 schema.sql 現建一個乾淨的暫存資料庫，結構類的測試都走這個。"""
    conn = get_connection(tmp_path_factory.mktemp("schema") / "schema.db")
    conn.executescript(SCHEMA_SQL)
    yield conn
    conn.close()


def table_info(conn, table):
    """回傳 [(欄位名, 型別, 在主鍵裡的順序)]，0 代表不是主鍵。"""
    return [(row[1], row[2], row[5]) for row in conn.execute(f"PRAGMA table_info({table})")]


def test_db_connects():
    """測試能不能成功連到資料庫檔案"""
    conn = get_connection()
    assert conn is not None
    conn.close()


def test_all_tables_exist(schema_conn):
    """schema.sql 定義的 5 張表都要建得出來"""
    tables = {row[0] for row in schema_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert EXPECTED_TABLES.issubset(tables)


def test_prices_table_has_expected_columns(schema_conn):
    """prices 的欄位清單。

    刻意用 == 而不是 issubset：多一個欄位也要讓測試紅掉。
    尤其是 adj_close——它在階段 D 步驟 16 被拿掉了，如果哪天有人「順手加回來」，
    那個欄位在增量更新下必定會過期（見 database/schema.sql 裡 prices 的註解），
    這個測試就是那道防線。
    """
    columns = [name for name, _type, _pk in table_info(schema_conn, "prices")]
    assert columns == [
        "stock_id", "date", "open", "high", "low", "close",
        "volume", "turnover", "source", "ingested_at",
    ]


def test_prices_primary_key_is_stock_id_and_date(schema_conn):
    """prices 的 primary key 是 (stock_id, date) 複合主鍵，而不是單一欄位"""
    pk = {name for name, _type, pk_order in table_info(schema_conn, "prices") if pk_order > 0}
    assert pk == {"stock_id", "date"}


def test_quarantine_primary_key_prevents_duplicate_rows(schema_conn):
    """data_quarantine 的主鍵是 (stock_id, date, reason)。

    沒有主鍵的話，每重跑一次 ingest 就會把同一筆壞資料再疊一份進來，
    隔離筆數會愈跑愈大，看不出這次到底新抓到幾筆問題。
    """
    pk = {name for name, _type, pk_order in table_info(schema_conn, "data_quarantine") if pk_order > 0}
    assert pk == {"stock_id", "date", "reason"}


def test_live_database_matches_schema(schema_conn):
    """正式的 quant.db 結構要跟 schema.sql 一致。

    這條是上面那個坑的守門員：改了 schema.sql 卻沒有重建資料庫時，
    舊表會原封不動留著，程式跑起來卻以為欄位已經換了。
    這個測試紅了，處置方式是刪掉 database/quant.db 再跑 database/init_db.py。
    """
    live = get_connection()
    try:
        for table in sorted(EXPECTED_TABLES):
            assert table_info(live, table) == table_info(schema_conn, table), (
                f"{table} 的結構跟 schema.sql 不一致；"
                f"請刪掉 database/quant.db 後重跑 database/init_db.py"
            )
    finally:
        live.close()


def test_connection_enables_foreign_keys():
    """SQLite 的外鍵檢查預設是關閉的，而且是每個連線各自設定。

    get_connection() 必須把它打開，否則 schema.sql 裡那些 FOREIGN KEY
    宣告完全不會生效，等於沒寫。
    """
    conn = get_connection()
    enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.close()
    assert enabled == 1


def test_foreign_key_blocks_price_for_unknown_stock(tmp_path):
    """stocks 表裡沒有的股票，不該塞得進 prices 表。

    用暫存資料庫測，不要把測試資料寫進正式的 quant.db。
    """
    conn = get_connection(tmp_path / "fk.db")
    conn.executescript(SCHEMA_SQL)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO prices (stock_id, date, close) VALUES ('9999', '2024-01-01', 10)")

    conn.close()
