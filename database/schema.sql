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

CREATE TABLE IF NOT EXISTS prices (
    stock_id    TEXT NOT NULL,
    date        TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,                        -- 原始收盤價
    adj_close   REAL,                        -- 還原收盤價（回測用這個算報酬）
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

CREATE TABLE IF NOT EXISTS data_quarantine (
    stock_id  TEXT,
    date      TEXT,
    raw_json  TEXT,
    reason    TEXT,
    caught_at TEXT DEFAULT (datetime('now'))
);

-- 以下這張表不在 phase1_plan.md 原本步驟 7 的規劃裡，是根據
-- notebooks/_spike_datasource.ipynb 的還原股價驗證結果加的：
-- prices.adj_close 是用 cash_div/stock_div 計算出來的，若不把這些事件存下來，
-- 之後要重算、稽核、或補算新事件時就無跡可循，所以額外開一張表持久化。
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
