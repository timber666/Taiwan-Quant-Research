"""
calculate_adjusted_price 的單元測試。

還原價原本是在 FinMindSource.fetch_prices 裡算的，所以測試也掛在
tests/test_finmind_source.py。階段 D 步驟 16 把還原計算從資料來源層拿掉之後
（理由見 database/schema.sql 裡 prices 的註解），這個函式跟 FinMind 已經完全無關，
測試就跟著搬到這裡，直接對 calculate_adjusted_price 本身驗證。

它從步驟 24 起會是**所有**還原價的唯一產出點——回測拿到的每一個報酬數字都經過
這裡，所以寧可測得細一點。能對到官方 reference_price 的案例一律拿真實數字來測，
不要用自己算出來的答案當標準答案（那只是把同一個公式抄兩遍）。
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from ingestion.adjustment import calculate_adjusted_price

DIV_COLUMNS = ["date", "cash_div", "stock_div"]


def price_frame(rows):
    """rows 是 (date, close) 的序列；OHLC 都用同一個價格，測試只關心 close。"""
    return pd.DataFrame([
        {"date": d, "open": c, "high": c, "low": c, "close": c} for d, c in rows
    ])


def adjusted_close(df_price, df_div, df_actions=None):
    out = calculate_adjusted_price(df_price, df_div, df_actions)
    return out.assign(date=out["date"].dt.strftime("%Y-%m-%d")).set_index("date")["adj_close"]


# ---------------------------------------------------------------------------
# 對得到官方參考價的真實案例
# ---------------------------------------------------------------------------

def test_cash_dividend_matches_official_reference_price():
    """2409 友達 2023-08-10 除息 0.80（全部配在公積）。

    前一日還原價 = (20.40 - 0.80) / 1 = 19.60，跟官方 reference_price 一致；
    除息日當天不打折，維持 18.95。
    """
    prices = price_frame([
        ("2023-08-08", 20.40),
        ("2023-08-09", 20.40),
        ("2023-08-10", 18.95),
        ("2023-08-11", 18.50),
    ])
    divs = pd.DataFrame([{"date": "2023-08-10", "cash_div": 0.80, "stock_div": 0.0}])

    adj = adjusted_close(prices, divs)

    assert adj["2023-08-08"] == pytest.approx(19.60)
    assert adj["2023-08-09"] == pytest.approx(19.60)
    assert adj["2023-08-10"] == pytest.approx(18.95)
    assert adj["2023-08-11"] == pytest.approx(18.50)


def test_large_stock_dividend_matches_official_reference_price():
    """6669 緯穎 2026-09-02 純除權，每 10 股配 19.827946 股。

    股價從 7800 跳到 2610（-66%），但那不是真的暴跌。
    還原後前一日 = 7800 / (1 + 1.9827946) = 2615.00，官方 reference_price 2614.99。
    這種大額配股是最容易把回測績效算爛的情況，所以特別留一個案例。
    """
    prices = price_frame([("2026-09-01", 7800.0), ("2026-09-02", 2610.0)])
    divs = pd.DataFrame([{"date": "2026-09-02", "cash_div": 0.0, "stock_div": 19.827946}])

    adj = adjusted_close(prices, divs)

    assert adj["2026-09-01"] == pytest.approx(2614.99, abs=0.05)
    assert adj["2026-09-02"] == pytest.approx(2610.0)


# ---------------------------------------------------------------------------
# 減資與面額變更（步驟 23 實跑後才發現漏掉的一整類事件）
# ---------------------------------------------------------------------------

def test_capital_reduction_is_adjusted():
    """2609 陽明 2017-05-04 減資（彌補虧損），官方公告 6.15 -> 13.16。

    不還原的話，回測會把這天看成一天暴漲 116%——那是股數變少造成的，
    持有人的資產並沒有變多。這是步驟 23 的 price_jump 標記把它揪出來的。
    """
    prices = price_frame([("2017-05-03", 6.15), ("2017-05-04", 13.15)])
    actions = pd.DataFrame([{"date": "2017-05-04", "before_price": 6.15, "after_price": 13.16}])

    adj = adjusted_close(price_frame([("2017-05-03", 6.15), ("2017-05-04", 13.15)]),
                         pd.DataFrame(columns=DIV_COLUMNS), actions)

    assert adj["2017-05-03"] == pytest.approx(13.16, abs=0.01)   # 歷史價往上調
    assert adj["2017-05-04"] == pytest.approx(13.15)             # 事件當天不動
    assert abs(adj.pct_change().iloc[-1]) < 0.01                 # 還原後幾乎沒有漲跌


def test_par_value_split_is_adjusted():
    """6919 康霈 2025-07-21 面額變更，官方公告 1215 -> 121.5（十分之一）。

    方向跟減資相反：股數變多、每股價格變低，歷史價要往下調。
    """
    actions = pd.DataFrame([{"date": "2025-07-21", "before_price": 1215.0, "after_price": 121.5}])

    adj = adjusted_close(price_frame([("2025-07-18", 1215.0), ("2025-07-21", 121.5)]),
                         pd.DataFrame(columns=DIV_COLUMNS), actions)

    assert adj["2025-07-18"] == pytest.approx(121.5)
    assert adj["2025-07-21"] == pytest.approx(121.5)


def test_dividend_and_action_on_the_same_day_are_multiplied():
    """同一天又除息又減資時，兩個因子要相乘，不是只取其中一個"""
    divs = pd.DataFrame([{"date": "2024-03-12", "cash_div": 10.0, "stock_div": 0.0}])
    actions = pd.DataFrame([{"date": "2024-03-12", "before_price": 100.0, "after_price": 200.0}])

    adj = adjusted_close(price_frame([("2024-03-11", 100.0), ("2024-03-12", 180.0)]),
                         divs, actions)

    # 除息因子 (100-10)/100 = 0.9，減資因子 200/100 = 2.0，相乘 1.8
    assert adj["2024-03-11"] == pytest.approx(180.0)


def test_no_actions_is_the_same_as_none():
    """絕大多數股票沒有任何公司行動，傳 None 或空表都不該改變結果"""
    prices = price_frame([("2024-03-11", 100.0), ("2024-03-12", 100.0)])
    divs = pd.DataFrame([{"date": "2024-03-12", "cash_div": 10.0, "stock_div": 0.0}])

    baseline = adjusted_close(prices, divs)
    empty = pd.DataFrame(columns=["date", "before_price", "after_price"])

    assert list(adjusted_close(prices, divs, empty)) == list(baseline)


# ---------------------------------------------------------------------------
# 同一天多筆事件（步驟 17 修的缺陷）
# ---------------------------------------------------------------------------

def test_two_events_on_the_same_day_do_not_duplicate_price_rows():
    """同日兩筆事件不可以把價格列複製出來。

    舊版用 pd.merge(df, div, on="date") 合併，沒有保證 date 唯一，
    一天有幾筆事件那天的價格就會變成幾列。這些列之後會被寫進 prices，
    而 prices 的主鍵是 (stock_id, date)——複製出來的列不是被 UPSERT 蓋掉，
    就是讓上游的列數統計對不起來。
    """
    prices = price_frame([("2024-03-11", 200.0), ("2024-03-12", 100.0)])
    divs = pd.DataFrame([
        {"date": "2024-03-12", "cash_div": 0.0, "stock_div": 10.0},
        {"date": "2024-03-12", "cash_div": 5.0, "stock_div": 0.0},
    ], columns=DIV_COLUMNS)

    out = calculate_adjusted_price(prices, divs)

    assert len(out) == len(prices)


def test_two_events_on_the_same_day_are_combined_into_one_reference_price():
    """同日的現金與股票股利要合併成一次調整，不是連續套用兩次。

    證交所公式 P_ref = (P_prev − D) / (1 + S) 本來就同時吃兩種股利：
    (200 − 5) / (1 + 1.0) = 97.5。

    舊版拆成兩筆各自計算時，第二筆拿到的 prev_close 會是「同一天自己的收盤價」
    而不是真正的前一日收盤價，算出 95.0——差 2.6%，而且是系統性偏低。
    """
    prices = price_frame([("2024-03-11", 200.0), ("2024-03-12", 100.0)])
    divs = pd.DataFrame([
        {"date": "2024-03-12", "cash_div": 0.0, "stock_div": 10.0},
        {"date": "2024-03-12", "cash_div": 5.0, "stock_div": 0.0},
    ], columns=DIV_COLUMNS)

    adj = adjusted_close(prices, divs)

    assert adj["2024-03-11"] == pytest.approx(97.5)
    assert adj["2024-03-12"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 落在休市日的事件（Phase 1 複查時發現的缺陷）
# ---------------------------------------------------------------------------

def test_event_on_a_market_closure_day_is_applied_to_the_next_trading_day():
    """2884 玉山金 2024-07-25 除權息，但 07-24、07-25 凱米颱風休市。

    FinMind 記的是原定的 07-25，prices 裡沒有那天，跳空實際發生在 07-26。
    舊版用日期直接 merge，這筆事件被默默丟掉：07-23 的還原價維持 29.80，
    07-26 的還原後報酬變成 −9.4%（正確是 −3.7%）。

    還原後 07-23 = (29.80 − 1.2) / (1 + 0.0200332) = 28.04 ≈ 官方 reference_price 28.03。
    """
    prices = price_frame([("2024-07-23", 29.80), ("2024-07-26", 27.00)])
    divs = pd.DataFrame([{"date": "2024-07-25", "cash_div": 1.2, "stock_div": 0.200332}])

    adj = adjusted_close(prices, divs)

    assert adj["2024-07-23"] == pytest.approx(28.03, abs=0.01)
    assert adj["2024-07-26"] == pytest.approx(27.00)


def test_corporate_action_on_a_closure_day_is_applied_to_the_next_trading_day():
    """減資 / 面額變更走的是另一條路徑（_apply_action_factors），一樣要順延"""
    actions = pd.DataFrame([{"date": "2024-07-25", "before_price": 100.0, "after_price": 200.0}])

    adj = adjusted_close(price_frame([("2024-07-23", 100.0), ("2024-07-26", 200.0)]),
                         pd.DataFrame(columns=DIV_COLUMNS), actions)

    assert adj["2024-07-23"] == pytest.approx(200.0)


def test_event_after_the_last_trading_day_changes_nothing():
    """事件晚於最後一個交易日（例如今天除息、收盤資料還沒抓進來）：

    它之後還沒有任何價格，不影響任何一段已觀測到的報酬。不能因為順延找不到
    交易日就出錯，也不能把它硬套到最後一天、讓「最新一天不動」的定義被破壞。
    """
    prices = price_frame([("2024-07-22", 100.0), ("2024-07-23", 100.0)])
    divs = pd.DataFrame([{"date": "2024-07-25", "cash_div": 10.0, "stock_div": 0.0}])

    adj = adjusted_close(prices, divs)

    assert list(adj) == [pytest.approx(100.0), pytest.approx(100.0)]


# ---------------------------------------------------------------------------
# 既有行為：重構時不要弄壞
# ---------------------------------------------------------------------------

def test_adjusted_prices_are_not_rounded():
    """還原價是算報酬用的中間值，不能先四捨五入。

    舊版 round(2)：低價股還原到個位數之後，誤差直接變成報酬誤差
    （實測 2609 陽明單日報酬最大偏差 0.33%）。這裡 3.00 × (3.00 / 3.10) = 2.9032...，
    round(2) 會變成 2.90，差 0.11%。
    """
    prices = price_frame([("2024-03-11", 3.00), ("2024-03-12", 3.10), ("2024-03-13", 3.00)])
    divs = pd.DataFrame([{"date": "2024-03-13", "cash_div": 0.10, "stock_div": 0.0}])

    adj = adjusted_close(prices, divs)

    assert adj["2024-03-11"] == pytest.approx(3.00 * 3.00 / 3.10, rel=1e-12)

def test_multiple_events_are_compounded():
    """多次事件的調整係數要累乘，不是只套用最近的那一次。

    收盤價固定 100，01-02 與 01-04 各除息 10：
      每次的因子都是 (100 − 10) / 100 = 0.9
      01-01 在兩次事件之前 -> 0.9 × 0.9 = 0.81 -> 81
      01-02、01-03 只在第二次之前 -> 0.9 -> 90
      01-04 是最後一次事件當天 -> 不打折 -> 100
    """
    prices = price_frame([
        ("2026-01-01", 100.0), ("2026-01-02", 100.0),
        ("2026-01-03", 100.0), ("2026-01-04", 100.0),
    ])
    divs = pd.DataFrame([
        {"date": "2026-01-02", "cash_div": 10.0, "stock_div": 0.0},
        {"date": "2026-01-04", "cash_div": 10.0, "stock_div": 0.0},
    ], columns=DIV_COLUMNS)

    adj = adjusted_close(prices, divs)

    assert list(adj) == [pytest.approx(81.0), pytest.approx(90.0),
                         pytest.approx(90.0), pytest.approx(100.0)]


def test_event_day_itself_is_not_discounted():
    """事件當天的因子只影響「更早」的日期，不影響自己。

    這是 notebooks/_spike_datasource.ipynb 裡實測 6669 抓到的 bug：
    倒序 cumprod 時若沒有先 shift(-1)，事件當天會被多打一次折
    （2610 被還原成 875）。
    """
    prices = price_frame([("2026-09-01", 7800.0), ("2026-09-02", 2610.0)])
    divs = pd.DataFrame([{"date": "2026-09-02", "cash_div": 0.0, "stock_div": 19.827946}])

    adj = adjusted_close(prices, divs)

    assert adj["2026-09-02"] == pytest.approx(2610.0)


def test_latest_day_is_never_adjusted():
    """後還原的定義：最新一天的還原價恆等於原始收盤價。

    這條是整個計算的定義性檢查——不管前面有幾次事件，只要最後一天被改動了，
    就代表方向搞反成前還原了。
    """
    prices = price_frame([("2026-01-01", 100.0), ("2026-01-02", 120.0), ("2026-01-03", 150.0)])
    divs = pd.DataFrame([{"date": "2026-01-02", "cash_div": 10.0, "stock_div": 0.0}])

    adj = adjusted_close(prices, divs)

    assert adj["2026-01-03"] == pytest.approx(150.0)


def test_no_dividend_events_leaves_prices_untouched():
    """沒有任何除權息時，還原價就等於原始價。

    步驟 24 之後這是最常見的路徑——大多數股票在大多數區間裡都沒有除權息，
    所以空的事件表不能出錯，也不該噴 warning。
    """
    prices = price_frame([("2026-01-01", 100.0), ("2026-01-02", 120.0)])

    adj = adjusted_close(prices, pd.DataFrame(columns=DIV_COLUMNS))

    assert list(adj) == [pytest.approx(100.0), pytest.approx(120.0)]


def test_unsorted_input_is_sorted_before_adjusting():
    """輸入亂序也要算對——排序是計算的前提（prev_close 靠 shift(1)）"""
    prices = price_frame([("2023-08-11", 18.50), ("2023-08-09", 20.40), ("2023-08-10", 18.95)])
    divs = pd.DataFrame([{"date": "2023-08-10", "cash_div": 0.80, "stock_div": 0.0}])

    adj = adjusted_close(prices, divs)

    assert list(adj.index) == ["2023-08-09", "2023-08-10", "2023-08-11"]
    assert adj["2023-08-09"] == pytest.approx(19.60)
