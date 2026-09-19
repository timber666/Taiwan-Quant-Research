import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import sqlite3
import pytest
from config.settings import BASE_DIR
from database.connection import get_connection
from database.init_db import init_db

EXPECTED_TABLES = {"stocks", "prices", "ingest_log", "data_quarantine", "dividend_events"}


@pytest.fixture(scope="module", autouse=True)
def ensure_schema():
    """跑測試前先確保 schema 存在，測試不依賴外部先手動執行過 init_db.py"""
    init_db()


def test_db_connects():
    """測試能不能成功連到資料庫檔案"""
    conn = get_connection()
    assert conn is not None
    conn.close()


def test_all_tables_exist():
    """測試 schema.sql 定義的 5 張表都存在"""
    conn = get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    conn.close()
    assert EXPECTED_TABLES.issubset(tables)


def test_prices_table_has_expected_columns():
    """測試 prices 表格的欄位是否符合新 schema（含 adj_close/turnover/source 等欄位）"""
    conn = get_connection()
    cursor = conn.execute("PRAGMA table_info(prices)")
    columns = [row[1] for row in cursor.fetchall()]
    conn.close()
    expected = [
        "stock_id", "date", "open", "high", "low", "close",
        "adj_close", "volume", "turnover", "source", "ingested_at",
    ]
    assert columns == expected


def test_prices_primary_key_is_stock_id_and_date():
    """測試 prices 的 primary key 是 (stock_id, date) 複合主鍵，而不是單一欄位"""
    conn = get_connection()
    cursor = conn.execute("PRAGMA table_info(prices)")
    # PRAGMA table_info 回傳的第 6 欄（index 5）是該欄位在 primary key 裡的順序，0 代表不是 pk
    pk_columns = {row[1] for row in cursor.fetchall() if row[5] > 0}
    conn.close()
    assert pk_columns == {"stock_id", "date"}


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
    conn.executescript((BASE_DIR / "database" / "schema.sql").read_text(encoding="utf-8"))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO prices (stock_id, date, close) VALUES ('9999', '2024-01-01', 10)")

    conn.close()
