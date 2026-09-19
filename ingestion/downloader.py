"""
下載排程：決定「抓誰、抓多少」，並把結果同步進資料庫。
"""

import sqlite3
from datetime import date, timedelta
from typing import NamedTuple

import pandas as pd
from tqdm import tqdm

from .sources.base import PriceSource
from config.settings import HISTORY_START_DATE


class SyncResult(NamedTuple):
    """sync_stock_list 的結果摘要。"""

    synced: int                  # 寫進（新增或更新）stocks 表的檔數
    deactivated: list[str]       # 這次新被標記為下市（is_active 由 1 改成 0）的股票


class DownloadResult(NamedTuple):
    """download_all 的結果摘要。

    四種結局刻意分開放，不要只回傳成功的那些：orchestrator（步驟 22）要印摘要、
    要把失敗記進 log，如果失敗清單只有 print 出來，上層就什麼都拿不到。

    「呼叫成功但回傳 0 列」也單獨一類，不混進 data——它跟真的抓到資料是兩回事，
    混在一起會讓摘要顯示「成功 N 檔」但其實什麼都沒抓到。
    """

    data: dict[str, pd.DataFrame]   # 成功且有抓到資料
    empty: list[str]                # 呼叫成功但回傳 0 列（停牌、或區間內沒有交易日）
    skipped: list[str]              # 根本沒打 API（沒有新資料要抓）
    failed: dict[str, str]          # 抓取失敗，值是錯誤訊息


def sync_stock_list(source: PriceSource, conn: sqlite3.Connection, universe: list[str]) -> SyncResult:
    """抓資料來源的股票清單，只保留 universe 內的股票，UPSERT 進 stocks 表。

    universe 裡「資料來源清單中找不到」的股票會被標記成 is_active = 0。
    這些通常是已下市、或代碼打錯的股票。不標記的話 is_active 永遠是 1，
    分不出「還在市場上」跟「已經下市」，回測就會有存活者偏誤
    （survivorship bias）——只用今天還活著的股票回測，績效會虛高。

    Args:
        source: 任一 PriceSource 實作（例如 FinMindSource）
        conn: 已開啟的 sqlite3 連線
        universe: 要保留的 stock_id 清單（例如 config.settings.SAMPLE_UNIVERSE）

    Returns:
        SyncResult(synced=寫入的檔數, deactivated=這次新標記為下市的股票)
    """
    stock_list = source.fetch_stock_list()
    filtered = stock_list[stock_list["stock_id"].isin(universe)]

    rows = list(
        filtered[["stock_id", "name", "market", "industry", "listing_date"]]
        .itertuples(index=False, name=None)
    )

    conn.executemany(
        """
        INSERT INTO stocks (stock_id, name, market, industry, listing_date, is_active, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, datetime('now'))
        ON CONFLICT(stock_id) DO UPDATE SET
            name = excluded.name,
            market = excluded.market,
            industry = excluded.industry,
            listing_date = excluded.listing_date,
            is_active = 1,
            updated_at = datetime('now')
        """,
        rows,
    )

    missing = sorted(set(universe) - set(filtered["stock_id"]))
    deactivated = _deactivate(conn, missing)

    conn.commit()

    if missing:
        print(f"警告：universe 裡有 {len(missing)} 檔在資料來源清單裡找不到: {missing}")
    if deactivated:
        print(f"  其中 {len(deactivated)} 檔本來是上市中，已標記為下市: {deactivated}")

    return SyncResult(synced=len(rows), deactivated=deactivated)


def _deactivate(conn: sqlite3.Connection, stock_ids: list[str]) -> list[str]:
    """把指定股票標記成下市（is_active = 0），回傳「這次真的被改到」的那些。

    只挑目前 is_active = 1 的來改，所以重跑時不會每次都宣稱同一檔又下市了一次
    ——回傳值代表的是「狀態變化」，不是「符合條件的全部」。
    """
    if not stock_ids:
        return []

    placeholders = ",".join("?" * len(stock_ids))
    still_active = [
        row[0] for row in conn.execute(
            f"SELECT stock_id FROM stocks WHERE stock_id IN ({placeholders}) AND is_active = 1",
            stock_ids,
        )
    ]
    if not still_active:
        return []

    placeholders = ",".join("?" * len(still_active))
    conn.execute(
        f"""
        UPDATE stocks SET is_active = 0, updated_at = datetime('now')
        WHERE stock_id IN ({placeholders})
        """,
        still_active,
    )
    return still_active


def plan_downloads(conn: sqlite3.Connection, universe: list[str]) -> dict[str, tuple[str, str]]:
    """決定每檔股票要抓的日期區間，增量更新的核心。

    查 ingest_log 拿每檔的 last_date：
    - 有紀錄 -> start = last_date 隔天（只抓新的部分，不重抓已經有的資料）
    - 沒紀錄（第一次跑這檔）-> start = HISTORY_START_DATE
    end 一律是今天。

    Args:
        conn: 已開啟的 sqlite3 連線
        universe: 要規劃下載的 stock_id 清單

    Returns:
        {stock_id: (start, end)}，日期都是 "YYYY-MM-DD" 字串

    注意：如果某檔股票昨天才更新過，這裡會算出 start = 明天，比 end（今天）晚。
    呼叫端（download_all）要自己過濾掉 start > end 的情況，不要真的拿這種區間去打 API。
    """
    if not universe:
        return {}

    today = date.today().isoformat()
    placeholders = ",".join("?" * len(universe))
    rows = conn.execute(
        f"SELECT stock_id, last_date FROM ingest_log WHERE stock_id IN ({placeholders})",
        universe,
    ).fetchall()
    last_dates = dict(rows)

    plan = {}
    for stock_id in universe:
        last_date = last_dates.get(stock_id)
        if last_date:
            start = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
        else:
            start = HISTORY_START_DATE
        plan[stock_id] = (start, today)

    return plan


def _has_possible_trading_day(start: str, end: str) -> bool:
    """區間內有沒有可能開市的日子。

    只排除週六日——國定假日還是會白打一次 API，要完全避免得等有交易日曆
    （規劃書步驟 26 才會建）。但光是週末這一項，就能省下每個週末
    「universe 檔數 × 每檔 3 次」的無謂呼叫。
    """
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if (last - first).days >= 6:      # 跨滿一週，一定包含平日，不用逐日檢查
        return True

    day = first
    while day <= last:
        if day.weekday() < 5:         # 0=週一 ... 4=週五
            return True
        day += timedelta(days=1)
    return False


def download_all(source: PriceSource, plan: dict[str, tuple[str, str]]) -> DownloadResult:
    """依照 plan_downloads 算出的區間，把每一檔股票的股價抓下來。

    這一步只負責「抓」，不寫進資料庫——先確認資料抓得對，
    之後再交給 cleaner -> validator -> loader 那條線處理。

    - 沒有東西可抓的（start > end，或區間內全是週末）直接跳過，不浪費 API 呼叫
    - 單檔抓取失敗會被記錄下來並跳過，不會讓整批下載中斷
    - 抓到 0 列的不算「成功」，另外歸到 empty

    Returns:
        DownloadResult(data, empty, skipped, failed)
    """
    results: dict[str, pd.DataFrame] = {}
    empty: list[str] = []
    skipped: list[str] = []
    failed: dict[str, str] = {}

    for stock_id, (start, end) in tqdm(plan.items(), desc="下載股價"):
        if start > end or not _has_possible_trading_day(start, end):
            skipped.append(stock_id)
            continue
        try:
            df = source.fetch_prices(stock_id, start, end)
        except Exception as e:
            failed[stock_id] = str(e)
            continue
        if df.empty:
            empty.append(stock_id)
            continue
        results[stock_id] = df

    print(
        f"下載完成: 成功 {len(results)} 檔, 無資料 {len(empty)} 檔, "
        f"略過 {len(skipped)} 檔（無新資料）, 失敗 {len(failed)} 檔"
    )
    if empty:
        print(f"  無資料: {empty}")
    if skipped:
        print(f"  略過: {skipped}")
    if failed:
        print("  失敗:")
        for stock_id, err in failed.items():
            print(f"    {stock_id}: {err}")

    return DownloadResult(data=results, empty=empty, skipped=skipped, failed=failed)
