"""
把資料來源抓回來的原始 DataFrame，整理成可以寫進 prices 表的樣子。

cleaner 只管「格式」——型別、日期寫法、欄位順序、重複列——不判斷資料對不對。
「high 比 low 還低」「股價是負的」那類問題屬於 validator（步驟 20）。

兩者分開是因為產物不同：cleaner 的產物是一份格式一致的 DataFrame，
validator 的產物是「好的留下、壞的送隔離區」的分流決策。混在一起的話，
一筆資料到底是被清掉還是被擋掉就講不清楚，quarantine 的統計也會失真。
"""

import pandas as pd

# 對齊 database/schema.sql 的 prices 表（步驟 16 之後不含 adj_close，
# 還原價改由 database/data_access.py 在讀取時計算）。
# ingested_at 不在這裡產生，交給 SQLite 的 DEFAULT，才不會受本機時區影響。
PRICE_COLUMNS = [
    "stock_id", "date", "open", "high", "low", "close", "volume", "turnover", "source",
]

# 資料來源必須提供的欄位（PriceSource.fetch_prices 的契約）
REQUIRED_INPUT_COLUMNS = ["date", "open", "high", "low", "close", "volume", "turnover"]

FLOAT_COLUMNS = ["open", "high", "low", "close", "turnover"]
INT_COLUMNS = ["volume"]

DEFAULT_SOURCE = "FinMind"


def _to_numeric(series: pd.Series) -> pd.Series:
    """轉成數值，轉不動的變 NaN。

    先拔掉千分位逗號再轉：FinMind 回的本來就是數值型別，但 docs/data_source.md
    把 TWSE OpenAPI 列為交叉驗證來源，而它回的是 "1,234,567" 這種字串。
    不先處理的話整欄會默默變成 NaN——屬於「換個來源資料就整批消失、
    但程式不會報錯」的那種坑，先擋在這裡比較省事。
    """
    if series.dtype == object:
        series = series.astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(series, errors="coerce")


def clean(df: pd.DataFrame, stock_id: str, source: str = DEFAULT_SOURCE) -> pd.DataFrame:
    """把單一股票的原始日線整理成 prices 表的格式。

    做這幾件事：
    - 日期統一成 "YYYY-MM-DD" 字串（無法解析的列會被捨棄並印出警告）
    - 數值欄位轉型，轉不動的設成缺值
    - 依日期去重（保留輸入裡比較後面那筆）、依日期升冪排序
    - 補上 stock_id、source 兩欄
    - 欄位順序對齊 prices 表

    Args:
        df: 資料來源回傳的原始日線，至少要有 REQUIRED_INPUT_COLUMNS 那些欄位
        stock_id: 這批資料屬於哪一檔
        source: 資料來源名稱，會原樣寫進 prices.source。之所以做成參數而不是寫死
            "FinMind"，是因為 cleaner 本身跟來源無關，之後加 TWSE 備援時
            不該為了改一個字串而動到這支程式

    Returns:
        欄位為 PRICE_COLUMNS、依日期升冪、index 重編過的 DataFrame

    Raises:
        ValueError: 輸入缺少必要欄位（代表資料來源沒有遵守 PriceSource 的契約，
            這是程式的錯，不是資料髒，所以直接擋下來而不是想辦法補救）
    """
    missing = [c for c in REQUIRED_INPUT_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"{stock_id}: 輸入缺少欄位 {missing}，"
            f"資料來源的 fetch_prices 沒有遵守 PriceSource 的契約"
        )

    out = df.copy()

    # 日期正規化。兩個參數都是刻意指定的：
    # - errors="coerce"：解析不了的變成 NaT 而不是拋例外，一批資料裡有一兩列壞掉
    #   不會把整檔股票的下載成果一起丟掉。
    # - format="mixed"：pandas 2.x 的預設行為是「用第一列推斷出一種格式，
    #   然後套用到全部」，於是同一批資料裡只要混了第二種寫法（例如 "2024/03/19"
    #   夾在 "2024-03-20" 之間），那些列會全部變成 NaT 被當成壞資料丟掉。
    #   實測就踩到這個：6 列的測試資料被吃掉 1 列，而且程式不會報錯。
    parsed = pd.to_datetime(out["date"], format="mixed", errors="coerce")
    unparseable = int(parsed.isna().sum())
    if unparseable:
        # 沒有日期的列連隔離都做不到——prices 和 data_quarantine 的主鍵都含 date，
        # 存不進去、之後也查不回來，只能丟掉。但一定要講出來，不能無聲無息。
        print(f"警告：{stock_id} 有 {unparseable} 列的日期無法解析，已捨棄")
    out = out[parsed.notna()].copy()
    out["date"] = parsed[parsed.notna()].dt.strftime("%Y-%m-%d")

    for col in FLOAT_COLUMNS:
        out[col] = _to_numeric(out[col]).astype(float)
    for col in INT_COLUMNS:
        # Int64（大寫）是 pandas 的可空整數。成交股數是「股數」，用 float 存會在
        # EDA 裡變成 1.23e+09 這種樣子，也分不清「沒抓到」跟「當天真的沒成交」。
        # 步驟 22 的 loader 要注意：缺值是 pd.NA，sqlite3 不認得，寫入前要轉成 None。
        out[col] = _to_numeric(out[col]).round().astype("Int64")

    # 去重必須排在排序之前。keep="last" 的語意是「輸入裡比較後面那一筆」，也就是
    # 比較晚抓到、比較新的那份；先排序的話，pandas 的 sort_values 預設不是穩定排序，
    # 同一天的幾筆之間先後會被打亂，keep="last" 就變成隨機挑一筆了。
    out = out.drop_duplicates(subset="date", keep="last")
    out = out.sort_values("date").reset_index(drop=True)

    # 這兩欄一律以呼叫端的認知為準。來源自己帶的 stock_id 不保證跟我們要查的一致
    # （查 2330 卻回了別的代碼這種事發生過就很難查），以 plan_downloads 排的為準。
    out["stock_id"] = stock_id
    out["source"] = source

    return out[PRICE_COLUMNS]
