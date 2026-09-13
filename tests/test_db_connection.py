import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import sqlite3
import pytest
from config.settings import DATABASE_PATH
from database.init_db import init_db

EXPECTED_TABLES = {"stocks", "prices", "ingest_log", "data_quarantine", "dividend_events"}


@pytest.fixture(scope="module", autouse=True)
def ensure_schema():
    """跑測試前先確保 schema 存在，測試不依賴外部先手動執行過 init_db.py"""
    init_db()


def test_db_connects():
    """測試能不能成功連到資料庫檔案"""
    conn = sqlite3.connect(DATABASE_PATH)
    assert conn is not None
    conn.close()


def test_all_tables_exist():
    """測試 schema.sql 定義的 5 張表都存在"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    conn.close()
    assert EXPECTED_TABLES.issubset(tables)


def test_prices_table_has_expected_columns():
    """測試 prices 表格的欄位是否符合新 schema（含 adj_close/turnover/source 等欄位）"""
    conn = sqlite3.connect(DATABASE_PATH)
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
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.execute("PRAGMA table_info(prices)")
    # PRAGMA table_info 回傳的第 6 欄（index 5）是該欄位在 primary key 裡的順序，0 代表不是 pk
    pk_columns = {row[1] for row in cursor.fetchall() if row[5] > 0}
    conn.close()
    assert pk_columns == {"stock_id", "date"}
