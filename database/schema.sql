CREATE TABLE IF NOT EXISTS stocks (
    stock_id TEXT PRIMARY KEY,
    name TEXT,
    industry TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    date TEXT,
    stock_id TEXT,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    FOREIGN KEY (stock_id) REFERENCES stocks(stock_id)
);