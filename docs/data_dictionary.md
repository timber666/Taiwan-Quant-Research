# 資料字典

`database/quant.db`（SQLite）的完整欄位說明。Schema 的權威定義在
[`database/schema.sql`](../database/schema.sql)，這份文件補的是那裡放不下的東西：
每個欄位的單位、可能的值、以及**容易誤用的地方**。

範例值都是資料庫裡的真實資料（2026-09-23 的快照）。

---

## 六張表與它們的關係

```
                    stocks  (股票基本資料，38 檔)
                      │ stock_id
        ┌─────────────┼─────────────┬──────────────┐
        ↓             ↓             ↓              ↓
     prices    dividend_events  corporate_    ingest_log
   (日線 OHLCV)   (除權息)        actions      (下載進度)
   104,816 列     465 筆       (減資/面額變更)    38 列
                                   14 筆

                 data_quarantine  (被擋下或標記的資料，440 筆)
                 刻意不設外鍵 —— 見下方說明
```

**還原股價（`adj_close`）不在任何一張表裡。** `prices` 只存原始成交價，
還原價是讀取時由 [`database/data_access.py`](../database/data_access.py) 用
`dividend_events` + `corporate_actions` 即時算出來的。理由見
[`database/schema.sql`](../database/schema.sql) 裡 `prices` 的註解。

---

## 共同慣例

| 項目 | 慣例 | 注意 |
|---|---|---|
| 日期 | `TEXT`，`"YYYY-MM-DD"` | SQLite 沒有日期型別。ISO 格式的字典序等同時間序，所以可以直接用 `<` `>` 比大小、`BETWEEN` 查區間 |
| 時間戳記 | `TEXT`，`"YYYY-MM-DD HH:MM:SS"`，**UTC** | ⚠️ SQLite 的 `datetime('now')` 回傳的是 UTC，不是本機時間。台灣時間要 **+8 小時**。範例：`ingested_at = "2026-09-23 08:18:05"` 實際上是台灣時間 16:18 |
| 價格 | `REAL`，單位：新台幣元 | 未還原的原始成交價 |
| 金額 | `REAL`，單位：新台幣元 | |
| 股數 | `INTEGER`，單位：股（不是張，1 張 = 1000 股） | |
| `NULL` | 代表「沒有這個資料」，不是 0 | 例如 `ingest_log.last_date` 是 `NULL` 代表這檔一列都還沒抓到 |

---

## `stocks` — 股票基本資料

每檔股票一列。是其他三張表的外鍵目標，所以**任何價格或事件寫進資料庫前，
這檔股票必須先存在於這裡**（`run_ingest` 因此一定先跑 `sync_stock_list`）。

| 欄位 | 型別 | 可空 | 說明 | 範例 |
|---|---|---|---|---|
| `stock_id` | TEXT | 否（**PK**） | 股票代碼，四位數字字串。保留字串型別是因為開頭可能有 0 | `"2330"` |
| `name` | TEXT | 否 | 公司簡稱。名稱含 `*` 表示該股有特殊狀況（如全額交割） | `"台積電"` |
| `market` | TEXT | 是 | 上市櫃別：`"twse"`（上市）/ `"tpex"`（上櫃）。**目前 universe 全部是 `twse`** | `"twse"` |
| `industry` | TEXT | 是 | 產業分類，目前 universe 涵蓋 15 種 | `"半導體業"` |
| `listing_date` | TEXT | 是 | 上市日。**目前全部是 `NULL`** | `NULL` |
| `is_active` | INTEGER | 是 | `1` = 仍在資料來源的清單裡，`0` = 已下市 | `1` |
| `updated_at` | TEXT | 是 | 這列最後一次被同步的時間（UTC） | `"2026-09-23 08:19:29"` |

**`listing_date` 為什麼是空的**：FinMind `taiwan_stock_info` 的 `date` 欄位是
「資料更新日」而不是上市日，目前沒有可靠的免費來源。欄位先留著，之後補。

**`is_active` 的用途是避免存活者偏誤**（survivorship bias）。只用今天還活著的股票
回測，會把所有倒閉、下市的公司排除在外，績效自然虛高。目前 38 檔都是 `1`——
這正是目前資料集的主要限制：**下市公司的歷史資料完全不在裡面**。

---

## `prices` — 日線 OHLCV

主鍵 `(stock_id, date)`，另有 `idx_prices_date` 索引供跨股票的橫斷面查詢使用。

| 欄位 | 型別 | 單位 | 可空 | 說明 | 範例 |
|---|---|---|---|---|---|
| `stock_id` | TEXT | | 否（**PK**） | 股票代碼 | `"2330"` |
| `date` | TEXT | | 否（**PK**） | 交易日 | `"2026-09-23"` |
| `open` | REAL | 元 | 是 | 開盤價 | `2475.0` |
| `high` | REAL | 元 | 是 | 最高價 | `2505.0` |
| `low` | REAL | 元 | 是 | 最低價 | `2475.0` |
| `close` | REAL | 元 | 是 | 收盤價，**未還原** | `2500.0` |
| `volume` | INTEGER | 股 | 是 | 成交股數 | `21978873` |
| `turnover` | REAL | 元 | 是 | 成交金額 | `54802017935.0` |
| `source` | TEXT | | 是 | 資料來源 | `"FinMind"` |
| `ingested_at` | TEXT | | 是 | 最後寫入時間（UTC） | `"2026-09-23 08:18:05"` |

**⚠️ 算報酬不要用 `close`。** 除權息當天股價會跳空下跌，但那不是虧損。
用 `close` 算長期報酬會系統性低估——2330 十一年用 `close` 算是 +1663%，
用還原價是 +2318%。請用 `data_access.get_prices(..., adjusted=True)` 拿到的 `adj_close`。

`close` 的正確用途是「畫出當時市場上實際看到的價格」。

**`open` 的可信度比其他三個價格低**：興櫃時期的報價、上市首日、停牌復牌當天
都可能出現 `open` 落在 `[low, high]` 之外或為 0 的情況。這些列**仍然保留在表裡**
（只被標記進 `data_quarantine`），因為 `close`/`high`/`low` 本身是好的。
詳見下方 `data_quarantine` 的 reason 說明。

---

## `dividend_events` — 除權息事件

還原股價的輸入之一。主鍵 `(stock_id, ex_date)`。

| 欄位 | 型別 | 單位 | 可空 | 說明 | 範例 |
|---|---|---|---|---|---|
| `stock_id` | TEXT | | 否（**PK**） | 股票代碼 | `"2409"` |
| `ex_date` | TEXT | | 否（**PK**） | **除權息交易日**（不是基準日，見下） | `"2023-08-10"` |
| `cash_div` | REAL | 元/股 | 是 | 現金股利，**盈餘配發 + 公積配發的總和** | `0.8` |
| `stock_div` | REAL | 股/**每 10 股** | 是 | 股票股利，同樣是兩者總和 | `0.0` |
| `reference_price` | REAL | 元 | 是 | 官方除權息參考價，僅供交叉驗證 | `19.6` |
| `source` | TEXT | | 是 | 資料來源 | `"FinMind"` |
| `ingested_at` | TEXT | | 是 | 最後寫入時間（UTC） | `"2026-09-19 11:05:45"` |

**⚠️ `stock_div` 的單位是「每 10 股配發幾股」，不是每股。** 這是台股慣例
（「配股 2 元」= 每 10 股配 2 股）。證交所的參考價公式因此是
`P_ref = (前一日收盤價 − cash_div) / (1 + stock_div / 10)`。
把它當成每股配股數會讓還原量差十倍。範例：2027 大成鋼 2023-07-28
`cash_div=2.4, stock_div=2.0` 表示每股配息 2.4 元、每 10 股配 2 股。

**`ex_date` 是交易日不是基準日**：FinMind 原始資料的 `date` 欄位是「權利分派基準日」，
比除權息交易日晚 6〜10 天。拿基準日去跟股價對齊會對到錯的一天。

**`reference_price` 不參與計算**（465 筆中有 7 筆是 `NULL`），只用來事後稽核：
用 `cash_div`/`stock_div` 算出的參考價應該要跟它一致。

---

## `corporate_actions` — 減資與面額變更

還原股價的輸入之二。主鍵 `(stock_id, ex_date, action_type)`——
帶 `action_type` 是因為同一天理論上可能同時發生兩種公司行動。

| 欄位 | 型別 | 單位 | 可空 | 說明 | 範例 |
|---|---|---|---|---|---|
| `stock_id` | TEXT | | 否（**PK**） | 股票代碼 | `"2609"` |
| `ex_date` | TEXT | | 否（**PK**） | 減資恢復買賣日 / 面額變更生效日 | `"2017-05-04"` |
| `action_type` | TEXT | | 否（**PK**） | `"capital_reduction"`（減資）或 `"split"`（面額變更） | `"capital_reduction"` |
| `before_price` | REAL | 元 | 否 | 官方公告的事件前收盤價 | `6.15` |
| `after_price` | REAL | 元 | 否 | 官方公告的事件後參考價 | `13.16` |
| `note` | TEXT | | 是 | 減資原因或分割類型 | `"Making up losses"` |
| `source` | TEXT | | 是 | 資料來源 | `"FinMind"` |
| `ingested_at` | TEXT | | 是 | 最後寫入時間（UTC） | `"2026-09-19 11:06:14"` |

**調整因子 = `after_price / before_price`**，直接相除即可，不需要自己推導減資比率——
官方已經把參考價算好了。減資的因子 > 1（股數變少、每股價格變高，歷史價要往上調），
面額變更的因子 < 1。

`note` 目前看到的值：`"Cash refund"`（現金退還減資）、`"Making up losses"`
（彌補虧損減資）、`"面額變更"`。中英文混雜是照 FinMind 原樣保留的。

**為什麼跟 `dividend_events` 分開存**：兩者的調整方式不同。除權息要用證交所公式
從股利金額推參考價，這裡的參考價則是官方直接公告的。硬塞進同一張表會讓欄位語意
變得模糊（`cash_div` 對減資來說是什麼？）。

---

## `ingest_log` — 下載進度

增量更新的依據，每檔股票一列。

| 欄位 | 型別 | 可空 | 說明 | 範例 |
|---|---|---|---|---|
| `stock_id` | TEXT | 否（**PK**） | 股票代碼 | `"2330"` |
| `last_date` | TEXT | 是 | `prices` 裡這檔**實際擁有**的最後一天 | `"2026-09-23"` |
| `last_run_at` | TEXT | 是 | 最後一次**嘗試**下載的時間（UTC） | `"2026-09-23 08:18:05"` |
| `rows_total` | INTEGER | 是 | `prices` 裡這檔的總列數 | `2858` |

**`last_date` 與 `rows_total` 是從 `prices` 現算的**，不是從下載到的資料取。
這讓 log 不可能宣稱「有我們實際上沒有的資料」：如果某天的資料全被 validator 擋掉，
`last_date` 就不會前進，下次自然會再抓一次那段。

`plan_downloads` 用它決定下次的起點：`start = last_date + 1 天`；
`last_date` 是 `NULL` 就從 `config.settings.HISTORY_START_DATE` 開始。

**`last_run_at` 記的是「最後一次嘗試」不是「最後一次成功」**，
兩者的差距（`last_run_at` 很新但 `last_date` 很舊）本身就是有用的診斷訊號。

---

## `data_quarantine` — 隔離區

validator 認為有問題的資料。主鍵 `(stock_id, date, reason)`。

| 欄位 | 型別 | 可空 | 說明 | 範例 |
|---|---|---|---|---|
| `stock_id` | TEXT | 否（**PK**） | 股票代碼 | `"6442"` |
| `date` | TEXT | 否（**PK**） | 出問題的交易日 | `"2015-02-03"` |
| `reason` | TEXT | 否（**PK**） | 違反了哪條規則，見下表 | `"high_lt_open"` |
| `raw_json` | TEXT | 是 | 整列原始資料的 JSON，缺值為 `null` | 見下 |
| `caught_at` | TEXT | 是 | 最後一次被攔下的時間（UTC） | `"2026-09-19 11:05:59"` |

`raw_json` 範例：

```json
{"close": 0.0, "date": "2015-02-03", "high": 0.0, "low": 0.0, "open": 39.06,
 "source": "FinMind", "stock_id": "6442", "turnover": 0.0, "volume": 0}
```

**三件容易誤解的事：**

1. **「隔離」不等於「丟棄」。** 規則分兩種：致命的不寫進 `prices`，標記的**照樣寫入**。
   這張表的定位是「人應該看一眼的東西」，不是「被拒絕的東西」。
2. **筆數大於列數。** 一列違反三條規則就記三筆，主鍵含 `reason` 就是為了這個。
   要算「有幾天有問題」請用 `COUNT(DISTINCT stock_id || date)`。
3. **刻意不設外鍵到 `stocks`。** 這張表要收容的正是有問題的資料，包含 `stock_id`
   本身就打錯的那種。如果連進隔離區都要先通過外鍵檢查，最可疑的資料會被擋在門外。

### `reason` 一覽（括號內為 2026-09-23 快照的筆數）

| reason | 處置 | 意義 | 實際成因 |
|---|---|---|---|
| `low_gt_open` (234) | 標記 | 最低價 > 開盤價 | 興櫃時期報價，`open` 與 `high`/`low` 不同口徑 |
| `high_lt_open` (158) | 標記 | 最高價 < 開盤價 | 同上 |
| `non_positive_price` (36) | **不寫入** | `high`/`low`/`close` 任一 ≤ 0 | 停牌或無成交日，FinMind 回傳全 0 |
| `price_jump` (9) | 標記 | 單日漲跌 > 15%（已排除除權息與公司行動日） | 興櫃沒有漲跌幅限制 |
| `non_positive_open` (3) | 標記 | 只有 `open` ≤ 0 | 上市首日、停牌復牌當天沒有開盤參考價 |
| `missing_price` (0) | **不寫入** | `high`/`low`/`close` 有缺值 | |
| `high_lt_low` (0) | **不寫入** | 最高價 < 最低價 | |
| `high_lt_close` (0) | **不寫入** | 最高價 < 收盤價 | |
| `low_gt_close` (0) | **不寫入** | 最低價 > 收盤價 | |
| `negative_volume` (0) | **不寫入** | 成交量 < 0 | |
| `duplicate_date` (0) | **不寫入** | 同一檔同一天出現兩列 | cleaner 已去重，命中代表上游有問題 |

**⚠️ 這張表的紀錄不會因為規則改變而失效。** 調整 validator 的規則之後，
舊規則產生的紀錄仍留在表裡，統計會混到已經不適用的項目。
處理方式：用 `python -m ingestion.run_ingest --full` 重跑，或先清掉舊紀錄。

---

## 怎麼讀這些資料

**不要自己寫 SQL 查 `prices`。** 走
[`database/data_access.py`](../database/data_access.py)：

```python
from database.data_access import get_prices, get_price_matrix, get_universe

df = get_prices("2330", "2024-01-01")        # 含 adj_close 的日線
mat = get_price_matrix(field="adj_close")     # index=date, columns=stock_id
ids = get_universe()                          # 仍在市場上的股票代碼
```

原因有兩個：一是還原股價只有那一層算得出來（`prices` 裡沒有），
二是之後若換資料庫或改 schema，上層程式不用跟著動。
