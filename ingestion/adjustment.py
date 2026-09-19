"""
還原股價（backward adjustment）計算。

邏輯跟驗證過程記錄在 notebooks/_spike_datasource.ipynb 跟 docs/data_source.md，
這裡只放驗證過、修正過 bug 之後的正式版本，不重複整個推導過程。

放在獨立檔案而不是塞進 finmind_source.py，是因為這個計算本身跟 FinMind 無關
（只要有原始 OHLC + 除權息事件就能算），之後如果加了其他資料來源，一樣可以共用。
"""

import pandas as pd


def calculate_adjusted_price(
    df_price: pd.DataFrame, df_div: pd.DataFrame, df_actions: pd.DataFrame = None
) -> pd.DataFrame:
    """用除權息事件計算還原股價（後還原：最新一天價格不變，把歷史價格往下折算）。

    Args:
        df_price: 至少要有 date, open, high, low, close 欄位的原始日線
        df_div: 至少要有 date（除權息交易日）, cash_div（現金股利/每股）,
            stock_div（股票股利/每 10 股配股數）欄位。
            同一天出現多筆事件會自動加總，呼叫端不必自己先合併。
        df_actions: 減資與面額變更事件，要有 date, before_price, after_price 欄位。
            可以是 None 或空表（多數股票本來就沒有）。

    Returns:
        df_price 加上 adj_open/adj_high/adj_low/adj_close 欄位的 DataFrame，
        依 date 升冪排序

    公式（跟台灣證交所官方除權息參考價公式一致）：
        參考價 P_ref = (前一日收盤價 - 現金股利) / (1 + 股票股利/10)
        單次調整因子 f = P_ref / 前一日收盤價
        每一天的累積因子 = 該日期「之後」所有事件因子的累乘（嚴格大於，
        事件當天自己不能算進去，否則當天價格會被多打一次折——這是我們
        在 notebooks/_spike_datasource.ipynb 實測 6669 時抓到的 bug）

    減資與面額變更的因子不用上面的公式推導，直接取官方公告的
    `after_price / before_price`——官方已經把參考價算好了，自己再推一次只是多一個
    出錯的機會。除權息因子偏小（< 1，把歷史價往下折），減資因子偏大（> 1，
    把歷史價往上調，因為減資後股數變少、每股價格變高）。
    同一天若同時有除權息與公司行動，兩個因子相乘。
    """
    df = df_price.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    div = df_div[["date", "cash_div", "stock_div"]].copy()
    div["date"] = pd.to_datetime(div["date"])
    # 同一天可能有多筆事件（例如現金與股票分開公告）。不先合併就 merge 的話，
    # 一天有幾筆事件、那天的價格列就會被複製幾份，而且每一筆各自去算調整因子時，
    # 第二筆拿到的 prev_close 會是「同一天自己的收盤價」而不是真正的前一日收盤價，
    # 算出來的還原價是錯的（實測：200 -> 95.0，正確應為 97.5）。
    # 相加是正確的合併方式：證交所公式 P_ref = (P_prev − D) / (1 + S) 本來就同時
    # 吃現金股利與股票股利，同一天的事件合併成一筆 (D_total, S_total) 才符合公式。
    div = div.groupby("date", as_index=False).sum()
    df = pd.merge(df, div, on="date", how="left")
    # to_numeric 而不是直接 fillna：df_div 沒有任何事件時（步驟 24 之後這是常態——
    # 大多數股票在大多數區間裡都沒有除權息），merge 進來的會是 object dtype 的空欄位，
    # 對它 fillna 會觸發 pandas 的 downcasting FutureWarning，而且後續算術也不保證正確。
    # 先轉成數值再補 0，順便把萬一混進來的字串擋掉。
    df["cash_div"] = pd.to_numeric(df["cash_div"], errors="coerce").fillna(0.0)
    df["stock_div"] = pd.to_numeric(df["stock_div"], errors="coerce").fillna(0.0)

    df["prev_close"] = df["close"].shift(1)
    df["split_ratio"] = df["stock_div"] / 10.0

    is_event = (df["cash_div"] > 0) | (df["stock_div"] > 0)
    p_ref = (df["prev_close"] - df["cash_div"]) / (1 + df["split_ratio"])
    df["adj_factor"] = 1.0
    df.loc[is_event, "adj_factor"] = p_ref / df["prev_close"]

    df = _apply_action_factors(df, df_actions)

    # 事件當天的 factor 只影響「更早」的日期，不影響自己（shift(-1) 是修正 bug 的關鍵）
    shifted = df["adj_factor"].shift(-1).fillna(1.0)
    df["cum_factor"] = shifted.iloc[::-1].cumprod().iloc[::-1]

    for col in ["open", "high", "low", "close"]:
        df[f"adj_{col}"] = (df[col] * df["cum_factor"]).round(2)

    return df.drop(columns=["cash_div", "stock_div", "prev_close", "split_ratio", "adj_factor", "cum_factor"])


def _apply_action_factors(df: pd.DataFrame, df_actions: pd.DataFrame) -> pd.DataFrame:
    """把減資 / 面額變更的因子乘進 adj_factor。

    這些事件的調整因子不用證交所的除權息公式推導，直接取官方公告的
    after_price / before_price。理由是官方已經把參考價算好了，而減資的公式還要
    知道減資比率與每股退還金額，自己再推一次只是多一個出錯的機會。
    """
    if df_actions is None or df_actions.empty:
        return df

    act = df_actions[["date", "before_price", "after_price"]].copy()
    act["date"] = pd.to_datetime(act["date"])
    act = act[act["before_price"] > 0]
    if act.empty:
        return df

    act["action_factor"] = act["after_price"] / act["before_price"]
    # 同一天若有多筆（例如減資與面額變更一起），因子相乘；跟除權息那邊用 sum 合併
    # 不同，是因為這裡合併的是「比例」不是「金額」。
    act = act.groupby("date", as_index=False)["action_factor"].prod()

    df = df.merge(act, on="date", how="left")
    df["action_factor"] = pd.to_numeric(df["action_factor"], errors="coerce").fillna(1.0)
    df["adj_factor"] *= df["action_factor"]
    return df.drop(columns=["action_factor"])
