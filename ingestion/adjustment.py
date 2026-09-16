"""
還原股價（backward adjustment）計算。

邏輯跟驗證過程記錄在 notebooks/_spike_datasource.ipynb 跟 docs/data_source.md，
這裡只放驗證過、修正過 bug 之後的正式版本，不重複整個推導過程。

放在獨立檔案而不是塞進 finmind_source.py，是因為這個計算本身跟 FinMind 無關
（只要有原始 OHLC + 除權息事件就能算），之後如果加了其他資料來源，一樣可以共用。
"""

import pandas as pd


def calculate_adjusted_price(df_price: pd.DataFrame, df_div: pd.DataFrame) -> pd.DataFrame:
    """用除權息事件計算還原股價（後還原：最新一天價格不變，把歷史價格往下折算）。

    Args:
        df_price: 至少要有 date, open, high, low, close 欄位的原始日線
        df_div: 至少要有 date（除權息交易日）, cash_div（現金股利/每股）,
            stock_div（股票股利/每 10 股配股數）欄位

    Returns:
        df_price 加上 adj_open/adj_high/adj_low/adj_close 欄位的 DataFrame，
        依 date 升冪排序

    公式（跟台灣證交所官方除權息參考價公式一致）：
        參考價 P_ref = (前一日收盤價 - 現金股利) / (1 + 股票股利/10)
        單次調整因子 f = P_ref / 前一日收盤價
        每一天的累積因子 = 該日期「之後」所有事件因子的累乘（嚴格大於，
        事件當天自己不能算進去，否則當天價格會被多打一次折——這是我們
        在 notebooks/_spike_datasource.ipynb 實測 6669 時抓到的 bug）
    """
    df = df_price.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    div = df_div[["date", "cash_div", "stock_div"]].copy()
    div["date"] = pd.to_datetime(div["date"])
    df = pd.merge(df, div, on="date", how="left")
    df["cash_div"] = df["cash_div"].fillna(0)
    df["stock_div"] = df["stock_div"].fillna(0)

    df["prev_close"] = df["close"].shift(1)
    df["split_ratio"] = df["stock_div"] / 10.0

    is_event = (df["cash_div"] > 0) | (df["stock_div"] > 0)
    p_ref = (df["prev_close"] - df["cash_div"]) / (1 + df["split_ratio"])
    df["adj_factor"] = 1.0
    df.loc[is_event, "adj_factor"] = p_ref / df["prev_close"]

    # 事件當天的 factor 只影響「更早」的日期，不影響自己（shift(-1) 是修正 bug 的關鍵）
    shifted = df["adj_factor"].shift(-1).fillna(1.0)
    df["cum_factor"] = shifted.iloc[::-1].cumprod().iloc[::-1]

    for col in ["open", "high", "low", "close"]:
        df[f"adj_{col}"] = (df[col] * df["cum_factor"]).round(2)

    return df.drop(columns=["cash_div", "stock_div", "prev_close", "split_ratio", "adj_factor", "cum_factor"])
