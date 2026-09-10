import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config.settings import DATABASE_PATH
import sqlite3
import random
from datetime import date, timedelta

conn = sqlite3.connect("database/quant.db")

with open("database/schema.sql") as f:
    conn.executescript(f.read())

# 清空舊資料，避免重複跑造成重複插入
conn.execute("DELETE FROM stocks")
conn.execute("DELETE FROM prices")

# 插入幾支股票
stocks = [
    ("2330", "台積電", "半導體"),
    ("2317", "鴻海", "電子零組件"),
    ("2454", "聯發科", "半導體"),
]
conn.executemany("INSERT INTO stocks VALUES (?, ?, ?)", stocks)

# 幫每支股票產生 30 天的假價格資料
start_date = date(2026, 8, 1)
for stock_id, _, _ in stocks:
    price = random.uniform(100, 900)
    for i in range(30):
        d = start_date + timedelta(days=i)
        change = random.uniform(-0.03, 0.03)
        price = price * (1 + change)
        open_p = price * random.uniform(0.99, 1.01)
        high_p = max(open_p, price) * random.uniform(1.0, 1.02)
        low_p = min(open_p, price) * random.uniform(0.98, 1.0)
        volume = random.randint(5_000_000, 50_000_000)
        conn.execute(
            "INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)",
            (d.isoformat(), stock_id, open_p, high_p, low_p, price, volume)
        )

conn.commit()
print("資料插入完成")
conn.close()