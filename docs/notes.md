# 學習筆記

紀錄專案裡遇到的、還不熟的概念，簡短記錄就好，之後隨時可以補充。

---

## 路徑處理：`Path(__file__).resolve().parent.parent`

**在哪裡**：`config/settings.py` 的 `BASE_DIR`

**是什麼**：把「這支檔案自己的路徑」轉成絕對路徑，再往上跳兩層資料夾，
算出專案根目錄。

**關鍵字**：
- `__file__`：目前這支 `.py` 檔案自己的路徑
- `Path(__file__)`：包成 `Path` 物件，才能用後面的 `.resolve()`、`/` 等操作
- `.resolve()`：轉成絕對路徑，並正規化掉 `..` 之類的東西
- `.parent`：往上一層資料夾（`settings.py` 在 `config/` 裡，`.parent` 就是
  `config/`；再 `.parent` 一次就是專案根目錄）
- `BASE_DIR / "database" / "quant.db"`：`Path` 把 `/` 重載成路徑拼接，
  會自動用對的作業系統分隔符號

**為什麼要用**：相對路徑（例如直接寫 `"database/quant.db"`）是相對於「執行
當下的工作目錄」，不是相對於程式檔案自己的位置——從不同資料夾或從
notebook 執行就會找錯檔案（這正是舊版 `init_db.py` 的 bug）。錨定在
`__file__` 上就跟從哪裡執行完全無關。

---

## 步驟 11：抽象基底類別（`ABC` / `abstractmethod`）

**在哪裡**：`ingestion/sources/base.py` 的 `PriceSource`

**是什麼**：一個只定義「要有哪些方法、回傳什麼格式」，但不寫實際邏輯的類別。
繼承它的子類別（例如之後的 `FinMindSource`）才要真正實作這些方法。

**關鍵字**：
- `class PriceSource(ABC)`：繼承 `ABC` 代表這是抽象類別，**不能直接被實例化**
  （`PriceSource()` 會直接報錯）
- `@abstractmethod`：標記這個方法子類別**必須**實作，漏寫的話，子類別在
  被建立實例的那一刻就會報錯，不用等程式真的跑到那一行

**為什麼要用**：上層程式（`downloader.py`）只呼叫 `source.fetch_prices(...)`，
不用管背後是 FinMind 還是 TWSE。以後要換資料源或加備援，只要多寫一個繼承
`PriceSource` 的 class，上層完全不用改。

---

## 步驟 12：指數退避（exponential backoff）

**在哪裡**：`ingestion/sources/finmind_source.py` 的 `_call_with_retry()`

**是什麼**：呼叫 API 失敗時自動重試，但每次重試前等待的時間愈來愈長
（1 秒 → 2 秒 → 4 秒），而不是固定間隔。

**關鍵字**：
- `BACKOFF_BASE_SECONDS ** attempt`：`attempt` 是迴圈計數 0、1、2，
  所以等待秒數是 2⁰、2¹、2²，呈指數成長
- 「不該重試的錯誤」：權限不足（錯誤訊息含 `Your level is`）重試幾次都一樣會失敗，
  直接 `raise` 往外丟，不要白等

**為什麼要用**：呼叫失敗通常代表對方伺服器忙、或自己打太快。固定間隔重試等於
持續加壓，愈等愈久才給對方喘息空間，成功率也比較高。這是串接外部 API 的標準做法。

---

## 步驟 13：UPSERT（`INSERT ... ON CONFLICT DO UPDATE`）

**在哪裡**：`ingestion/downloader.py` 的 `sync_stock_list()`；
`ingestion/loader.py` 的四個寫入函式也是同一招

**是什麼**：「沒有就新增、已經有就更新」的寫入方式，名字是 UPDATE + INSERT 的合體。

**關鍵字**：
- `INSERT INTO stocks (...) VALUES (?, ?, ...)`：新增一列。`?` 是佔位符號，
  `executemany` 會依序把每個 tuple 的值填進去。不要自己把值拼進 SQL 字串——
  名稱裡有引號之類的特殊字元會讓 SQL 壞掉，也有 SQL injection 的風險
- `ON CONFLICT(stock_id)`：主鍵 `stock_id` 已經存在（衝突）時……
- `DO UPDATE SET name = excluded.name`：……不報錯，改成更新那一列。
  `excluded` 指的是「剛剛想新增、但被擋下來的那一列」，也就是新值
- 冪等（idempotent）：同一件事做一次跟做很多次，結果都一樣

**為什麼要用**：整條管線必須可以重跑。資料已經在資料庫裡時，普通的 `INSERT`
會因為主鍵重複而報錯（`IntegrityError`），整批失敗；UPSERT 會改成更新，
重跑幾次結果都一樣。這是之後每天自動排程更新的前提。

---

## 步驟 14：增量更新（incremental update）

**在哪裡**：`ingestion/downloader.py` 的 `plan_downloads()`，進度紀錄存在 `ingest_log` 表

**是什麼**：每次只抓「資料庫裡還沒有的那一段」，不從頭重抓。

**關鍵字**：
- `ingest_log.last_date`：每檔股票已經抓到的最後一天。它是從 `prices` 裡實際存在的
  最後一天算出來的（`loader.update_ingest_log()`），所以紀錄不會比真實資料超前
- 有紀錄 → `start = last_date + 1 天`；沒紀錄（第一次抓）→ `start = HISTORY_START_DATE`
  （2015-01-01）；`end` 一律是今天。
  例：2330 的 `last_date` 是 `2026-09-23`、今天是 `2026-10-13` → 抓 `2026-09-24 ~ 2026-10-13`
- `date.fromisoformat(...) + timedelta(days=1)`：字串轉成日期、加一天，
  再用 `.isoformat()` 轉回 `"YYYY-MM-DD"` 字串
- `start > end`：代表今天已經抓過、沒有新資料，呼叫端直接跳過，連 API 都不打

**為什麼要用**：每天從 2015 年重抓既浪費時間，也會把已經在資料庫裡的資料重寫一遍。
但要注意它省的是**資料量與寫入**，不是 API 呼叫次數——不論區間多短，每檔股票還是要
問同樣幾個 API（實測 3 天的增量 49.9 秒，全量重抓 67 秒）。所以要擴大到全市場，
解法是批次查詢，不是靠增量。
