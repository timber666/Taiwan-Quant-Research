"""
資料品質檢查：把清理過的日線分成「可以寫進 prices」與「要記進隔離區」兩份。

跟 cleaner 的分工：cleaner 管格式（型別、日期寫法、重複列），validator 管內容
（這筆數字有沒有可能是真的）。cleaner 的產物是一份格式一致的 DataFrame，
validator 的產物是分流決策。

**隔離不等於丟棄。** 規則分兩種：

- 致命（fatal）：資料本身自相矛盾，例如最高價比最低價低、股價是負的。
  這種列不寫進 prices，只記進 data_quarantine。
- 標記（flag）：資料可能是對的，只是罕見到值得人看一眼，例如單日漲跌超過 15%。
  這種列**照樣寫進 prices**，但同時也記一筆到 data_quarantine 當作備查。

所以同一列可能同時出現在 good_df 和 bad_df，這是刻意的：data_quarantine 的定位是
「人應該看一眼的東西」，不是「被拒絕的東西」。
"""

import pandas as pd

# 單日漲跌幅超過這個比例就標記。
# 台股大多數個股的單日漲跌幅上限是 ±10%，所以超過 15% 幾乎不可能是正常交易造成的，
# 而是除權息、減資、面額變更、或停牌後復牌。這些多半是真的，所以標記而不隔離。
PRICE_JUMP_THRESHOLD = 0.15

PRICE_FIELDS = ["open", "high", "low", "close"]

# cleaner.clean() 的輸出欄位，validator 直接接在它後面
REQUIRED_COLUMNS = ["stock_id", "date", "open", "high", "low", "close", "volume"]

# OHLC 之間的大小關係。拆成五條各自命名而不是合併成一條 "ohlc_inconsistent"，
# 是因為步驟 26 的 EDA 要看「各 reason 幾筆」——知道是 high < low 還是 high < close
# 對追查來源問題的價值完全不同。
OHLC_RULES = [
    ("high_lt_low", lambda d: d["high"] < d["low"]),
    ("high_lt_open", lambda d: d["high"] < d["open"]),
    ("high_lt_close", lambda d: d["high"] < d["close"]),
    ("low_gt_open", lambda d: d["low"] > d["open"]),
    ("low_gt_close", lambda d: d["low"] > d["close"]),
]


def _as_bool(mask: pd.Series) -> pd.Series:
    """把可空布林（pandas 的 boolean dtype）壓成純 bool，缺值一律當作「沒命中」。

    cleaner 的 volume 是 Int64（可空整數），對它做 `volume < 0` 拿到的是 boolean
    dtype，缺值的位置是 pd.NA 而不是 False。這種遮罩帶進後面的邏輯會出事：
    `False | pd.NA` 等於 pd.NA、`~pd.NA` 還是 pd.NA，而 pandas 的布林索引把 NA
    當成 False——於是那一列既不會留在 good_df，也不會被記進 bad_df，
    **整列無聲無息地消失**。實測就是這樣掉了一列（成交量缺值的那天）。

    缺值的語意是「這條規則無法判斷」，不是「違規」，所以當成沒有命中。
    統一套在每一條規則上，之後新增規則時也不用再想這件事。
    """
    return mask.fillna(False).astype(bool)


def _price_jump_mask(df: pd.DataFrame, ex_dates: set) -> pd.Series:
    """標出單日漲跌幅過大的列，但跳過除權息日。

    除權息當天的原始價本來就會跳空——6669 緯穎 2026-09-02 從 7800 掉到 2610
    （-66%）、2409 友達每年除息也都會觸發。那些不是資料有問題，是市場的正常行為。
    不排除的話，quarantine 會被除權息洗版，真正需要人看的異常反而被埋掉。

    ex_dates 由 run_ingest 從剛抓到的除權息事件傳進來（步驟 23）。
    減資、面額變更不在 dividend_events 裡，所以還是會被標記——那是對的，
    那些確實需要人看一眼。
    """
    ordered = df.sort_values("date")
    prev_close = ordered["close"].shift(1)

    # prev_close 為 0 或缺值時不計算，否則會得到 inf。第一列沒有前一日，同樣跳過。
    comparable = prev_close.notna() & (prev_close > 0)
    change = (ordered["close"] - prev_close).abs() / prev_close
    jumped = comparable & (change > PRICE_JUMP_THRESHOLD)

    if ex_dates:
        jumped &= ~ordered["date"].isin(ex_dates)

    # 排序過的結果要對回原本的列順序
    return jumped.reindex(df.index, fill_value=False)


def validate(df: pd.DataFrame, ex_dates=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """檢查清理過的日線，回傳 (good_df, bad_df)。

    Args:
        df: cleaner.clean() 的輸出
        ex_dates: 這檔股票的除權息交易日（"YYYY-MM-DD" 字串的集合）。
            這些日子不做 price_jump 檢查。傳 None 代表不知道除權息日，
            那些跳空就會全部被標記出來。

    Returns:
        (good_df, bad_df)

        - good_df: 可以寫進 prices 的列，欄位與輸入相同
        - bad_df: 要記進 data_quarantine 的列，欄位是輸入加上 reason。
          **一列違反幾條規則就會出現幾次**（例如同時 high < low 又股價為負），
          因為 data_quarantine 的主鍵是 (stock_id, date, reason)，
          拆開來記步驟 26 才能直接 GROUP BY reason 看分布。

    Raises:
        ValueError: 輸入缺少必要欄位
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"輸入缺少欄位 {missing}，validator 要接在 cleaner.clean() 之後")

    if df.empty:
        return df.copy(), df.assign(reason=pd.Series(dtype=object))

    work = df.copy()
    ex_dates = set(ex_dates) if ex_dates else set()

    # 價格有缺值的列先單獨挑出來。不先處理的話，NaN 參與比較一律得到 False，
    # 於是 high >= close 這類檢查會通過，反而是 low > close 之類的判成 False、
    # 看起來「沒問題」——真正的問題（根本沒有價格）就被蓋掉了。
    # 所以：缺值自成一類，而且 OHLC 的大小關係只在價格完整的列上檢查。
    complete = work[PRICE_FIELDS].notna().all(axis=1)

    # (reason, 命中的列, 是否致命)
    checks = [("missing_price", ~complete, True)]

    for reason, rule in OHLC_RULES:
        checks.append((reason, complete & rule(work), True))

    checks.append((
        "non_positive_price",
        complete & (work[PRICE_FIELDS] <= 0).any(axis=1),
        True,
    ))

    # 刻意寫成 volume < 0 而不是 not (volume >= 0)：後者會把缺值也算進來，
    # 標成 negative_volume 是誤導。成交量缺值不致命（prices.volume 允許 NULL，
    # 而且價格還是可用的），所以這裡不管它。
    checks.append(("negative_volume", work["volume"] < 0, True))

    # cleaner 已經去過重了，這條照理說永遠不會命中。留著是因為它便宜，而且萬一
    # 有人繞過 cleaner 直接呼叫 validator，重複的日期會在寫進 prices 時才被主鍵擋下來，
    # 那時離問題發生的地方已經很遠。命中時兩筆都隔離——無法判斷哪筆才對，
    # 默默挑一筆正是上游出問題的原因。
    checks.append(("duplicate_date", work["date"].duplicated(keep=False), True))

    checks.append(("price_jump", _price_jump_mask(work, ex_dates), False))

    rejected = pd.Series(False, index=work.index)
    flagged = []
    for reason, mask, fatal in checks:
        mask = _as_bool(mask)
        if fatal:
            rejected |= mask
        if mask.any():
            flagged.append(work[mask].assign(reason=reason))

    good_df = work[~rejected]
    if flagged:
        bad_df = pd.concat(flagged).sort_values(["date", "reason"])
    else:
        bad_df = work.assign(reason=pd.Series(dtype=object)).iloc[:0]

    return good_df.reset_index(drop=True), bad_df.reset_index(drop=True)
