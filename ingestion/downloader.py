"""
下載排程：決定「抓誰、抓多少」，並把結果同步進資料庫。
"""

import sqlite3
from datetime import date, timedelta

import pandas as pd
from tqdm import tqdm

from .sources.base import PriceSource
from config.settings import HISTORY_START_DATE


def sync_stock_list(source: PriceSource, conn: sqlite3.Connection, universe: list[str]) -> int:
    """抓資料來源的股票清單，只保留 universe 內的股票，UPSERT 進 stocks 表。

    Args:
        source: 任一 PriceSource 實作（例如 FinMindSource）
        conn: 已開啟的 sqlite3 連線
        universe: 要保留的 stock_id 清單（例如 config.settings.SAMPLE_UNIVERSE）

    Returns:
        實際寫入（新增或更新）的檔數
    """
    stock_list = source.fetch_stock_list()
    filtered = stock_list[stock_list["stock_id"].isin(universe)]

    missing = set(universe) - set(filtered["stock_id"])
    if missing:
        print(f"警告：universe 裡有 {len(missing)} 檔在資料來源清單裡找不到: {sorted(missing)}")

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
    conn.commit()
    return len(rows)


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


def download_all(source: PriceSource, plan: dict[str, tuple[str, str]]) -> dict[str, pd.DataFrame]:
    """依照 plan_downloads 算出的區間，把每一檔股票的股價抓下來。

    這一步只負責「抓」，不寫進資料庫——先確認資料抓得對，
    之後再交給 cleaner -> validator -> loader 那條線處理。

    - start > end（沒有新資料要抓）的股票直接跳過，不浪費 API 呼叫
    - 單檔抓取失敗會被記錄下來並跳過，不會讓整批下載中斷

    Returns:
        {stock_id: DataFrame}，只包含成功抓到資料的股票
        （略過和失敗的清單會印出來，不會混進回傳值裡）
    """
    results: dict[str, pd.DataFrame] = {}
    skipped: list[str] = []
    failed: dict[str, str] = {}

    for stock_id, (start, end) in tqdm(plan.items(), desc="下載股價"):
        if start > end:
            skipped.append(stock_id)
            continue
        try:
            df = source.fetch_prices(stock_id, start, end)
        except Exception as e:
            failed[stock_id] = str(e)
            continue
        results[stock_id] = df

    print(f"下載完成: 成功 {len(results)} 檔, 略過 {len(skipped)} 檔（無新資料）, 失敗 {len(failed)} 檔")
    if skipped:
        print(f"  略過: {skipped}")
    if failed:
        print("  失敗:")
        for stock_id, err in failed.items():
            print(f"    {stock_id}: {err}")

    return results
