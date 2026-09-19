"""
把整條資料管線串起來的進入點。

    sync_stock_list -> plan_downloads -> 逐檔：
        fetch_dividends -> upsert_dividends
        fetch_prices    -> clean -> validate -> upsert_prices + quarantine
                        -> update_ingest_log -> commit

執行方式：

    python -m ingestion.run_ingest --mode sample

**不是** `python ingestion/run_ingest.py`。ingestion/ 裡用的是相對 import，
直接把檔案當腳本跑會得到 `ImportError: attempted relative import with no
known parent package`。

**為什麼是逐檔處理、而不是先全部抓完再一起寫入**：每檔各自 commit，中途因為 API
額度用完而中斷時，已經處理完的股票不會白抓，下次靠 ingest_log 接著跑就好。
sample universe 只有 114 次呼叫、離 600 次/hr 的上限很遠，但步驟 29 放大到全市場
是 5100 次，一定會撞上限——orchestrator 從一開始就該為那個情況設計。

除權息要先抓，因為 validate 需要用除權息交易日來排除 price_jump 的誤報
（除權息當天原始價本來就會跳空，見 ingestion/validator.py）。
"""

import argparse
import sys
from dataclasses import dataclass, field

from tqdm import tqdm

from config.settings import (
    FINMIND_API_TOKEN, HISTORY_START_DATE, SAMPLE_UNIVERSE, UNIVERSE_MODE,
)
from database.connection import get_connection

from .cleaner import clean
from .downloader import has_possible_trading_day, plan_downloads, sync_stock_list
from .loader import quarantine, update_ingest_log, upsert_dividends, upsert_prices
from .sources.base import PriceSource, RateLimitError
from .sources.finmind_source import FinMindSource
from .validator import validate


@dataclass
class RunSummary:
    """一次執行的統計。"""

    processed: int = 0                          # 真的打了 API 並寫入的檔數
    inserted: int = 0                           # prices 新增列數
    updated: int = 0                            # prices 更新列數
    quarantined: int = 0                        # 隔離筆數（一列違反 N 條規則算 N 筆）
    dividends: int = 0                          # dividend_events 新增筆數
    empty: list = field(default_factory=list)   # 呼叫成功但沒有資料
    skipped: list = field(default_factory=list) # 沒打 API（沒有新資料要抓）
    inactive: list = field(default_factory=list)# 已下市，不再嘗試
    failed: dict = field(default_factory=dict)  # 抓取或寫入失敗
    rate_limited: str = ""                      # 非空代表因為額度用完提早中止


def resolve_universe(source: PriceSource, mode: str, only: str | None) -> list[str]:
    """決定這次要處理哪些股票。"""
    if only:
        return [only]
    if mode == "all":
        # 多一次 fetch_stock_list 的呼叫（不是每檔一次），換到「全市場」這份清單
        return sorted(source.fetch_stock_list()["stock_id"])
    return list(SAMPLE_UNIVERSE)


def active_members(conn, universe: list[str]) -> list[str]:
    """只留下還在市場上的股票。

    已下市的（sync_stock_list 標成 is_active = 0）不再嘗試下載——它們不會有新資料，
    每次都去打一次 API 是純浪費。哪天它們重新出現在來源清單裡，
    sync_stock_list 會把 is_active 改回 1，自然就會恢復下載。
    """
    placeholders = ",".join("?" * len(universe))
    rows = conn.execute(
        f"SELECT stock_id FROM stocks WHERE is_active = 1 AND stock_id IN ({placeholders})",
        universe,
    ).fetchall()
    active = {row[0] for row in rows}
    return [s for s in universe if s in active]


def ingest_one(conn, source: PriceSource, stock_id: str, start: str, end: str, summary: RunSummary):
    """處理單一股票：抓 -> 清 -> 驗 -> 寫，最後 commit。

    整檔在同一個交易裡：價格、除權息、隔離紀錄、ingest_log 要嘛一起成功、
    要嘛一起退回。中途失敗卻留下「價格寫了但 ingest_log 沒更新」的狀態，
    下次增量更新就會重抓那一段。
    """
    dividends = source.fetch_dividends(stock_id, start, end)
    prices = source.fetch_prices(stock_id, start, end)

    if prices.empty:
        summary.empty.append(stock_id)

    ex_dates = set(dividends["ex_date"]) if not dividends.empty else None

    div_result = upsert_dividends(conn, stock_id, dividends)
    good, bad = validate(clean(prices, stock_id), ex_dates=ex_dates)
    price_result = upsert_prices(conn, good)
    quarantined = quarantine(conn, bad)
    update_ingest_log(conn, stock_id)
    conn.commit()

    summary.processed += 1
    summary.inserted += price_result.inserted
    summary.updated += price_result.updated
    summary.dividends += div_result.inserted
    summary.quarantined += quarantined


def print_summary(conn, summary: RunSummary):
    print()
    print("=" * 60)
    if summary.rate_limited:
        print("!! 因為 API 額度用完提早中止，已處理的部分都已存檔")
        print(f"   {summary.rate_limited}")
        print("   等額度回復後再跑一次，會從 ingest_log 記錄的地方接著抓")
        print("-" * 60)

    print(f"處理 {summary.processed} 檔："
          f"prices 新增 {summary.inserted} 列 / 更新 {summary.updated} 列，"
          f"除權息新增 {summary.dividends} 筆，隔離 {summary.quarantined} 筆")

    for label, items in [
        ("沒有新資料（略過，未打 API）", summary.skipped),
        ("已下市（不再嘗試）", summary.inactive),
        ("呼叫成功但沒有資料", summary.empty),
    ]:
        if items:
            preview = items if len(items) <= 12 else items[:12] + ["..."]
            print(f"  {label}: {len(items)} 檔 {preview}")

    if summary.failed:
        print(f"  失敗: {len(summary.failed)} 檔")
        for stock_id, err in summary.failed.items():
            print(f"    {stock_id}: {err}")

    print("-" * 60)
    stocks, rows, first, last = conn.execute(
        "SELECT COUNT(DISTINCT stock_id), COUNT(*), MIN(date), MAX(date) FROM prices"
    ).fetchone()
    print(f"資料庫現況: prices {rows} 列，涵蓋 {stocks} 檔，日期 {first} ~ {last}")
    print(f"            dividend_events "
          f"{conn.execute('SELECT COUNT(*) FROM dividend_events').fetchone()[0]} 筆")

    reasons = conn.execute(
        "SELECT reason, COUNT(*) FROM data_quarantine GROUP BY reason ORDER BY COUNT(*) DESC"
    ).fetchall()
    if reasons:
        print("            data_quarantine:")
        for reason, count in reasons:
            print(f"              {reason}: {count}")
    print("=" * 60)


def run(mode: str = UNIVERSE_MODE, only: str | None = None, source: PriceSource | None = None):
    """跑一次完整的 ingest，回傳 RunSummary。

    source 可以注入，測試才不用真的連網。
    """
    conn = get_connection()
    source = source or FinMindSource(FINMIND_API_TOKEN)
    summary = RunSummary()

    universe = resolve_universe(source, mode, only)
    print(f"universe: {len(universe)} 檔（mode={mode}"
          f"{f', stock={only}' if only else ''}），歷史起點 {HISTORY_START_DATE}")

    sync_stock_list(source, conn, universe)

    tradable = active_members(conn, universe)
    summary.inactive = [s for s in universe if s not in tradable]

    plan = plan_downloads(conn, tradable)

    for stock_id, (start, end) in tqdm(plan.items(), desc="抓取與寫入"):
        if start > end or not has_possible_trading_day(start, end):
            summary.skipped.append(stock_id)
            continue
        try:
            ingest_one(conn, source, stock_id, start, end, summary)
        except RateLimitError as e:
            # 額度用完之後，後面每一檔都會失敗。繼續跑只是把同一則錯誤複製 N 份，
            # 不如把已完成的部分收好（每檔都已各自 commit），直接中止。
            conn.rollback()
            summary.rate_limited = str(e)
            break
        except Exception as e:
            conn.rollback()
            summary.failed[stock_id] = f"{type(e).__name__}: {e}"

    print_summary(conn, summary)
    conn.close()
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="下載台股日線與除權息資料，寫進 SQLite",
        epilog="範例: python -m ingestion.run_ingest --mode sample",
    )
    parser.add_argument("--mode", choices=["sample", "all"], default=UNIVERSE_MODE,
                        help="sample = config.settings.SAMPLE_UNIVERSE，all = 全市場")
    parser.add_argument("--stock", metavar="STOCK_ID",
                        help="只跑單一股票，方便除錯（會忽略 --mode）")
    args = parser.parse_args(argv)

    summary = run(mode=args.mode, only=args.stock)

    # 額度用完是「這次沒跑完」，用非零 exit code 表示，之後排程才判斷得出來。
    # 個別股票失敗不算——那在正常運作下本來就會偶爾發生。
    return 1 if summary.rate_limited else 0


if __name__ == "__main__":
    sys.exit(main())
