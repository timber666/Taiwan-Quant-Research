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
