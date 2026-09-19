-- Phase 1 資料庫 schema
-- 設計依據：docs/phase1_plan.md 步驟 7

CREATE TABLE IF NOT EXISTS stocks (
    stock_id     TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    market       TEXT,                       -- 'twse'（上市） / 'tpex'（上櫃）
    industry     TEXT,
    listing_date TEXT,
    is_active    INTEGER DEFAULT 1,
    updated_at   TEXT DEFAULT (datetime('now'))
);

-- prices 只存「原始事實」，不存還原價。
-- 原本這裡有一個 adj_close 欄位，階段 D 步驟 16 拿掉了，理由：
-- 還原價是後還原（backward adjustment），每發生一次除權息，比它更早的所有還原價
-- 就全部要重算。增量更新每次只抓新的幾天，不可能回頭改歷史列，於是存下來的
-- adj_close 會過期——實測會出現「事件完全沒被還原」或「序列中間冒出假跳空」兩種錯。
-- 現在改成：原始價存這裡、除權息事件存 dividend_events，還原價由
-- database/data_access.py 在讀取時即時計算，永遠不會過期。
CREATE TABLE IF NOT EXISTS prices (
    stock_id    TEXT NOT NULL,
    date        TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,                        -- 原始收盤價（未還原）
    volume      INTEGER,                     -- 成交股數
    turnover    REAL,                        -- 成交金額
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
    rows_total  INTEGER,
    FOREIGN KEY (stock_id) REFERENCES stocks(stock_id)
);

-- 被 validator 擋下來的壞資料。
-- 主鍵 (stock_id, date, reason)：沒有主鍵的話，每重跑一次 ingest 就會把同樣的壞資料
-- 再疊一份進來，數字愈跑愈大，看不出「這次真的隔離了幾筆」。有主鍵才能用 UPSERT 寫入。
-- 刻意「不」對 stocks 設外鍵：這張表的用途就是收容有問題的資料，包含 stock_id 本身
-- 就是錯的那種；如果連進得來都要先通過外鍵檢查，真正最可疑的資料反而會被擋在門外。
CREATE TABLE IF NOT EXISTS data_quarantine (
    stock_id  TEXT NOT NULL,
    date      TEXT NOT NULL,
    raw_json  TEXT,
    reason    TEXT NOT NULL,
    caught_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (stock_id, date, reason)
);

-- 這張表不在 phase1_plan.md 原本步驟 7 的規劃裡，是根據
-- notebooks/_spike_datasource.ipynb 的還原股價驗證結果加的。
-- 階段 D 步驟 16 之後它的地位又更重要了：prices 不再存 adj_close，
-- 這張表是計算還原價的**唯一**資料來源，也是事後稽核「某天的折價是怎麼來的」的依據。
-- 除權息以外、同樣會讓股價「跳空但不是真的漲跌」的公司行動：減資與面額變更。
-- 這張表是步驟 23 實跑後才加的：當時 validator 的 price_jump 標記出 18 筆大跳空，
-- 逐一查證後發現其中 11 筆是減資或面額變更（例如 2609 陽明 2017-05-04 從 6.15
-- 變成 13.16、6919 康霈 2025-07-21 從 1215 變成 121.5），它們完全不在
-- dividend_events 裡，等於這些股票的還原價會是錯的——跟一開始修掉的除權息問題
-- 是同一類錯誤，只是來源不同。
--
-- 跟 dividend_events 分開存，是因為調整方式不一樣：除權息要用證交所公式從股利
-- 算出參考價，這裡則是官方直接公告「事件前收盤價」與「事件後參考價」，
-- 兩者相除就是調整因子，不需要也不該自己再推導一次。
CREATE TABLE IF NOT EXISTS corporate_actions (
    stock_id     TEXT NOT NULL,
    ex_date      TEXT NOT NULL,            -- 減資恢復買賣日 / 面額變更生效日
    action_type  TEXT NOT NULL,            -- 'capital_reduction' | 'split'
    before_price REAL NOT NULL,            -- 官方公告的事件前收盤價
    after_price  REAL NOT NULL,            -- 官方公告的事件後參考價
    note         TEXT,                     -- 減資原因 / 分割類型
    source       TEXT,
    ingested_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (stock_id, ex_date, action_type),
    FOREIGN KEY (stock_id) REFERENCES stocks(stock_id)
);

CREATE TABLE IF NOT EXISTS dividend_events (
    stock_id        TEXT NOT NULL,
    ex_date         TEXT NOT NULL,            -- 除權息交易日
    cash_div        REAL DEFAULT 0,           -- 現金股利（每股）
    stock_div       REAL DEFAULT 0,           -- 股票股利（每 10 股配股數，非每股）
    reference_price REAL,                     -- 官方除權息參考價（用來交叉驗證還原計算）
    source          TEXT,
    ingested_at     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (stock_id, ex_date),
    FOREIGN KEY (stock_id) REFERENCES stocks(stock_id)
);
