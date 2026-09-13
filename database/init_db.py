import sys
import sqlite3
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config.settings import DATABASE_PATH, BASE_DIR

SCHEMA_PATH = BASE_DIR / "database" / "schema.sql"


def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()

    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    conn.close()
    return [t[0] for t in tables]


if __name__ == "__main__":
    tables = init_db()
    print(f"資料庫已就緒: {DATABASE_PATH}")
    print(f"已建立的表: {tables}")
