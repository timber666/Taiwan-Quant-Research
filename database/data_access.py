"""
資料存取層：所有讀取資料庫的動作都走這裡。

Phase 2 的回測引擎、Phase 3 的因子研究都只跟這一層打交道，不自己寫 SQL。
這樣之後換資料庫（SQLite -> PostgreSQL）或改 schema，上層完全不用動。

**還原股價是在這一層算出來的，不是存在資料庫裡。** `prices` 只存原始成交價，
除權息存在 `dividend_events`、減資與面額變更存在 `corporate_actions`，
讀取時才用 `ingestion/adjustment.py` 把它們組起來。

這個設計是階段 D 步驟 16 改的，理由是後還原的調整因子取決於「這一天之後發生過
哪些事件」——每發生一次除權息，比它更早的所有還原價就全部要重算。存進資料庫的
還原價在下一次除權息之後就過期了，而增量更新不會回頭改歷史列。
改成讀取時計算，就不可能過期（詳見 database/schema.sql 裡 prices 的註解）。
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import pandas as pd

from database.connection import get_connection
from ingestion.adjustment import calculate_adjusted_price

RAW_COLUMNS = ["open", "high", "low", "close", "volume", "turnover"]
ADJUSTED_COLUMNS = ["adj_open", "adj_high", "adj_low", "adj_close"]


def _with_connection(conn):
    """沒有傳連線就自己開一個，並回傳「要不要自己關掉」。

    讓呼叫端可以選擇：一般使用不用管連線，批次操作（例如 get_price_matrix
    一次讀幾十檔）可以共用同一個連線，測試則可以塞一個暫存資料庫進來。
    """
    if conn is not None:
        return conn, False
    return get_connection(), True


def _to_date_index(df: pd.DataFrame) -> pd.DataFrame:
    """把 date 欄位變成 DatetimeIndex。

    用 DatetimeIndex 而不是字串 index，是為了能直接做時間序列的操作——
    `df.loc["2020"]` 這種部分字串索引、resample、rolling 都需要它。
    """
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    return out.set_index("date").sort_index()


def get_universe(active_only: bool = True, conn=None) -> list[str]:
    """回傳資料庫裡的股票代碼清單。

    Args:
        active_only: 只要還在市場上的。設成 False 會把已下市的也拿回來——
            回測要避免存活者偏誤（survivorship bias）時需要那些，
            只用今天還活著的股票回測，績效會虛高。
    """
    conn, should_close = _with_connection(conn)
    try:
        sql = "SELECT stock_id FROM stocks"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY stock_id"
        return [row[0] for row in conn.execute(sql)]
    finally:
        if should_close:
            conn.close()


def get_prices(
    stock_id: str,
    start: str = None,
    end: str = None,
    adjusted: bool = True,
    conn=None,
) -> pd.DataFrame:
    """取單一股票的日線。

    Args:
        stock_id: 股票代碼
        start, end: "YYYY-MM-DD"，含頭含尾。None 代表不限制。
        adjusted: True 會多算出 adj_open / adj_high / adj_low / adj_close。
            **回測算報酬一定要用 adj_close**，用 close 會把除權息當成真的下跌。
            畫「當時市場上看到的股價」才用 close。

    Returns:
        以 date 為 DatetimeIndex 的 DataFrame，依日期升冪。
        欄位：open, high, low, close, volume, turnover
        （adjusted=True 時再加上 adj_open, adj_high, adj_low, adj_close）

    Raises:
        KeyError: stocks 表裡沒有這個代碼。

        刻意用丟例外而不是回傳空表來處理「查一檔不存在的股票」：代碼打錯
        （例如把 2330 打成 233O）如果只拿到空表，上層會算出一堆 NaN，
        看起來像「這個因子沒有訊號」而不是「程式有 bug」。
        代碼存在但區間內沒有資料，則回傳欄位正確的空表——那是正常情況。
    """
    conn, should_close = _with_connection(conn)
    try:
        if not conn.execute(
            "SELECT 1 FROM stocks WHERE stock_id = ?", (stock_id,)
        ).fetchone():
            raise KeyError(f"stocks 表裡沒有 {stock_id}；可用的代碼見 get_universe()")

        # **刻意撈整段歷史，不是只撈 [start, end]。**
        #
        # 還原價是整條序列的性質，不是單一天的性質：某一天的還原價取決於它「之後」
        # 發生過哪些事件，而事件的調整因子又要用該事件前一日的收盤價去算——
        # 那一天很可能落在查詢區間之外。例如查 2020 年，2021 年的除權息仍然要套用，
        # 但算那個因子需要 2021 年除息前一天的收盤價。
        #
        # 所以：先在完整序列上算好還原價，最後才切出要的區間。這樣
        # get_prices("2330", "2020-01-01", "2020-12-31") 跟
        # get_prices("2330").loc["2020"] 必然一致——是結構上保證，不是碰巧。
        #
        # 代價是即使只查一天也要讀完整檔歷史。以目前規模（單檔約 2800 列、
        # 全庫 10 萬列）完全不是問題；真的變慢再來考慮只讀 [start, 最新日]。
        prices = pd.read_sql(
            "SELECT date, open, high, low, close, volume, turnover "
            "FROM prices WHERE stock_id = ? ORDER BY date",
            conn, params=(stock_id,),
        )

        if prices.empty:
            empty = pd.DataFrame(
                columns=RAW_COLUMNS + (ADJUSTED_COLUMNS if adjusted else []),
                index=pd.DatetimeIndex([], name="date"),
            )
            return empty

        if adjusted:
            prices = _attach_adjusted(conn, stock_id, prices)

        out = _to_date_index(prices)
        # 成交股數用可空整數，跟 cleaner 的輸出一致。read_sql 遇到 NULL 會把整欄
        # 變成 float，成交量在 EDA 裡就會顯示成 1.23e+09 這種樣子。
        out["volume"] = out["volume"].round().astype("Int64")

        if start is not None:
            out = out[out.index >= pd.Timestamp(start)]
        if end is not None:
            out = out[out.index <= pd.Timestamp(end)]
        return out
    finally:
        if should_close:
            conn.close()


def _attach_adjusted(conn, stock_id: str, prices: pd.DataFrame) -> pd.DataFrame:
    """把 adj_* 欄位算出來接到原始價後面。

    兩種事件都要撈：
    - dividend_events：除權息，用證交所公式從股利算參考價
    - corporate_actions：減資與面額變更，官方直接公告事件前後參考價

    只撈其中一張表是階段 D 實跑時踩過的坑——2609 陽明 2017-05-04 的減資
    若不還原，回測會看成一天暴漲 116%（見 docs/data_source.md）。
    """
    dividends = pd.read_sql(
        "SELECT ex_date AS date, cash_div, stock_div "
        "FROM dividend_events WHERE stock_id = ?",
        conn, params=(stock_id,),
    )
    actions = pd.read_sql(
        "SELECT ex_date AS date, before_price, after_price "
        "FROM corporate_actions WHERE stock_id = ?",
        conn, params=(stock_id,),
    )

    adjusted = calculate_adjusted_price(prices, dividends, actions)
    # calculate_adjusted_price 會把 date 轉成 datetime，這裡先轉回字串交給
    # _to_date_index 統一處理，避免兩邊各轉一次、格式不一致
    adjusted["date"] = adjusted["date"].dt.strftime("%Y-%m-%d")
    return adjusted[["date"] + RAW_COLUMNS + ADJUSTED_COLUMNS]


def get_price_matrix(
    stock_ids: list[str] = None,
    field: str = "adj_close",
    start: str = None,
    end: str = None,
    conn=None,
) -> pd.DataFrame:
    """把多檔股票的同一個欄位排成矩陣：index = date，columns = stock_id。

    橫斷面研究（Phase 3 的因子、Phase 5 的投資組合）都需要這個形狀：
    同一天各檔股票的值排成一列，才能做排序、分組、算相關性。

    Args:
        stock_ids: 要哪幾檔。None 代表目前還在市場上的全部（get_universe()）。
        field: 要哪個欄位，預設 adj_close。算報酬用 adj_close，
            要看當時的真實掛牌價才用 close。

    Returns:
        DataFrame，index 是所有股票日期的聯集（升冪），沒有資料的格子是 NaN。

        用聯集而不是交集，是因為交集會默默砍掉資料：只要有一檔在某天停牌，
        那一整天就會從矩陣裡消失，其他 37 檔的資料也一起賠進去。
        留成 NaN 讓上層自己決定怎麼處理（丟掉、補值、或只在有資料的格子上運算）
        才是對的——這個決定不該由資料存取層偷偷做掉。
    """
    conn, should_close = _with_connection(conn)
    try:
        if stock_ids is None:
            stock_ids = get_universe(conn=conn)

        adjusted = field.startswith("adj_")
        series = {}
        for stock_id in stock_ids:
            prices = get_prices(stock_id, start, end, adjusted=adjusted, conn=conn)
            if field not in prices.columns:
                raise KeyError(
                    f"沒有 {field} 這個欄位，可用的是 "
                    f"{RAW_COLUMNS + ADJUSTED_COLUMNS}"
                )
            series[stock_id] = prices[field]

        if not series:
            return pd.DataFrame(index=pd.DatetimeIndex([], name="date"))

        matrix = pd.DataFrame(series)
        matrix.index.name = "date"
        return matrix.sort_index()
    finally:
        if should_close:
            conn.close()
