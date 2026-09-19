"""
validator.py 的單元測試。

每條規則一個 case，price_jump 另外寫「除權息日 / 非除權息日」的對照組——
那組是整個 validator 最容易寫錯的地方：不排除除權息日的話，quarantine 會被
正常的除權息跳空洗版，真正需要人看的異常反而被埋掉。
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from ingestion.cleaner import PRICE_COLUMNS
from ingestion.validator import validate, PRICE_JUMP_THRESHOLD


def row(date, close=100.0, **overrides):
    """組一列 cleaner 輸出格式的日線；預設是一筆完全正常的資料。

    OHLC 預設全部等於 close，這樣只調 close 也不會造出「high 比 close 低」
    這種自相矛盾的列——要測 OHLC 的規則時再明確指定 open/high/low。
    """
    base = {
        "stock_id": "2330", "date": date,
        "open": close, "high": close, "low": close, "close": close,
        "volume": 1000, "turnover": 100000.0, "source": "FinMind",
    }
    base.update(overrides)
    return base


def frame(rows):
    """組一份 cleaner 輸出格式的 DataFrame。

    volume 一定要轉成 Int64：cleaner 產出的就是這個型別，而它跟一般 int64 的
    差別會實際影響 validator 的行為（對缺值比較會得到 pd.NA 而不是 False）。
    測資如果用預設推斷出來的 object/int64，那條路徑在測試裡根本不會被走到。
    """
    return pd.DataFrame(rows, columns=PRICE_COLUMNS).astype({"volume": "Int64"})


def reasons_on(bad, date):
    """某一天被記了哪些 reason"""
    return set(bad.loc[bad["date"] == date, "reason"])


def dates_in(df):
    return set(df["date"])


# ---------------------------------------------------------------------------
# 致命規則：資料自相矛盾，不寫進 prices
# ---------------------------------------------------------------------------

def test_missing_price_is_its_own_reason():
    """價格有缺值時只報 missing_price，不要連帶報一堆 OHLC 的假問題。

    NaN 參與比較一律得到 False，所以如果不先把缺值的列分出來，
    high >= close 這種檢查會「通過」，而 low > close 之類的判成 False 看起來沒事，
    真正的問題（根本沒有價格）反而被蓋掉。
    """
    bad = validate(frame([row("2024-03-11", open=np.nan, high=np.nan)]))[1]

    assert reasons_on(bad, "2024-03-11") == {"missing_price"}


def test_missing_price_suppresses_the_other_ohlc_reasons():
    """價格不完整時只報 missing_price，即使剩下的欄位彼此也對不起來。

    這一列 open 缺值，而 high=95 比 low=99 還低。沒有「只在價格完整的列上檢查
    OHLC」這層保護的話，同一列會同時報 missing_price 和 high_lt_low。
    這列反正已經被擋掉了，多報的 reason 只會讓 quarantine 的統計變難讀。

    （上一個測試分辨不出這件事：NaN 參與比較一律得到 False，
    所以價格「全部」缺值時，有沒有保護的結果剛好一樣。）
    """
    bad = validate(frame([row("2024-03-11", open=np.nan, high=95.0, low=99.0)]))[1]

    assert reasons_on(bad, "2024-03-11") == {"missing_price"}


def test_high_lower_than_low_is_rejected():
    good, bad = validate(frame([row("2024-03-11", high=95.0, low=99.0)]))

    assert "high_lt_low" in reasons_on(bad, "2024-03-11")
    assert good.empty                                   # 致命 -> 不寫進 prices


def test_high_lower_than_open_is_flagged_but_kept():
    """open 對不上只標記、不隔離——理由見 validator.OHLC_RULES 的註解。

    這種列的 close/high/low 本身是合理的，而回測用的是 close。
    為了一個怪的 open 把整天丟掉，會在序列中間開洞，
    而缺一天會讓 t-1 到 t+1 被當成一天的報酬，比留著那個 open 危險得多。
    """
    # close/high/low 彼此相容，只有 open 落在區間外——這正是興櫃報價的形狀
    good, bad = validate(frame([row("2024-03-11", open=100.0, high=99.5,
                                    low=99.0, close=99.0)]))

    assert reasons_on(bad, "2024-03-11") == {"high_lt_open"}
    assert dates_in(good) == {"2024-03-11"}      # 照樣寫進 prices


def test_high_lower_than_close_is_rejected():
    """close 對不上就是致命的——回測算報酬用的就是 close，不能信的話整列都不能用"""
    good, bad = validate(frame([row("2024-03-11", high=99.5, open=99.0, close=100.0, low=99.0)]))

    assert "high_lt_close" in reasons_on(bad, "2024-03-11")
    assert good.empty


def test_low_higher_than_open_is_flagged_but_kept():
    """同上：低價高於開盤價也只標記"""
    good, bad = validate(frame([row("2024-03-11", open=100.0, high=101.0,
                                    low=100.5, close=101.0)]))

    assert reasons_on(bad, "2024-03-11") == {"low_gt_open"}
    assert dates_in(good) == {"2024-03-11"}


def test_low_higher_than_close_is_rejected():
    good, bad = validate(frame([row("2024-03-11", low=100.5, open=101.0, close=100.0, high=101.0)]))

    assert "low_gt_close" in reasons_on(bad, "2024-03-11")
    assert good.empty


def test_non_positive_price_is_rejected():
    """股價不可能是 0 或負數——出現就是資料源或解析出問題"""
    good, bad = validate(frame([row("2024-03-11", low=-5.0, open=-1.0)]))

    assert "non_positive_price" in reasons_on(bad, "2024-03-11")
    assert good.empty


def test_negative_volume_is_rejected():
    good, bad = validate(frame([row("2024-03-11", volume=-5)]))

    assert "negative_volume" in reasons_on(bad, "2024-03-11")
    assert good.empty


def test_missing_volume_is_not_treated_as_negative():
    """成交量缺值不致命，不該被標成 negative_volume，更不該讓整列消失。

    檢查刻意寫成 volume < 0 而不是 not (volume >= 0)：後者會把缺值也算進來。
    prices.volume 允許 NULL，而且價格本身還是可用的，所以放行。

    這條同時是一個實際踩到的 bug 的回歸測試：cleaner 的 volume 是 Int64，
    `volume < 0` 對缺值回傳 pd.NA 而不是 False；那個 NA 傳進 rejected 之後，
    `~pd.NA` 還是 NA，而 pandas 的布林索引把 NA 當 False——結果那一列
    **既不在 good_df、也不在 bad_df，整列無聲無息消失**。
    所以這裡要同時斷言「沒被標記」和「還在 good_df 裡」。
    """
    good, bad = validate(frame([row("2024-03-11", volume=pd.NA)]))

    assert bad.empty
    assert dates_in(good) == {"2024-03-11"}


def test_duplicate_dates_are_both_quarantined():
    """重複日期時兩筆都隔離，不要默默挑一筆。

    cleaner 已經去過重，這條照理不會命中。真的命中代表上游出事了，
    而「默默挑一筆」正是上游會出問題的原因，所以兩筆都送去讓人看。
    """
    good, bad = validate(frame([row("2024-03-11", close=100.0), row("2024-03-11", close=200.0)]))

    assert len(bad.loc[bad["reason"] == "duplicate_date"]) == 2
    assert good.empty


def test_one_row_can_produce_several_reasons():
    """一列違反幾條規則就記幾筆。

    data_quarantine 的主鍵是 (stock_id, date, reason)，拆開記步驟 26 才能直接
    GROUP BY reason 看分布；擠成一個字串的話還要自己切字串。
    """
    bad = validate(frame([row("2024-03-11", high=95.0, low=99.0, open=100.0, close=100.0)]))[1]

    assert reasons_on(bad, "2024-03-11") == {"high_lt_low", "high_lt_open", "high_lt_close"}


# ---------------------------------------------------------------------------
# price_jump：標記但不隔離，而且要避開除權息日
# ---------------------------------------------------------------------------

def test_price_jump_is_flagged_but_still_written():
    """漲跌過大只是「值得看一眼」，不是「資料錯了」。

    減資、面額變更、停牌後復牌都會造成大跳空，而那些價格是真的。
    隔離掉的話回測會憑空少一天資料，比留著更危險。
    """
    good, bad = validate(frame([row("2024-03-11", close=100.0), row("2024-03-12", close=70.0)]))

    assert reasons_on(bad, "2024-03-12") == {"price_jump"}
    assert "2024-03-12" in dates_in(good)          # 照樣寫進 prices


def test_price_jump_is_skipped_on_ex_dividend_dates():
    """除權息當天的跳空是市場正常行為，不該被標記。

    6669 緯穎 2026-09-02 從 7800 掉到 2610（-66%）、2409 友達每年除息都會觸發。
    不排除的話 quarantine 會被除權息洗版，真正的異常反而被埋掉。
    """
    df = frame([row("2024-03-11", close=100.0), row("2024-03-12", close=70.0)])

    good, bad = validate(df, ex_dates={"2024-03-12"})

    assert bad.empty
    assert dates_in(good) == {"2024-03-11", "2024-03-12"}


def test_only_the_listed_ex_dates_are_skipped():
    """只有除權息當天豁免，其他日子的跳空照標。

    這條是上一個測試的對照組：如果實作寫成「有 ex_dates 就整批不檢查」，
    上面那個測試也會通過，但保護就整個沒了。
    """
    df = frame([
        row("2024-03-11", close=100.0),
        row("2024-03-12", close=70.0),    # 除息日，豁免
        row("2024-03-13", close=49.0),    # 不是除息日，要標
    ])

    bad = validate(df, ex_dates={"2024-03-12"})[1]

    assert dates_in(bad) == {"2024-03-13"}


def test_jump_exactly_at_the_threshold_is_not_flagged():
    """臨界值本身不算異常（規則是「超過」15%，不是「達到」）。

    100 -> 115 刻意寫成字面值而不是 100 * (1 + PRICE_JUMP_THRESHOLD)：
    後者算出來是 114.99999999999999，漲幅 0.1499999...，根本沒踩在臨界點上，
    於是 > 改成 >= 這種差一格的錯誤測不出來。下面那行 assert 是保險——
    哪天門檻常數改了，它會直接失敗提醒這個測資要跟著換，
    而不是默默變成一個測不到東西的測試。
    """
    assert abs(115.0 - 100.0) / 100.0 == PRICE_JUMP_THRESHOLD

    df = frame([row("2024-03-11", close=100.0), row("2024-03-12", close=115.0)])

    assert validate(df)[1].empty


def test_first_row_has_no_previous_day_to_compare():
    """一批資料的第一列沒有前一日可比，不該被標記。

    增量更新每次只抓幾天，第一列的前一日在上一批裡——那不是跳空，是視窗邊界。
    """
    assert validate(frame([row("2024-03-11", close=100.0)]))[1].empty


def test_zero_previous_close_does_not_blow_up():
    """前一日收盤為 0 時不能拿去當分母，否則漲跌幅會變成 inf"""
    df = frame([row("2024-03-11", close=0.0), row("2024-03-12", close=70.0)])

    bad = validate(df)[1]

    assert "price_jump" not in set(bad["reason"])
    assert "non_positive_price" in set(bad["reason"])      # 0 元那列本身還是要擋


def test_price_jump_follows_date_order_not_row_order():
    """漲跌幅要照日期順序算，不是照列順序。

    cleaner 會排序，但 validator 不該把正確性押在「呼叫端一定先做過某件事」上。
    """
    df = frame([row("2024-03-12", close=70.0), row("2024-03-11", close=100.0)])

    bad = validate(df)[1]

    assert dates_in(bad) == {"2024-03-12"}      # 跳空記在後面那天，不是前面那天


# ---------------------------------------------------------------------------
# 分流與邊界
# ---------------------------------------------------------------------------

def test_clean_rows_pass_through_untouched():
    df = frame([row("2024-03-11"), row("2024-03-12")])

    good, bad = validate(df)

    assert list(good.columns) == PRICE_COLUMNS
    assert dates_in(good) == {"2024-03-11", "2024-03-12"}
    assert bad.empty and list(bad.columns) == PRICE_COLUMNS + ["reason"]


def test_bad_rows_do_not_take_good_ones_with_them():
    """一列壞掉只擋那一列，同一批的其他天照樣進 prices"""
    df = frame([row("2024-03-11"), row("2024-03-12", high=95.0, low=99.0), row("2024-03-13")])

    good, _ = validate(df)

    assert dates_in(good) == {"2024-03-11", "2024-03-13"}


def test_empty_frame_keeps_the_schema():
    good, bad = validate(frame([]))

    assert len(good) == 0 and list(good.columns) == PRICE_COLUMNS
    assert len(bad) == 0 and "reason" in bad.columns


def test_missing_required_column_raises():
    """缺欄位代表沒接在 cleaner 後面，是程式的錯，直接擋下來"""
    df = frame([row("2024-03-11")]).drop(columns=["volume"])

    with pytest.raises(ValueError, match="volume"):
        validate(df)


def test_input_frame_is_not_modified():
    df = frame([row("2024-03-11", close=100.0), row("2024-03-12", close=70.0)])
    before = df.copy()

    validate(df)

    pd.testing.assert_frame_equal(df, before)
