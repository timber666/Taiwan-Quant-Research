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

# 「沒有這個價格就等於沒有這一天」的欄位。open 刻意不在裡面——理由跟 OHLC_RULES
# 那兩條 open 規則一樣：上市首日與停牌復牌當天沒有開盤參考價，FinMind 會填 0，
# 但那天是真的有成交的。實測到三天被整列丟掉：6861 睿生光電 2021-11-30（上市首日，
# 成交 41 萬股）、6919 康霈 2022-12-26（上市首日，767 萬股）與 2024-03-29
# （停牌 12 天後復牌，142 萬股），三天的 high/low/close/volume 全都正常，
# 只有 open 是 0。回測用的是 close，為了一個 open 把整天丟掉並不划算。
FATAL_PRICE_FIELDS = ["high", "low", "close"]

# cleaner.clean() 的輸出欄位，validator 直接接在它後面
REQUIRED_COLUMNS = ["stock_id", "date", "open", "high", "low", "close", "volume"]

# OHLC 之間的大小關係，(reason, 判斷式, 是否致命)。
# 拆成五條各自命名而不是合併成一條 "ohlc_inconsistent"，是因為步驟 26 的 EDA 要看
# 「各 reason 幾筆」——知道是 high < low 還是 high < close，對追查來源問題的價值完全不同。
#
# **跟 open 有關的兩條只標記、不隔離。** 步驟 23 實跑後發現它們擋掉了 392 天的資料，
# 而且全部集中在 4 檔股票的興櫃時期（6446 藥華藥、6919 康霈、6861 睿生光電、
# 6442 光聖，各自上市之後就再也沒發生）。興櫃是議價成交、沒有集合競價，FinMind 的
# open 跟 high/low 不是同一套口徑（那段期間的報價像 138.64 這種不符合台股跳動單位的
# 數字），所以 open 會落在 [low, high] 之外。
# 但那些列的 close/high/low 本身是合理的，而回測用的是 close——為了一個對不上的 open
# 把整天丟掉，會在序列中間開洞（6919 一度少掉 13.4% 的交易日），
# 而缺一天會讓 t-1 到 t+1 被當成一天的報酬，比留著一個怪的 open 危險得多。
OHLC_RULES = [
    ("high_lt_low", lambda d: d["high"] < d["low"], True),
    ("high_lt_close", lambda d: d["high"] < d["close"], True),
    ("low_gt_close", lambda d: d["low"] > d["close"], True),
    ("high_lt_open", lambda d: d["high"] < d["open"], False),
    ("low_gt_open", lambda d: d["low"] > d["open"], False),
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

    ex_dates 由 run_ingest 傳進來，包含除權息交易日與減資 / 面額變更日
    （後者是步驟 23 實跑後才補上的：當時 price_jump 標出 18 筆大跳空，
    查證後有 11 筆是減資或面額變更，例如 2609 陽明 2017-05-04 的 +116%）。

    剩下會被標記的，就是真正只能靠人判斷的：興櫃時期沒有漲跌幅限制的大波動，
    以及尚未納入的其他公司行動。
    """
    ordered = df.sort_values("date")
    prev_close = ordered["close"].shift(1)

    # prev_close 為 0 或缺值時不計算，否則會得到 inf。第一列沒有前一日，同樣跳過。
    #
    # 當日 close 為 0 也要排除。無成交日的 close 是 0，「昨天 39 元、今天 0 元」
    # 會被算成 -100% 的跳空，但那不是價格異常，是那天根本沒有價格——而且那一列
    # 已經被 non_positive_price 擋掉了，不會寫進 prices，再標一次 price_jump
    # 只是把同一個問題記兩遍。實測：44 筆 price_jump 裡有 36 筆是這樣來的假訊號，
    # 真正需要人判斷的只有 8 筆。quarantine 的定位是「人應該看一眼的東西」，
    # 被 82% 的雜訊稀釋掉就失去意義了。
    comparable = prev_close.notna() & (prev_close > 0) & (ordered["close"] > 0)
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
        ex_dates: 這檔股票「已知會跳空」的日子（"YYYY-MM-DD" 字串的集合），
            包含除權息交易日與減資 / 面額變更日。這些日子不做 price_jump 檢查。
            傳 None 代表不知道，那些跳空就會全部被標記出來。

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

    for reason, rule, fatal in OHLC_RULES:
        checks.append((reason, complete & rule(work), fatal))

    # 收高低價任一個 <= 0 -> 這天沒有可用的價格，致命。
    fatal_price = complete & (work[FATAL_PRICE_FIELDS] <= 0).any(axis=1)
    checks.append(("non_positive_price", fatal_price, True))

    # open <= 0 單獨一條，而且只標記不隔離。
    # 加上 ~fatal_price 是為了不要重複記帳：全 0 的無成交日（收高低也是 0）已經被
    # 上面那條擋下來了，再記一筆 non_positive_open 只是同一件事講兩遍，
    # 會讓步驟 26 的 quarantine 分布統計失真。這裡要抓的是「只有 open 有問題」。
    checks.append((
        "non_positive_open",
        complete & ~fatal_price & (work["open"] <= 0),
        False,
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
