"""
把 cleaner / validator 的產物寫進資料庫。

四個寫入點，各自對應一張表：
    upsert_prices      -> prices
    upsert_dividends   -> dividend_events
    quarantine         -> data_quarantine
    update_ingest_log  -> ingest_log

**全部都是 UPSERT，不是 INSERT。** 整條管線必須可以重跑：重跑一次不該讓資料變兩份，
也不該因為主鍵衝突就整批失敗。這是「冪等」的實際意思，也是之後排程自動化的前提。

**這裡的函式都不 commit。** 交易由呼叫端（步驟 23 的 run_ingest）控制，這樣一檔股票的
價格、除權息、隔離紀錄、ingest_log 才會一起成功或一起失敗。分開 commit 的話，
中途掛掉會留下「價格寫了但 ingest_log 沒更新」這種狀態，下次增量更新就會重抓。

**寫入順序有硬性限制。** database/connection.py 強制開啟外鍵檢查，而 prices、
ingest_log、dividend_events 三張表都有 FOREIGN KEY 指向 stocks，所以這三個函式
一律要在 downloader.sync_stock_list() 之後才能呼叫，否則會 IntegrityError。
（data_quarantine 刻意沒有外鍵，理由見 database/schema.sql。）
"""

import json
import sqlite3
from typing import NamedTuple

import pandas as pd

from .cleaner import PRICE_COLUMNS, DEFAULT_SOURCE

# PriceSource.fetch_dividends 的回傳欄位
DIVIDEND_COLUMNS = ["ex_date", "cash_div", "stock_div", "reference_price"]

# PriceSource.fetch_corporate_actions 的回傳欄位
ACTION_COLUMNS = ["ex_date", "action_type", "before_price", "after_price", "note"]


class UpsertResult(NamedTuple):
    """寫入結果。分開回報是為了讓步驟 23 的摘要能講「新增 X 列 / 更新 Y 列」——
    兩者意義完全不同：增量更新時新增列數應該接近 universe 檔數，
    如果某次跑出「更新 3000 列」，那代表在重抓已經有的歷史，排程邏輯出問題了。"""

    inserted: int
    updated: int


def _rows(df: pd.DataFrame, columns: list[str]) -> list[tuple]:
    """把 DataFrame 轉成 executemany 吃得下的 tuple 清單。

    astype(object) 這一步不是為了好看，是必要的，它同時解決兩個會炸掉的問題：

    1. cleaner 的 volume 是 Int64，缺值是 pd.NA。sqlite3 不認得它，直接丟進
       executemany 會得到 "type 'NAType' is not supported"，而且是**寫到一半才失敗**
       ——前面的列已經進去了，後面的沒有。
    2. 就算沒有缺值，Int64 欄位取出來的是 numpy.int64，sqlite3 會把它當成 BLOB 存
       （實測讀回來是 b'\\x01\\x00\\x00...' 而不是 1），欄位型別看起來對、值卻是錯的。

    astype(object) 會把 numpy 標量換成 Python 標量，再用 where 把缺值換成 None
    （SQL 的 NULL）。
    """
    subset = df[columns]
    converted = subset.astype(object).where(subset.notna(), None)
    return list(converted.itertuples(index=False, name=None))


def _jsonable(record: dict) -> dict:
    """把一列資料轉成可以 json.dumps 的字典。

    json.dumps 遇到 NaN 會吐出 {"x": NaN}——那不是合法 JSON，之後想用任何標準
    JSON 工具讀 raw_json 都會失敗。缺值一律轉成 null。
    """
    out = {}
    for key, value in record.items():
        if pd.isna(value):
            out[key] = None
        elif hasattr(value, "item"):      # numpy 標量 -> Python 標量
            out[key] = value.item()
        else:
            out[key] = value
    return out


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def upsert_prices(conn: sqlite3.Connection, df: pd.DataFrame) -> UpsertResult:
    """把 validator 放行的日線寫進 prices。

    Args:
        conn: 已開啟的連線（呼叫端負責 commit）
        df: validator 回傳的 good_df，欄位為 cleaner.PRICE_COLUMNS

    Returns:
        UpsertResult(新增列數, 更新列數)
    """
    if df.empty:
        return UpsertResult(0, 0)

    before = _count(conn, "prices")
    conn.executemany(
        """
        INSERT INTO prices
            (stock_id, date, open, high, low, close, volume, turnover, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_id, date) DO UPDATE SET
            open        = excluded.open,
            high        = excluded.high,
            low         = excluded.low,
            close       = excluded.close,
            volume      = excluded.volume,
            turnover    = excluded.turnover,
            source      = excluded.source,
            ingested_at = datetime('now')
        """,
        _rows(df, PRICE_COLUMNS),
    )
    inserted = _count(conn, "prices") - before
    # df 的 (stock_id, date) 是唯一的（cleaner 去過重、validator 也擋過重複），
    # 所以沒有新增的那些必然是更新。
    return UpsertResult(inserted, len(df) - inserted)


def upsert_dividends(
    conn: sqlite3.Connection, stock_id: str, df: pd.DataFrame, source: str = DEFAULT_SOURCE
) -> UpsertResult:
    """把除權息事件寫進 dividend_events。

    **這是整個階段 D 最關鍵的一步。** prices 從步驟 16 起就不存還原價了，
    步驟 24 的 data_access 要在讀取時算還原價，唯一的資料來源就是這張表。
    沒有這一步，還原價無從算起。

    Args:
        df: PriceSource.fetch_dividends 的回傳，欄位為 DIVIDEND_COLUMNS
    """
    if df.empty:
        return UpsertResult(0, 0)

    before = _count(conn, "dividend_events")
    rows = [(stock_id, *values, source) for values in _rows(df, DIVIDEND_COLUMNS)]
    conn.executemany(
        """
        INSERT INTO dividend_events
            (stock_id, ex_date, cash_div, stock_div, reference_price, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_id, ex_date) DO UPDATE SET
            cash_div        = excluded.cash_div,
            stock_div       = excluded.stock_div,
            reference_price = excluded.reference_price,
            source          = excluded.source,
            ingested_at     = datetime('now')
        """,
        rows,
    )
    inserted = _count(conn, "dividend_events") - before
    return UpsertResult(inserted, len(df) - inserted)


def upsert_corporate_actions(
    conn: sqlite3.Connection, stock_id: str, df: pd.DataFrame, source: str = DEFAULT_SOURCE
) -> UpsertResult:
    """把減資與面額變更寫進 corporate_actions。

    跟 dividend_events 一樣，這是步驟 24 算還原價的輸入。兩張表分開是因為調整方式
    不同：除權息要用證交所公式從股利推參考價，這裡的參考價是官方直接公告的。

    Args:
        df: PriceSource.fetch_corporate_actions 的回傳，欄位為 ACTION_COLUMNS
    """
    if df.empty:
        return UpsertResult(0, 0)

    before = _count(conn, "corporate_actions")
    rows = [(stock_id, *values, source) for values in _rows(df, ACTION_COLUMNS)]
    conn.executemany(
        """
        INSERT INTO corporate_actions
            (stock_id, ex_date, action_type, before_price, after_price, note, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_id, ex_date, action_type) DO UPDATE SET
            before_price = excluded.before_price,
            after_price  = excluded.after_price,
            note         = excluded.note,
            source       = excluded.source,
            ingested_at  = datetime('now')
        """,
        rows,
    )
    inserted = _count(conn, "corporate_actions") - before
    return UpsertResult(inserted, len(df) - inserted)


def quarantine(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """把 validator 攔下來或標記的列記進 data_quarantine。

    raw_json 存整列的原始內容，這樣事後回頭查「當初到底是什麼樣的資料被擋掉」
    不需要重新下載。reason 已經是獨立欄位，就不重複塞進 JSON 裡。

    Args:
        df: validator 回傳的 bad_df（欄位是 PRICE_COLUMNS 加上 reason）

    Returns:
        寫入的筆數。注意這是「問題筆數」不是「列數」——一列違反三條規則會記三筆。
    """
    if df.empty:
        return 0

    rows = []
    for record in df.to_dict("records"):
        reason = record.pop("reason")
        rows.append((
            record["stock_id"],
            record["date"],
            json.dumps(_jsonable(record), ensure_ascii=False, sort_keys=True),
            reason,
        ))

    conn.executemany(
        """
        INSERT INTO data_quarantine (stock_id, date, raw_json, reason)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(stock_id, date, reason) DO UPDATE SET
            raw_json  = excluded.raw_json,
            caught_at = datetime('now')
        """,
        rows,
    )
    return len(rows)


def update_ingest_log(conn: sqlite3.Connection, stock_id: str) -> None:
    """更新這檔股票的下載進度紀錄。

    last_date 與 rows_total **從 prices 現算**，不從剛寫入的 DataFrame 取。
    理由是 plan_downloads 拿 last_date 決定下次要從哪天開始抓——讓它等於
    「資料庫裡真的有的最後一天」，就不可能出現「log 宣稱有資料、實際上沒有」
    的情形。例如某天的資料全部被 validator 擋掉時，last_date 不會前進，
    下次會再抓一次那段，這是對的：我們確實還沒拿到那天的資料。

    代價是：完全抓不到資料的股票（已下市之類）last_date 會一直是 NULL，
    每次都從 HISTORY_START_DATE 重抓。sample universe 規模下可以接受；
    放大到全市場時（步驟 29）要再處理。

    last_run_at 是唯一無法從 prices 推導的欄位——它記的是「最後一次嘗試」，
    不是「最後一次成功」，兩者的差距本身就是有用的診斷訊息。
    """
    last_date, rows_total = conn.execute(
        "SELECT MAX(date), COUNT(*) FROM prices WHERE stock_id = ?", (stock_id,)
    ).fetchone()

    conn.execute(
        """
        INSERT INTO ingest_log (stock_id, last_date, last_run_at, rows_total)
        VALUES (?, ?, datetime('now'), ?)
        ON CONFLICT(stock_id) DO UPDATE SET
            last_date   = excluded.last_date,
            last_run_at = excluded.last_run_at,
            rows_total  = excluded.rows_total
        """,
        (stock_id, last_date, rows_total),
    )
