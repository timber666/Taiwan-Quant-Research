"""
data_access.py 的單元測試。

用暫存資料庫加手工編的資料，不碰正式的 quant.db——測試要能講出「為什麼答案是
這個數字」，拿真實資料只能驗證「跟上次跑出來一樣」。

整組測試的重點是 **get_prices 的切窗一致性**：同一檔股票在不同查詢區間下，
還原價必須完全一樣。這是最容易寫錯、而且錯了以後回測報酬會默默失真的地方。
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from config.settings import BASE_DIR
from database.connection import get_connection
from database.data_access import get_price_matrix, get_prices, get_universe

# 手工資料：收盤價一律 100，這樣還原價的數字就是「調整因子 × 100」，可以心算驗證。
#
#   2021-06-01 除息 10 元       -> 因子 (100 - 10) / 100 = 0.9
#   2021-12-01 減資 100 -> 200  -> 因子 200 / 100 = 2.0
#
# 後還原是把「該日之後」的因子累乘（事件當天自己不算），所以：
#   2021-06-01 之前   0.9 × 2.0 = 1.8  -> 180
#   2021-06-01 當天         2.0       -> 200
#   2021-12-01 當天起        1.0       -> 100
PRICE_DATES = ["2020-01-02", "2020-06-15", "2020-12-30",
               "2021-06-01", "2021-12-01", "2021-12-02"]
EXPECTED_ADJ = [180.0, 180.0, 180.0, 200.0, 100.0, 100.0]


@pytest.fixture
def conn(tmp_path):
    """一個裝了手工資料的暫存資料庫。

    2330 有完整的價格與兩種事件，2317 只有部分日期（測 price matrix 的聯集），
    1111 在 stocks 裡但沒有任何價格，9999 已下市。
    """
    c = get_connection(tmp_path / "test.db")
    c.executescript((BASE_DIR / "database" / "schema.sql").read_text(encoding="utf-8"))

    c.executemany(
        "INSERT INTO stocks (stock_id, name, is_active) VALUES (?, ?, ?)",
        [("2330", "台積電", 1), ("2317", "鴻海", 1), ("1111", "沒有價格", 1),
         ("9999", "已下市", 0)],
    )
    c.executemany(
        "INSERT INTO prices (stock_id, date, open, high, low, close, volume, turnover, source)"
        " VALUES (?, ?, 100, 100, 100, 100, 1000, 100000, 'test')",
        [("2330", d) for d in PRICE_DATES],
    )
    # 2317 只有其中兩天，而且有一天是 2330 沒有的
    c.executemany(
        "INSERT INTO prices (stock_id, date, open, high, low, close, volume, turnover, source)"
        " VALUES (?, ?, 50, 50, 50, 50, 2000, 100000, 'test')",
        [("2317", "2020-01-02"), ("2317", "2020-07-01")],
    )
    c.execute(
        "INSERT INTO dividend_events (stock_id, ex_date, cash_div, stock_div) VALUES"
        " ('2330', '2021-06-01', 10, 0)"
    )
    c.execute(
        "INSERT INTO corporate_actions (stock_id, ex_date, action_type, before_price, after_price)"
        " VALUES ('2330', '2021-12-01', 'capital_reduction', 100, 200)"
    )
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# get_prices：形狀與欄位
# ---------------------------------------------------------------------------

def test_returns_datetime_index_sorted_ascending(conn):
    """index 要是 DatetimeIndex，之後的 .loc["2020"]、resample、rolling 才用得了"""
    out = get_prices("2330", conn=conn)

    assert isinstance(out.index, pd.DatetimeIndex)
    assert out.index.name == "date"
    assert list(out.index) == sorted(out.index)


def test_adjusted_flag_changes_the_columns(conn):
    """adjusted=False 只給原始價，True 才多出 adj_* 四欄"""
    raw = get_prices("2330", adjusted=False, conn=conn)
    adj = get_prices("2330", adjusted=True, conn=conn)

    assert list(raw.columns) == ["open", "high", "low", "close", "volume", "turnover"]
    assert list(adj.columns) == [
        "open", "high", "low", "close", "volume", "turnover",
        "adj_open", "adj_high", "adj_low", "adj_close",
    ]


def test_volume_is_a_nullable_integer(conn):
    """成交股數是股數。read_sql 遇到 NULL 會把整欄變 float，
    EDA 裡就會顯示成 1.23e+09——轉回 Int64 跟 cleaner 的輸出一致。"""
    out = get_prices("2330", conn=conn)

    assert out["volume"].dtype == "Int64"


# ---------------------------------------------------------------------------
# get_prices：日期過濾
# ---------------------------------------------------------------------------

def test_start_and_end_are_both_inclusive(conn):
    out = get_prices("2330", "2020-06-15", "2021-06-01", conn=conn)

    assert [d.strftime("%Y-%m-%d") for d in out.index] == [
        "2020-06-15", "2020-12-30", "2021-06-01"
    ]


def test_start_alone_keeps_everything_after_it(conn):
    out = get_prices("2330", start="2021-01-01", conn=conn)

    assert len(out) == 3


def test_no_bounds_returns_the_whole_history(conn):
    assert len(get_prices("2330", conn=conn)) == len(PRICE_DATES)


# ---------------------------------------------------------------------------
# 還原價（這組是重點）
# ---------------------------------------------------------------------------

def test_dividend_and_corporate_action_are_both_applied(conn):
    """兩種事件都要算進去，而且是累乘。

    只撈 dividend_events 的話，2021-12-01 的減資不會被還原——那正是階段 D
    實跑時踩到的坑（2609 陽明 2017 年的減資會被看成一天暴漲 116%）。
    """
    out = get_prices("2330", conn=conn)

    assert list(out["adj_close"]) == pytest.approx(EXPECTED_ADJ)


def test_window_query_matches_the_same_slice_of_the_full_history(conn):
    """**整組測試最重要的一條。**

    同一檔股票在不同查詢區間下，還原價必須完全一樣。不一致的話，回測換個
    區間重跑就會得到不同的報酬，而且不會有任何錯誤訊息。
    """
    window = get_prices("2330", "2020-01-01", "2020-12-31", conn=conn)["adj_close"]
    sliced = get_prices("2330", conn=conn).loc["2020", "adj_close"]

    assert window.equals(sliced)


def test_events_after_the_window_still_adjust_it(conn):
    """查詢區間**之後**發生的事件一樣要套用——這是上一條為什麼會錯的原因。

    2020 年這段裡沒有任何事件，但 2021 年的除息與減資都必須反映在 2020 年的
    還原價上（0.9 × 2.0 = 1.8 -> 180）。只撈 [start, end] 區間內事件的實作
    會回傳 100，而且看起來一點問題都沒有。
    """
    out = get_prices("2330", "2020-01-01", "2020-12-31", conn=conn)

    assert list(out["adj_close"]) == pytest.approx([180.0, 180.0, 180.0])
    assert list(out["close"]) == pytest.approx([100.0, 100.0, 100.0])   # 原始價不受影響


def test_latest_day_is_never_adjusted(conn):
    """後還原的定義性檢查：最新一天的還原價恆等於原始收盤價。

    這條若不成立就代表方向搞反成前還原了。
    """
    out = get_prices("2330", conn=conn)

    assert out["adj_close"].iloc[-1] == pytest.approx(out["close"].iloc[-1])


def test_all_four_ohlc_fields_are_adjusted(conn):
    """adj_open/high/low 也要跟著調整，Phase 2 若要做開盤價進場會用到"""
    out = get_prices("2330", "2020-01-02", "2020-01-02", conn=conn)

    for field in ["adj_open", "adj_high", "adj_low", "adj_close"]:
        assert out[field].iloc[0] == pytest.approx(180.0)


def test_stock_without_events_is_unadjusted(conn):
    """沒有任何除權息或公司行動時，還原價就等於原始價（大多數股票的多數區間）"""
    out = get_prices("2317", conn=conn)

    assert list(out["adj_close"]) == pytest.approx(list(out["close"]))


# ---------------------------------------------------------------------------
# get_prices：錯誤與邊界
# ---------------------------------------------------------------------------

def test_unknown_stock_id_raises(conn):
    """代碼打錯要出聲，不要回傳空表。

    空表會讓上層算出一堆 NaN，看起來像「這個因子沒有訊號」而不是「程式有 bug」。
    """
    with pytest.raises(KeyError, match="233O"):
        get_prices("233O", conn=conn)


def test_known_stock_without_prices_returns_an_empty_frame(conn):
    """代碼存在但沒有價格是正常情況（剛加進 universe、還沒抓資料），
    要回傳欄位正確的空表讓上層可以無腦往下傳"""
    out = get_prices("1111", conn=conn)

    assert len(out) == 0
    assert "adj_close" in out.columns
    assert isinstance(out.index, pd.DatetimeIndex)


def test_window_with_no_data_returns_an_empty_frame(conn):
    out = get_prices("2330", "1990-01-01", "1990-12-31", conn=conn)

    assert len(out) == 0
    assert list(out.columns) == [
        "open", "high", "low", "close", "volume", "turnover",
        "adj_open", "adj_high", "adj_low", "adj_close",
    ]


# ---------------------------------------------------------------------------
# get_universe
# ---------------------------------------------------------------------------

def test_universe_excludes_delisted_by_default(conn):
    assert get_universe(conn=conn) == ["1111", "2317", "2330"]


def test_universe_can_include_delisted(conn):
    """回測要避免存活者偏誤時需要已下市的股票：
    只用今天還活著的股票回測，績效會虛高"""
    assert get_universe(active_only=False, conn=conn) == ["1111", "2317", "2330", "9999"]


# ---------------------------------------------------------------------------
# get_price_matrix
# ---------------------------------------------------------------------------

def test_matrix_has_dates_as_index_and_stocks_as_columns(conn):
    m = get_price_matrix(["2330", "2317"], conn=conn)

    assert list(m.columns) == ["2330", "2317"]
    assert isinstance(m.index, pd.DatetimeIndex)
    assert m.index.name == "date"


def test_matrix_uses_the_union_of_dates_not_the_intersection(conn):
    """任一檔沒有資料的日子不該把整天砍掉。

    交集的話，只要有一檔停牌，那一整天就會從矩陣裡消失，其他股票的資料也
    一起賠進去。留成 NaN，讓上層自己決定要丟掉還是補值。
    """
    m = get_price_matrix(["2330", "2317"], conn=conn)

    # 2330 有 6 天、2317 有 2 天，其中 2020-01-02 重疊 -> 聯集 7 天
    assert len(m) == 7
    assert m.loc[pd.Timestamp("2020-07-01"), "2330"] != m.loc[pd.Timestamp("2020-07-01"), "2330"]  # NaN
    assert m.loc[pd.Timestamp("2020-07-01"), "2317"] == pytest.approx(50.0)


def test_matrix_defaults_to_the_whole_active_universe(conn):
    """不指定股票就是目前還在市場上的全部——橫斷面研究通常就是要這個"""
    m = get_price_matrix(conn=conn)

    assert set(m.columns) == {"1111", "2317", "2330"}


def test_matrix_can_use_raw_close(conn):
    """要看當時真實掛牌價時用 close，這時不該做還原"""
    m = get_price_matrix(["2330"], field="close", conn=conn)

    assert list(m["2330"]) == pytest.approx([100.0] * 6)


def test_matrix_adjusted_field_is_actually_adjusted(conn):
    """預設的 adj_close 要真的是還原價，別跟 close 搞混"""
    m = get_price_matrix(["2330"], conn=conn)

    assert list(m["2330"]) == pytest.approx(EXPECTED_ADJ)


def test_raw_field_does_not_run_the_adjustment(conn, monkeypatch):
    """要 close 這種原始欄位時，不該白跑一遍還原計算。

    matrix 會逐檔呼叫 get_prices，全 universe 就是幾十次，每次都多算一次
    用不到的還原價是純浪費。這件事比對輸出看不出來——還原與否 close 欄位都在、
    值也一樣——所以改用「把還原函式換成一呼叫就讓測試失敗的版本」來驗證。
    """
    import database.data_access as da
    monkeypatch.setattr(
        da, "calculate_adjusted_price",
        lambda *a, **k: pytest.fail("要 close 不該去算還原價"),
    )

    m = get_price_matrix(["2330"], field="close", conn=conn)

    assert list(m["2330"]) == pytest.approx([100.0] * 6)


def test_matrix_respects_the_date_window(conn):
    m = get_price_matrix(["2330"], start="2021-01-01", conn=conn)

    assert len(m) == 3


def test_matrix_rejects_an_unknown_field_with_a_useful_message(conn):
    """欄位打錯時，錯誤訊息要列出有哪些可用欄位。

    只斷言「有丟 KeyError」是不夠的——pandas 自己取不到欄位時也會丟 KeyError，
    但訊息裡只有打錯的那個名字，看不出正確的該長什麼樣。
    """
    with pytest.raises(KeyError, match="adj_close"):
        get_price_matrix(["2330"], field="close_price", conn=conn)


def test_matrix_with_no_stocks_returns_an_empty_frame(conn):
    m = get_price_matrix([], conn=conn)

    assert len(m) == 0
    assert isinstance(m.index, pd.DatetimeIndex)
