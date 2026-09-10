import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import sqlite3
from config.settings import DATABASE_PATH


def test_db_connects():
    """測試能不能成功連到資料庫檔案"""
    conn = sqlite3.connect(DATABASE_PATH)
    assert conn is not None
    conn.close()


def test_stocks_table_exists():
    """測試 stocks 表格存在，且有資料"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.execute("SELECT COUNT(*) FROM stocks")
    count = cursor.fetchone()[0]
    conn.close()
    assert count > 0


def test_prices_table_has_expected_columns():
    """測試 prices 表格的欄位是否符合預期"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.execute("PRAGMA table_info(prices)")
    columns = [row[1] for row in cursor.fetchall()]
    conn.close()
    expected = ["date", "stock_id", "open", "high", "low", "close", "volume"]
    assert columns == expected