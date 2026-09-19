"""
資料庫連線的統一入口。

所有要連 SQLite 的地方都走這裡，不要自己 `sqlite3.connect()`。

原因：SQLite 的外鍵檢查**預設是關閉的，而且是每個連線各自設定**。
`schema.sql` 裡寫了 FOREIGN KEY 不代表它會生效——沒有下
`PRAGMA foreign_keys = ON` 的連線，那些宣告完全形同虛設，可以把
`stocks` 表裡根本不存在的股票塞進 `prices`。集中在一個地方開連線，
就不會有「某支程式忘了開」這種漏洞。
"""

import sys
import sqlite3
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config.settings import DATABASE_PATH


def get_connection(db_path=None) -> sqlite3.Connection:
    """開一個已啟用外鍵檢查的 SQLite 連線。

    Args:
        db_path: 資料庫檔案路徑。預設用 `config.settings.DATABASE_PATH`，
            測試可以傳入暫存檔路徑，不要動到正式的 quant.db。

    Returns:
        已經下過 `PRAGMA foreign_keys = ON` 的連線
    """
    conn = sqlite3.connect(db_path or DATABASE_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
