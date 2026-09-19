# Phase 1 執行規劃｜資料基礎設施（第 3〜6 週）

## 目標（完成標準）

> 輸入一個股票代碼，程式能從自建資料庫撈出多年日線資料，畫出「原始價 vs 還原價」、
> 成交量、基本統計；整個下載流程可以重跑不會壞、可以增量更新，
> 而且有一份資料品質報告證明資料是乾淨的。

**範圍界定**

要做：上市 + 上櫃「普通股」的日線 OHLCV + 成交金額、還原股價、股票基本資料。

這階段不碰：財報 / 三大法人 / 融資券（Phase 3）、分鐘或 tick 資料、PostgreSQL 遷移、
排程自動化（Airflow / cron）。

---

## 進度追蹤

- [x] 階段 A：前置設定與資料來源評估（步驟 1〜6）
- [x] 階段 B：資料庫 Schema 重設計（步驟 7〜10）
- [x] 階段 C：Downloader（步驟 11〜15）
- [ ] 階段 D：Cleaner + Validator + Loader（步驟 16〜23）
- [ ] 階段 E：資料存取層 + EDA + 驗收（步驟 24〜30）

> 建議每完成 1〜2 步就 `git commit` 一次，訊息像
> `Phase 1 (step 7): redesign database schema`。

---

# 階段 A：前置設定與資料來源評估（第 3 週前半）

## 步驟 1：修好 `requirements.txt` 並補套件

- **為什麼**：目前的 `requirements.txt` 是 PowerShell `pip freeze >` 產生的 UTF-16，
  內容是亂碼，別人 `pip install -r` 會失敗。
- **怎麼做**：
  1. 在 venv 啟用狀態下：
     ```bash
     pip install FinMind lxml tqdm python-dotenv
     pip freeze | Out-File -Encoding utf8 requirements.txt
     ```
  2. 打開檔案確認開頭沒有 BOM 亂碼、每行是 `package==version`。
- **完成標準**：新開一個乾淨 venv，`pip install -r requirements.txt` 能成功跑完。

## 步驟 2：註冊 FinMind、設定 token

- **為什麼**：免費但註冊後速率上限較高，全量下載才不會一直被擋。
- **怎麼做**：
  1. 到 FinMind 官網註冊，拿到 API token。
  2. `.env` 加一行：`FINMIND_API_TOKEN=你的token`
  3. `config/settings.py` 加：
     ```python
     import os
     from dotenv import load_dotenv
     load_dotenv(BASE_DIR / ".env")
     FINMIND_API_TOKEN = os.getenv("FINMIND_API_TOKEN")
     ```
- **完成標準**：
  `python -c "from config.settings import FINMIND_API_TOKEN; print(bool(FINMIND_API_TOKEN))"`
  印出 `True`。

## 步驟 3：實測三個資料來源（spike）

- **為什麼**：規劃書特別點名「資料來源是最容易卡住的地方」，花 2〜3 天值得。
- **怎麼做**：在 `notebooks/` 開一個 `_spike_datasource.ipynb`（底線開頭代表拋棄式），分別試：
  - **FinMind**：`TaiwanStockInfo`、`TaiwanStockPrice`（原始價）、
    `TaiwanStockPriceAdj`（還原價），抓 2330 近 5 年。
  - **TWSE OpenAPI**：抓當日 / 近月股價，看欄位。
  - **公開資訊觀測站**：看一下財報頁面格式（先不用真的爬）。
  - 每個來源記錄：欄位完整度、有沒有還原價、速率限制、抓 5 年要多久、上手難度。
- **完成標準**：能跑出 2330 近 5 年的還原日線 DataFrame，並知道每個來源的優缺點。

## 步驟 4：寫 `docs/data_source.md`

- **為什麼**：這是面試講「你怎麼做技術選型」的素材，比口頭講有說服力。
- **怎麼做**：一頁就好，包含：三個來源的比較表、最後選哪個當主來源、選它的理由、
  已知限制（例如 FinMind 免費額度、下市股票不完整）。
- **完成標準**：陌生人看完知道你為什麼選這個來源。

## 步驟 5：搞懂還原股價（除權息調整）

- **為什麼**：這是台股資料頭號坑。除權息當天原始價會「跳空下跌」，但那不是真的虧損，
  回測若用原始價算報酬會嚴重失真。
- **怎麼做**：
  1. 找一檔近年有配息的股票（例如 2412 中華電），在 spike notebook 把
     `close` 和 `adj_close` 疊在同一張圖。
  2. 挑除息日前後兩天，手算一次：原始價跌了多少、還原價為什麼是連續的。
  3. 在 `docs/data_source.md` 補一小段「還原股價說明」。
- **完成標準**：你能用自己的話解釋「為什麼回測要用 adj_close，畫真實股價要用 close」。

## 步驟 6：定義投資範圍（universe）

- **為什麼**：全市場 1700+ 檔一次抓會打爆 API、也難除錯。先小範圍跑通。
- **怎麼做**：`config/settings.py` 加：
  ```python
  # Phase 1 先鎖小範圍，跑通後再放大
  UNIVERSE_MODE = "sample"          # "sample" | "all"
  SAMPLE_UNIVERSE = ["2330", "2317", "2454", "2412", "2882"]  # 約 30~50 檔，涵蓋不同產業
  HISTORY_START_DATE = "2015-01-01"
  ```
- **完成標準**：`from config.settings import SAMPLE_UNIVERSE` 拿得到一個 30〜50 檔的清單。

---

# 階段 B：資料庫 Schema 重設計（第 3 週後半）

## 步驟 7：重寫 `database/schema.sql`

- **為什麼**：目前 `prices` 沒有主鍵、沒有索引、沒有還原價欄位，無法支撐後面的 pipeline。
- **怎麼做**：改成四張表：
  ```sql
  CREATE TABLE IF NOT EXISTS stocks (
      stock_id     TEXT PRIMARY KEY,
      name         TEXT NOT NULL,
      market       TEXT,
      industry     TEXT,
      listing_date TEXT,
      is_active    INTEGER DEFAULT 1,
      updated_at   TEXT DEFAULT (datetime('now'))
  );

  CREATE TABLE IF NOT EXISTS prices (
      stock_id    TEXT NOT NULL,
      date        TEXT NOT NULL,
      open        REAL,
      high        REAL,
      low         REAL,
      close       REAL,
      adj_close   REAL,
      volume      INTEGER,
      turnover    REAL,
      source      TEXT,
      ingested_at TEXT DEFAULT (datetime('now')),
      PRIMARY KEY (stock_id, date),
      FOREIGN KEY (stock_id) REFERENCES stocks(stock_id)
  );
  CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

  CREATE TABLE IF NOT EXISTS ingest_log (
      stock_id    TEXT PRIMARY KEY,
      last_date   TEXT,
      last_run_at TEXT,
      rows_total  INTEGER
  );

  CREATE TABLE IF NOT EXISTS data_quarantine (
      stock_id  TEXT,
      date      TEXT,
      raw_json  TEXT,
      reason    TEXT,
      caught_at TEXT DEFAULT (datetime('now'))
  );
  ```
- **完成標準**：檔案存好，SQL 沒有語法錯誤。

## 步驟 8：改乾淨 `database/init_db.py`

- **為什麼**：目前它會塞假資料、還硬寫死路徑 `"database/quant.db"`。
- **怎麼做**：重寫成只做三件事：讀 `DATABASE_PATH`、`executescript(schema.sql)`、
  印出「已建立的表」。**刪掉所有假資料插入的程式碼**。用
  `config.settings.DATABASE_PATH`，不要用相對字串。
- **完成標準**：刪掉 `database/quant.db` 後跑 `python database/init_db.py`，
  重新產生一個空的、有 4 張表的資料庫；再跑一次不會報錯（冪等）。

## 步驟 9：更新 `tests/test_db_connection.py`

- **為什麼**：schema 改了，舊測試（檢查 7 個欄位、檢查 stocks 有資料）會失敗。
- **怎麼做**：
  - 改 `test_prices_table_has_expected_columns` 的 expected 清單為新欄位。
  - 把「stocks 有資料」的測試改成「4 張表都存在」。
  - 新增：`prices` 的主鍵是 `(stock_id, date)`（用 `PRAGMA table_info` 檢查 `pk` 欄位）。
- **完成標準**：`pytest` 全綠。

## 步驟 10：階段 B 收尾

- **怎麼做**：`git commit`，訊息
  `Phase 1 (step 7-9): redesign schema, clean init_db, update tests`。
  開一個 branch `phase1-data-infra` 來做接下來的東西。

---

# 階段 C：Downloader — 把真資料抓下來（第 4 週）

## 步驟 11：定義資料來源抽象介面 `ingestion/sources/base.py`

- **為什麼**：之後若要換來源或加 TWSE 備援，只要多寫一個 class，不用改上層。
- **怎麼做**：
  ```python
  from abc import ABC, abstractmethod
  import pandas as pd

  class PriceSource(ABC):
      @abstractmethod
      def fetch_stock_list(self) -> pd.DataFrame:
          """回傳欄位: stock_id, name, market, industry, listing_date"""

      @abstractmethod
      def fetch_prices(self, stock_id: str, start: str, end: str) -> pd.DataFrame:
          """回傳欄位: date, open, high, low, close, adj_close, volume, turnover"""
  ```
- **完成標準**：檔案能 import，不報錯。

## 步驟 12：實作 `ingestion/sources/finmind_source.py`

- **為什麼**：這是真正打 API 的地方。
- **怎麼做**：`class FinMindSource(PriceSource)`：
  - `__init__` 收 token。
  - `fetch_stock_list`：呼叫 `TaiwanStockInfo`，過濾出「普通股」
    （排除 ETF、權證、特別股），欄位改名對齊介面。
  - `fetch_prices`：同時抓 `TaiwanStockPrice`（open/high/low/close/volume/turnover）
    和 `TaiwanStockPriceAdj`（adj_close），用 date 合併。
  - 加：呼叫之間 `time.sleep`、失敗 `retry` 3 次（指數退避）、HTTP 429 特別處理。
- **完成標準**：`FinMindSource(token).fetch_prices("2330", "2023-01-01", "2023-12-31")`
  回傳一個欄位正確、日期升冪的 DataFrame。

## 步驟 13：`ingestion/downloader.py` — 先建 `stocks` 表

- **怎麼做**：寫 `sync_stock_list(source, conn)`：抓 stock list → 只保留 universe 內的
  → UPSERT 進 `stocks` 表。
- **完成標準**：跑完後 `SELECT COUNT(*) FROM stocks` 等於你的 sample universe 檔數。

## 步驟 14：`ingestion/downloader.py` — 決定每檔要抓的區間

- **為什麼**：增量更新的核心。
- **怎麼做**：寫 `plan_downloads(conn, universe) -> dict[stock_id, (start, end)]`：
  - 查 `ingest_log`，若某股票有 `last_date` → start = last_date + 1 天。
  - 若沒有紀錄 → start = `HISTORY_START_DATE`。
  - end = 今天。
- **完成標準**：第一次跑，每檔都回傳 `(HISTORY_START_DATE, today)`。

## 步驟 15：首次全量下載到暫存

- **怎麼做**：寫 `download_all(source, plan) -> dict[stock_id, DataFrame]`，
  用 `tqdm` 顯示進度，單檔失敗記 log 並跳過。
  **這步先不寫進正式 `prices` 表**，先確認資料抓得對。
- **完成標準**：sample universe 全部抓完，手動檢查其中 2〜3 檔 DataFrame
  （日期連續、adj_close 不為 null、價格合理）。

---

# 階段 D：Cleaner + Validator + Loader（第 5 週）

## 本階段的設計決策：還原股價改成「讀取時計算」

原本的規劃是把 `adj_close` 當成 `prices` 表的一個欄位，在寫入時算好存進去。
進入階段 D 前重新檢查，發現這個設計跟階段 C 的**增量更新會互相衝突**，
所以這裡先改掉，再往下做。

**問題**：`ingestion/adjustment.py` 是後還原（backward adjustment），調整因子只能
從「本次抓取視窗內」的除權息事件算出來。全量第一次跑沒問題，但之後每次除權息，
資料庫裡更早的 `adj_close` 就全部過期了，而增量流程不會回頭改它們。用專案裡真正的
`calculate_adjusted_price` 實測（收盤價固定 100，2024-03-13 除息 10 元），
依視窗邊界落在哪裡，會出現兩種不同的錯：

| 情境 | 症狀 |
|---|---|
| DB 已存到 03-12，新視窗從 03-13（事件是視窗第一列）開始 | 事件當天 `prev_close` 是 NaN（`shift(1)` 取不到視窗外的前一天）→ 調整因子變成 NaN → 被 `shift(-1)` 推出陣列，**這次事件完全沒有被還原**，+11.11% 的報酬整個消失 |
| DB 已存到 03-11，新視窗從 03-12 開始（事件是視窗第二列） | 視窗內的 03-12 被打折、視窗外的 03-11 沒有，**在序列中間憑空生出 -10% 的假跳空**，回測會當成真的暴跌 |

**決定**：`prices` 表只存**原始事實**（raw OHLCV），除權息事件存進 `dividend_events`，
還原價變成**衍生視圖**，由步驟 24 的 `database/data_access.py` 在讀取時即時計算。

**為什麼這樣比較好**：

1. 結構上根除快取過期問題，而不是每次除權息都回頭 UPDATE 歷史列（那會讓
   `ingested_at` 失去意義，稽核性變差）。
2. 原始價是不可變的事實，還原價是隨時可重算的視圖，兩者分開是資料工程的常見分層。
3. 可以做 point-in-time 回溯（「2023-06-01 當下看到的序列長什麼樣」），
   Phase 3 因子研究要避免 look-ahead bias 時會用到。
4. 效能不是問題：38 檔 × 10 年不到 10 萬列，pandas 算一次幾十毫秒。

**代價**：`prices` 少一個欄位、`fetch_prices` 不再回傳 `adj_close`，
`ingestion/adjustment.py` 在步驟 24 之前會暫時沒有呼叫者（不是死碼，是還沒接上）。

---

## 步驟 16：前置重構 — 讓 `prices` 只存原始價

- **為什麼**：見上面的設計決策。這一步要先把資料結構與介面改對，否則後面
  cleaner / loader 都會照著錯的欄位寫，改起來更痛。
- **怎麼做**：
  1. `database/schema.sql`：
     - `prices` 移除 `adj_close` 欄位。
     - `data_quarantine` 補主鍵 `PRIMARY KEY (stock_id, date, reason)`。
       目前這張表沒有主鍵，重跑 ingest 會把同樣的壞資料一直往裡疊，
       數字會愈跑愈大，看不出「這次真的隔離了幾筆」。
  2. `ingestion/sources/base.py`：`fetch_prices` 的 docstring 改成回傳
     `date, open, high, low, close, volume, turnover`（不含 `adj_close`），
     並註明還原價由 `database/data_access.py` 在讀取時計算。
  3. `ingestion/sources/finmind_source.py`：
     - `fetch_prices` 不再呼叫 `fetch_dividends` / `calculate_adjusted_price`，
       欄位改名後直接回傳原始價。
     - `_call_with_retry` 補上速率限制的特別處理（步驟 12 原本就要求但沒做）：
       FinMind 是**每小時** 600 次的額度，撞到時 1→2→4 秒的退避完全不夠，
       會三次全敗。訊息含 rate limit / 429 時要嘛拉長等待、要嘛直接放棄並回報，
       不要假裝重試過。
  4. 跟著改的測試：
     - `tests/test_db_connection.py` 的 expected 欄位清單。
     - `tests/test_downloader.py` 的 `PRICE_COLUMNS`。
     - `tests/test_finmind_source.py` 的 `test_fetch_prices_adjusts_close_using_total_dividend`
       → 搬到步驟 17 的 `tests/test_adjustment.py`，改成直接測
       `calculate_adjusted_price`；原地改留一個「`fetch_prices` 不該再打股利 API」
       的測試（用假 DataLoader 的 `calls` 驗證）。
  5. 刪掉 `database/quant.db` 重跑 `python database/init_db.py`。
     `prices` 目前是空的，沒有資料損失；`stocks` 那 38 檔下次跑 ingest 會自動重建。
- **完成標準**：`pytest` 全綠；`PRAGMA table_info(prices)` 看不到 `adj_close`；
  `FinMindSource.fetch_prices` 回傳 7 個欄位，而且只打了一次 `taiwan_stock_daily`。

## 步驟 17：修 `calculate_adjusted_price` 的同日多筆事件缺陷

- **為什麼**：`calculate_adjusted_price` 內部用 `pd.merge(df, div, on="date")` 合併
  除權息事件，**沒有保證 `date` 唯一**。同一天出現兩筆事件（例如現金與股票分兩筆公告）
  時，價格列會被複製一份，還原也會被重複打折。實測：輸入 3 列價格 + 同日兩筆事件
  （現金 5 元、股票每 10 股配 10 股），輸出變成 4 列，`adj_close` 被折成 47.5
  （正確值 95）。同日同時除權又除息在台股並不罕見，這個地雷遲早會踩到，
  而且步驟 24 之後**所有**還原價都會經過這個函式，影響面比現在大。
- **怎麼做**：在 `calculate_adjusted_price` 開頭先把事件依日期彙總：
  ```python
  div = (df_div[["date", "cash_div", "stock_div"]]
         .groupby("date", as_index=False)
         .sum())
  ```
  相加是正確的做法：證交所公式 `P_ref = (P_prev − D) / (1 + S)` 本來就同時吃現金
  股利與股票股利，同一天的多筆事件合併成一筆 `(D_total, S_total)` 才符合公式；
  拆開來連套兩次反而會在第二次用到已經被折過的 `P_prev`，算出更錯的數字。
- 新增 `tests/test_adjustment.py`：
  - 同日兩筆事件 → 輸出列數等於輸入列數，且 `adj_close` 正確（回歸測試）
  - 從 `test_finmind_source.py` 搬過來的 2409 友達案例（全部配在公積）
  - 多次事件累乘（用 `docs/data_source.md` 記錄的 6669 兩次真實事件）
  - 事件當天不打折（守住目前 `shift(-1)` 那個修正，別在重構時弄壞）
- **完成標準**：`pytest tests/test_adjustment.py` 全綠；把 `groupby` 拿掉，
  同日多筆事件那個 test 會失敗（突變測試確認測試真的有守住東西）。

## 步驟 18：`ingestion/cleaner.py`

- **怎麼做**：寫 `clean(df, stock_id) -> pd.DataFrame`，做：
  - 日期統一成 `YYYY-MM-DD` 字串
  - 數值欄位轉 float / int，無法轉的設 NaN
  - 依 `date` 去重（保留最後一筆）、升冪排序
  - 補上 `stock_id`、`source="FinMind"` 欄位
  - 欄位順序對齊步驟 16 之後的 `prices` 表：
    `stock_id, date, open, high, low, close, volume, turnover, source`
    （**沒有 `adj_close`**）
- **完成標準**：丟一個「有重複日期、有字串數字、亂序」的假 DataFrame 進去，出來是乾淨的。

## 步驟 19：cleaner 測試 `tests/test_cleaner.py`

- **怎麼做**：針對上面每一種髒狀況各寫一個 test case。
- **完成標準**：`pytest tests/test_cleaner.py` 全綠。

## 步驟 20：`ingestion/validator.py`

- **怎麼做**：寫 `validate(df, ex_dates=None) -> tuple[good_df, bad_df]`，
  `bad_df` 附 `reason`。規則：
  - `high >= low`、`high >= open`、`high >= close`、`low <= open`、`low <= close`
  - `open/high/low/close > 0`
  - `volume >= 0`
  - `date` 不重複
  - 單日 `close` 相對前一日漲跌幅 `abs > 0.15` → 標記 `reason="price_jump"`
    （**標記但不隔離**，因為可能是真的，例如減資）
  - 前四類違規 → 進 `bad_df`
- **為什麼多一個 `ex_dates` 參數**：`price_jump` 是拿**原始價**算的，而除權息當天
  原始價本來就會跳空。6669 緯穎 2026-09-02 是 7800 → 2610（-66%），
  2409 友達每年除息也都會觸發。不排除的話，quarantine 報表會被除權息洗版，
  真正需要人看的異常反而被埋掉。`ex_dates` 是一個「該股除權息交易日」的集合，
  由步驟 23 的 orchestrator 從剛抓到的股利事件傳進來；這些日子跳過 `price_jump` 檢查。
- **完成標準**：丟一個含 `high < low` 的列，它會被分到 `bad_df` 且 reason 正確；
  同樣一個 -30% 的跌幅，日期在 `ex_dates` 裡不會被標記、不在裡面會被標記。

## 步驟 21：validator 測試 `tests/test_validator.py`

- **怎麼做**：每條規則一個 test case，`price_jump` 要寫「除權息日 / 非除權息日」
  兩個對照組。
- **完成標準**：全綠。

## 步驟 22：`ingestion/loader.py` + 冪等測試 `tests/test_loader.py`

- **怎麼做**：四個函式
  - `upsert_prices(conn, good_df)`：
    `INSERT INTO prices (...) VALUES (...) ON CONFLICT(stock_id, date) DO UPDATE SET ...`
  - `upsert_dividends(conn, stock_id, div_df)`：寫進 `dividend_events`，
    `ON CONFLICT(stock_id, ex_date) DO UPDATE`。
    **這是新增的、也是整個階段 D 最關鍵的一步** —— 這張表從階段 B 建好到現在
    一直是空的，而步驟 24 讀取時要還原股價，唯一的資料來源就是它。
    沒有這一步，步驟 24 無米可炊。
  - `quarantine(conn, bad_df)`：寫進 `data_quarantine`（`raw_json` 存整列的 JSON），
    用步驟 16 加的主鍵做 `ON CONFLICT(stock_id, date, reason) DO UPDATE`，
    重跑不會疊加。
  - `update_ingest_log(conn, stock_id, df)`：更新 `last_date`、`last_run_at`、`rows_total`
- **注意寫入順序**：`database/connection.py` 強制開啟外鍵檢查，而 `prices`、
  `ingest_log`、`dividend_events` 三張表都有 FK 指向 `stocks`。
  所以它們一律要在 `sync_stock_list` 之後才能寫，否則會 `IntegrityError`。
- **測試**：同一份資料連續跑兩次，`prices` / `dividend_events` / `data_quarantine`
  三張表的列數都不變；第二次跑會更新 `ingested_at` 但不新增列。
- **完成標準**：`pytest tests/test_loader.py` 全綠。

## 步驟 23：`ingestion/run_ingest.py` — 串起整條管線並實跑

- **怎麼做**：orchestrator，流程：
  ```
  connect DB
  → sync_stock_list
  → plan_downloads
  → for each stock:
        fetch_dividends  → upsert_dividends        （先做，validate 需要 ex_dates）
        fetch_prices     → clean
                         → validate(df, ex_dates)
                         → upsert_prices + quarantine
                         → update_ingest_log
  → 印出摘要: 新增 X 列 / 更新 Y 列 / 隔離 Z 列 / 除權息事件 E 筆 / 涵蓋 N 檔 / 日期範圍 ...
  ```
  加 `argparse`：`--mode sample|all`、`--stock 2330`（只跑單檔，方便除錯）。
- **執行方式是 `python -m ingestion.run_ingest --mode sample`**，
  不是 `python ingestion/run_ingest.py`。已實測：`ingestion/` 裡用的是相對 import
  （`from .sources.base import ...`），直接把檔案當腳本跑會得到
  `ImportError: attempted relative import with no known parent package`。
  這點跟原規劃寫的指令不同，以這裡為準。
- **API 額度**：sample 的 38 檔 × 每檔 3 次呼叫（daily + dividend + dividend_result）
  = 114 次，安全落在 FinMind 免費的 600 次/hr 內。步驟 29 要放大到全市場時，
  1700+ 檔 × 3 ≈ 5100 次會直接爆掉，那時才需要分批或跨小時排程。
- **實跑並檢查**：跑 `--mode sample` 把資料真正灌進 `quant.db`，然後：
  - `SELECT COUNT(*) FROM prices` 應該有數萬列
  - `SELECT COUNT(*) FROM dividend_events` 應該接近 `docs/data_source.md` 記錄的
    464 筆（2015 年至今、38 檔）
  - `SELECT reason, COUNT(*) FROM data_quarantine GROUP BY reason`
    裡面每一類你都要能解釋為什麼
- **完成標準**：一次跑完並印出摘要；`prices` 有數萬列真實資料；
  `dividend_events` 不是空的；quarantine 裡的東西你都能解釋。

---

# 階段 E：資料存取層 + EDA + 驗收（第 6 週）

## 步驟 24：`database/data_access.py`

- **為什麼**：Phase 2 回測引擎只會透過這層拿資料，不會自己碰 SQL。
  而且經過階段 D 的設計調整，**還原股價是在這一層算的**（`prices` 表只存原始價，
  `dividend_events` 存事件），所以這層不只是包 SQL，它是還原價唯一的產出點。
- **怎麼做**：
  ```python
  def get_prices(stock_id, start=None, end=None, adjusted=True) -> pd.DataFrame
  def get_universe(active_only=True) -> list[str]
  def get_price_matrix(stock_ids, field="adj_close", start=None, end=None) -> pd.DataFrame
  # get_price_matrix: index=date, columns=stock_id
  ```
  `adjusted=True` 時：撈原始價 → 撈該股的 `dividend_events` →
  用 `ingestion/adjustment.py` 的 `calculate_adjusted_price` 算出 `adj_close`。
  全部走 `config.settings.DATABASE_PATH`。
- **⚠️ 最容易錯的地方**：要套用的事件是「`ex_date` 晚於查詢區間起點的**全部**事件」，
  **不能被查詢的 `end` 限制住**。例如 `get_prices("2330", "2020-01-01", "2020-12-31")`
  必須把 2021 年到今天的所有除權息都算進去，否則這段 2020 年的序列會跟
  「查 2015〜今天再切出 2020 年」那段對不起來，同一檔股票在不同查詢下報酬會不一致。
- **完成標準**：`get_prices("2330", "2023-01-01")` 回傳一個以 date 為 index 的 DataFrame；
  且 `get_prices("2330", "2020-01-01", "2020-12-31")` 的 `adj_close`
  跟 `get_prices("2330", "2015-01-01").loc["2020"]` 完全一致。

## 步驟 25：data_access 測試 `tests/test_data_access.py`

- **怎麼做**：測日期過濾正確、`adjusted=True/False` 拿到不同欄位、
  `get_price_matrix` 形狀正確。
- **完成標準**：全綠。

## 步驟 26：`notebooks/02_data_quality_eda.ipynb`（Phase 1 主要驗收品）

- **怎麼做**，做出這幾張圖 / 表：
  1. 輸入一個代碼 → `close` vs `adj_close` 疊圖（看得到除權息缺口被填平）
  2. 成交量副圖
  3. `describe()` + 年化波動率（`daily_return.std() * sqrt(252)`）
  4. 全 universe 覆蓋率：每檔股票的資料起訖日、總天數、缺漏天數（對照交易日曆）
  5. quarantine 統計：各 reason 幾筆
- **完成標準**：這個 notebook 從頭跑到尾不報錯，就是規劃書說的
  「輸入股票代碼，能撈出歷史資料並畫出價格、成交量、基本統計」。

## 步驟 27：驗證增量更新

- **怎麼做**：隔一天（或改 code 模擬），再跑一次 `run_ingest.py --mode sample`，
  確認摘要顯示「只新增了 1 天的資料」，不是整包重抓。
- **完成標準**：第二次跑很快、新增列數 ≈ universe 檔數。

## 步驟 28：補文件

- **怎麼做**：
  - 新增 `docs/data_dictionary.md`：4 張表每個欄位的意義、單位、範例值。
  - 更新 `README.md` 的 pipeline 圖，加入
    `Source → Downloader → Cleaner → Validator → Loader → DB` 與 universe 說明。
  - 更新 `docs/journal.md`：Phase 1 做了什麼、卡關與解法、下一步（Phase 2 回測引擎）。
- **完成標準**：陌生人讀 docs 就能看懂資料怎麼進來、資料庫長什麼樣。

## 步驟 29：擴大到全市場（可選，行有餘力）

- **怎麼做**：`UNIVERSE_MODE = "all"`，`run_ingest.py --mode all` 跑一次。
  觀察 API 額度、耗時、quarantine 量。若太吃力就留在 sample，並在 journal 記錄原因。
- **完成標準**：知道全市場跑一次要多久、會遇到什麼問題（就算最後決定先不做）。

## 步驟 30：Phase 1 收尾

- **怎麼做**：
  - `pytest` 全綠。
  - `git` 把 `phase1-data-infra` branch 發 PR、合併回 `main`。
  - 更新 `README.md` 進度：`- [x] Phase 1`。
- **完成標準**：main branch 上有一套能跑的資料管線 + 一份資料品質 EDA notebook。

---

## Phase 1 最終檢查清單

- [ ] `requirements.txt` 正常編碼、含 FinMind
- [ ] `docs/data_source.md`（來源評估 + 還原股價說明）
- [ ] `database/schema.sql`：stocks / prices(只存原始價) / dividend_events /
      ingest_log / data_quarantine(有主鍵)
- [ ] `database/init_db.py`：只建表、冪等、走 settings 路徑
- [ ] `ingestion/`：base、finmind_source、downloader、cleaner、validator、loader、run_ingest
- [ ] `tests/`：adjustment、cleaner、validator、loader、data_access 測試全綠
- [ ] `database/data_access.py`：get_prices / get_universe / get_price_matrix
      （還原價在這層即時計算）
- [ ] `notebooks/02_data_quality_eda.ipynb` 跑得動
- [ ] 增量更新驗證過
- [ ] `docs/data_dictionary.md` + README pipeline 圖 + journal 更新
- [ ] PR 合併，README 進度勾 Phase 1

---

## 這階段練到的能力（履歷用語）

- ETL pipeline 設計：來源抽象化、增量更新、冪等寫入、失敗隔離
- 資料品質工程：validation rules、quarantine、覆蓋率報告
- SQL schema 設計：複合主鍵、索引、正規化
- 金融資料領域知識：除權息 / 還原股價、台股市場結構、存活者偏誤
