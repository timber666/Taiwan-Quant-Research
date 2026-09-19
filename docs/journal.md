# 開發日誌

## Phase 0（第 1〜2 週）｜專案設計與環境建立

**做了什麼：**

- 建立專案架構、建立14個資料夾
- 學習使用 Github Desktop 進行 Commit, Branch , Merge 流程
- 用 SQLite 來學習資料庫基本語法
- 輸入假資料，並用 jupyter notebook 來練習 Pandas 語法
- 建立 config/settings.py 集中管理路徑設定，.env 管理敏感資訊
- 寫了第一批 pytest 測試，驗證資料庫連線和表格結構

**卡關與解法：**

- 重跑 init_db.py 出現 "table already exists" → schema.sql 的 CREATE TABLE 改成 CREATE TABLE IF NOT EXISTS 解決

**下一步：**
Phase 1 要花時間先評估台股資料來源（TWSE OpenAPI / FinMind），
這是原規劃裡提醒過最容易卡關的地方，先花 2〜3 天專門做這件事。
---

## Phase 1 階段 A〜B（第 3 週）｜資料來源評估與 Schema 重設計

**做了什麼：**

- 實測 FinMind / TWSE OpenAPI / MOPS 三個來源，寫成 `docs/data_source.md` 的比較表
- 決定主來源用 FinMind，還原股價自行計算（付費會員才拿得到 `taiwan_stock_daily_adj`）
- 定義 sample universe：38 檔、涵蓋 18 個產業，避免整包被電子股佔滿
- 重寫 `schema.sql`：prices 加複合主鍵與索引、新增 `ingest_log` 與 `data_quarantine`

**卡關與解法：**

- 免費會員拿不到還原股價 → 改抓除權息公告，用證交所公式自己算，
  並用 6669 緯穎的兩次真實事件對照官方 `reference_price` 驗證
- 還原價累乘時把事件當天自己也算進去，導致當天價格被多打一次折（2610 被還原成 875）
  → `adj_factor` 先 `shift(-1)` 再倒序累乘，讓因子只套用到更早的日期

## Phase 1 階段 C（第 4 週）｜Downloader

**做了什麼：**

- `PriceSource` 抽象介面 + `FinMindSource` 實作，上層不依賴特定資料來源
- `plan_downloads` 查 `ingest_log` 算增量區間，`download_all` 逐檔抓取且單檔失敗不中斷

**卡關與解法（都是實測才發現的）：**

- **股利只取了「盈餘配發」、漏掉「公積配發」**。證交所的參考價公式用的是兩者總和。
  掃過 universe 2015 年至今 464 筆事件，27 筆公積不為零：2454 聯發科 2020-07-06
  少算一半、2409 友達連續三年全部配在公積等於完全沒還原。
  教訓是**單一樣本驗證通過不等於邏輯正確**——6669 那兩次剛好公積是 0。
- **股利 API 的日期過濾套在「權利分派基準日」而不是「除權息交易日」**，
  兩者差 6〜10 天，直接拿 `end` 去查會漏掉區間尾端的事件。改成查詢時放寬 30 天，
  回來後自己用 `ex_date` 濾回區間。
- **SQLite 的外鍵預設是關閉的**，而且是每個連線各自設定，所以 schema 裡的
  `FOREIGN KEY` 一直是裝飾品 → 新增 `database/connection.py` 當連線的唯一入口。
- **`is_active` 永遠不會變成 0**，分不出「還在市場上」與「已下市」，
  這是存活者偏誤的來源 → `sync_stock_list` 補上下市標記。

## Phase 1 階段 D（第 5 週）｜Cleaner + Validator + Loader

**做了什麼：**

- 進階段 D 前先做了一次架構調整：**還原股價從「寫入時存欄位」改成「讀取時計算」**。
  `prices` 只存原始價，事件存 `dividend_events` / `corporate_actions`。
- `cleaner`（格式）→ `validator`（內容，分致命／標記兩種）→ `loader`（四張表的 UPSERT）
- `run_ingest.py` 串起整條管線，逐檔 commit，額度用完會中止並保留已完成的部分
- 實跑 sample universe：prices 約 10.5 萬列、dividend_events 465 筆

**卡關與解法：**

- **存下來的 `adj_close` 在增量更新下必定過期**。實測依視窗邊界會出現兩種錯：
  事件落在視窗第一列時整個事件不被還原；落在第二列時序列中間冒出假跳空。
  → 改成讀取時計算，結構上根除這個問題。
- **同一天有多筆除權息公告時，merge 會把價格列複製、而且還原價算錯**
  （200 元被算成 95.0，正確是 97.5）→ 合併前先依日期 `groupby().sum()`。
- **減資與面額變更也會讓股價跳空**。實跑後 `price_jump` 標出 18 筆大跳空，
  逐一查證發現 11 筆是減資或面額變更（2609 陽明 2017 年減資若不還原，
  回測會看成一天暴漲 116%）→ 新增 `corporate_actions` 表。
  這是整個階段 D 唯一從「實際資料反推出需求」而不是照計畫做的部分。
- **興櫃時期的 `open` 不可靠**（議價成交、沒有集合競價），會落在 `[low, high]` 之外。
  一開始把它當致命錯誤，結果擋掉 392 天的資料 → 改成只標記不隔離。
  後續複查又發現同一類問題：上市首日與停牌復牌當天 `open` 是 0，
  整列被 `non_positive_price` 丟掉（6919 康霈那天成交了 767 萬股）
  → `open` 從致命規則裡拆出來，獨立成 `non_positive_open` 只標記。

**下一步：**
階段 E（步驟 24〜30）：`database/data_access.py` 把還原股價算出來，
然後做資料品質 EDA notebook 當 Phase 1 的驗收品。
