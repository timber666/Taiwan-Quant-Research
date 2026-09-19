"""
cleaner.py 的單元測試。

每一種「髒」各一個 case。這裡只測格式，不測資料對不對——
「high 比 low 低」那類是 validator 的職責（步驟 20），刻意不在這裡驗，
否則兩支程式的責任邊界會愈來愈模糊。
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from ingestion.cleaner import clean, PRICE_COLUMNS

SOURCE_COLUMNS = ["date", "open", "high", "low", "close", "volume", "turnover"]


def raw_row(date, close=100.0, volume=1000, **overrides):
    """組一列資料來源格式的日線，OHLC 預設都用同一個價格。"""
    row = {
        "date": date,
        "open": close, "high": close, "low": close, "close": close,
        "volume": volume, "turnover": 100000.0,
    }
    row.update(overrides)
    return row


def raw_frame(rows):
    return pd.DataFrame(rows, columns=SOURCE_COLUMNS)


# ---------------------------------------------------------------------------
# 日期
# ---------------------------------------------------------------------------

def test_date_becomes_iso_string():
    """日期一律轉成 YYYY-MM-DD 字串。

    prices.date 是 TEXT，而且之後大量的區間比較（plan_downloads、data_access）
    都直接拿字串比大小——字典序對 ISO 格式等同時間序，但前提是格式統一。
    """
    df = raw_frame([raw_row(pd.Timestamp("2024-03-18"))])

    out = clean(df, "2330")

    assert out.loc[0, "date"] == "2024-03-18"
    assert isinstance(out.loc[0, "date"], str)


def test_mixed_date_formats_are_all_parsed():
    """同一批資料裡混了兩種日期寫法，兩種都要解析出來。

    這是實際踩到的回歸測試：pandas 2.x 的 to_datetime 預設會用第一列推斷出
    一種格式再套用到全部，於是斜線寫法夾在破折號寫法之間時會變成 NaT
    被當壞資料丟掉，而且程式不會報錯。cleaner 指定 format="mixed" 才正確。
    """
    df = raw_frame([raw_row("2024-03-20"), raw_row("2024/03/19"), raw_row("2024-03-18")])

    out = clean(df, "2330")

    assert list(out["date"]) == ["2024-03-18", "2024-03-19", "2024-03-20"]


def test_unparseable_date_is_dropped_with_a_warning(capsys):
    """日期壞掉的列只能丟棄，但不能無聲無息。

    prices 和 data_quarantine 的主鍵都含 date，沒有日期的列連隔離都做不到
    ——存不進去、之後也查不回來。所以丟，但一定要印出來讓人知道丟了幾列。
    """
    df = raw_frame([raw_row("2024-03-18"), raw_row("not-a-date"), raw_row("2024-03-19")])

    out = clean(df, "2330")

    assert list(out["date"]) == ["2024-03-18", "2024-03-19"]      # 其他列不受影響
    assert "2330" in capsys.readouterr().out                      # 有講出來


# ---------------------------------------------------------------------------
# 數值型別
# ---------------------------------------------------------------------------

def test_numeric_strings_are_converted():
    """字串型別的數字要轉成數值，否則之後所有算術都會出錯或整欄變 object"""
    df = raw_frame([raw_row("2024-03-18", close="100.5", volume="1000")])

    out = clean(df, "2330")

    assert out.loc[0, "close"] == pytest.approx(100.5)
    assert out.loc[0, "volume"] == 1000


def test_thousand_separators_are_stripped():
    """千分位逗號要先拔掉再轉數值。

    FinMind 回的本來就是數值，但 docs/data_source.md 把 TWSE OpenAPI 列為
    交叉驗證來源，而它回的是帶逗號的字串。不處理的話整欄會默默變 NaN
    ——屬於「換個來源資料就整批消失、但程式不報錯」的那種坑。
    """
    df = raw_frame([raw_row("2024-03-18", close="1,234.5", volume="1,500",
                            turnover="1,851,750")])

    out = clean(df, "2330")

    assert out.loc[0, "close"] == pytest.approx(1234.5)
    assert out.loc[0, "volume"] == 1500
    assert out.loc[0, "turnover"] == pytest.approx(1851750.0)


def test_unconvertible_values_become_missing_instead_of_raising():
    """轉不動的值設成缺值，不要拋例外。

    一列壞掉不該把整檔股票幾千列的下載成果一起丟掉。這種列會在下一關
    （validator）被擋進隔離區，那裡才是判斷「這筆資料能不能用」的地方。
    """
    df = raw_frame([raw_row("2024-03-18", close="N/A", volume="")])

    out = clean(df, "2330")

    assert pd.isna(out.loc[0, "close"])
    assert pd.isna(out.loc[0, "volume"])


def test_volume_is_nullable_integer():
    """成交股數是股數，要用整數型別。

    用 float 存的話，EDA 裡會變成 1.23e+09 這種樣子。用 pandas 的 Int64
    （可空整數）而不是 numpy int64，才能同時表達「沒抓到」——
    步驟 22 的 loader 要記得把 pd.NA 轉成 None，sqlite3 不認得 pd.NA。
    """
    df = raw_frame([raw_row("2024-03-18", volume=1000), raw_row("2024-03-19", volume=None)])

    out = clean(df, "2330")

    assert out["volume"].dtype == "Int64"
    assert out.loc[0, "volume"] == 1000
    assert out.loc[1, "volume"] is pd.NA


# ---------------------------------------------------------------------------
# 去重與排序
# ---------------------------------------------------------------------------

def test_duplicate_dates_keep_the_later_input_row():
    """同一天出現兩次時，保留輸入裡比較後面那一筆（比較晚抓到、比較新的）。

    輸入刻意排成亂序：去重必須在排序「之前」做，keep="last" 的語意才是
    「輸入裡較後面」。先排序的話，pandas 的 sort_values 預設不是穩定排序，
    同一天的幾筆先後會被打亂，keep="last" 就變成隨機挑一筆。
    """
    df = raw_frame([
        raw_row("2024-03-20", close=20.0),
        raw_row("2024-03-18", close=18.0),      # 先出現的，應該被蓋掉
        raw_row("2024-03-19", close=19.0),
        raw_row("2024-03-18", close=99.0),      # 後出現的，應該留下
    ])

    out = clean(df, "2330")

    assert len(out) == 3
    assert out.set_index("date").loc["2024-03-18", "close"] == pytest.approx(99.0)


def test_duplicates_are_detected_after_date_normalisation():
    """去重要在日期正規化之後——同一天的兩種寫法算同一天。

    順序反過來的話，這兩列會被當成不同日期各留一份，
    寫進 prices 時才被主鍵擋下來，問題發生的地方離原因很遠。
    """
    df = raw_frame([raw_row("2024-03-18", close=18.0), raw_row("2024/03/18", close=99.0)])

    out = clean(df, "2330")

    assert len(out) == 1
    assert out.loc[0, "close"] == pytest.approx(99.0)


def test_rows_are_sorted_by_date_ascending_with_reset_index():
    """依日期升冪，index 重編。

    index 不重編的話，呼叫端用 .loc[0] 取值時拿到的東西會跟預期不同。
    """
    df = raw_frame([raw_row("2024-03-20"), raw_row("2024-03-18"), raw_row("2024-03-19")])

    out = clean(df, "2330")

    assert list(out["date"]) == ["2024-03-18", "2024-03-19", "2024-03-20"]
    assert list(out.index) == [0, 1, 2]


# ---------------------------------------------------------------------------
# 欄位
# ---------------------------------------------------------------------------

def test_stock_id_and_source_are_added():
    df = raw_frame([raw_row("2024-03-18")])

    out = clean(df, "2330")

    assert out.loc[0, "stock_id"] == "2330"
    assert out.loc[0, "source"] == "FinMind"


def test_source_can_be_overridden():
    """source 是參數不是寫死的：cleaner 跟資料來源無關，加 TWSE 備援時不該改這支程式"""
    df = raw_frame([raw_row("2024-03-18")])

    out = clean(df, "2330", source="TWSE")

    assert out.loc[0, "source"] == "TWSE"


def test_stock_id_from_caller_wins_over_the_one_in_the_data():
    """來源自己帶的 stock_id 不算數，以呼叫端要查的那一檔為準。

    查 2330 卻回了別的代碼這種事一旦發生，資料會被記到錯的股票底下，
    而且非常難查。以 plan_downloads 排定的為準才對得起上游。
    """
    df = raw_frame([raw_row("2024-03-18")])
    df["stock_id"] = "9999"

    out = clean(df, "2330")

    assert set(out["stock_id"]) == {"2330"}


def test_columns_match_the_prices_table():
    """欄位與順序要對齊 prices 表，loader 才能直接 executemany"""
    df = raw_frame([raw_row("2024-03-18")])

    out = clean(df, "2330")

    assert list(out.columns) == PRICE_COLUMNS
    assert "adj_close" not in out.columns      # 步驟 16 拿掉了，還原價是讀取時算的


def test_missing_required_column_raises():
    """缺欄位代表資料來源沒遵守 PriceSource 契約，是程式的錯不是資料髒，直接擋下來"""
    df = raw_frame([raw_row("2024-03-18")]).drop(columns=["turnover"])

    with pytest.raises(ValueError, match="turnover"):
        clean(df, "2330")


# ---------------------------------------------------------------------------
# 邊界
# ---------------------------------------------------------------------------

def test_empty_frame_keeps_the_schema():
    """沒有資料時要回傳「欄位正確但沒有列」的 DataFrame，不是 None。

    呼叫端（步驟 22 的 orchestrator）才能無腦往下傳給 validator，不用先檢查 None。
    """
    out = clean(raw_frame([]), "2330")

    assert len(out) == 0
    assert list(out.columns) == PRICE_COLUMNS


def test_input_frame_is_not_modified():
    """不要動到呼叫端傳進來的 DataFrame。

    download_all 的結果之後還會被拿去做別的事（印摘要、除錯時重看），
    被 clean 就地改掉的話會出現「印出來的跟存進去的不一樣」這種難查的問題。
    """
    df = raw_frame([raw_row("2024-03-18", close="100.5")])
    before = df.copy()

    clean(df, "2330")

    pd.testing.assert_frame_equal(df, before)
