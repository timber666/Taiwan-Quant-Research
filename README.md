# Taiwan Quant Research Platform

台股量化研究與回測平台

## Project Goal

建立一套從金融資料 → 特徵 → 策略 → 模型 → 回測 → 績效分析的完整量化研究流程。
目標不是預測明天哪支股票會漲，而是打造一套可驗證、可重現的量化研究方法論。

## Research Pipeline

```
取得台股資料
    ↓
資料清洗 / 資料庫
    ↓
建立 Features
    ↓
研究投資假設
    ↓
建立策略
    ↓
Backtesting
    ↓
Machine Learning
    ↓
Portfolio Construction
    ↓
Risk Management
    ↓
Performance Analysis
    ↓
Web Dashboard
```

## 目前進度

- [x] Phase 0：專案設計與環境建立
- [ ] Phase 1：建立金融資料基礎設施 — 階段 A〜D 完成，進行中
- [ ] Phase 2：Backtesting Engine
- [ ] Phase 3：Factor Research
- [ ] Phase 4：Machine Learning
- [ ] Phase 5：Portfolio + Risk Management
- [ ] Phase 6：Productization

目前狀態：**Phase 1 進行中**。資料管線
（`Source → Downloader → Cleaner → Validator → Loader → SQLite`）已經可以跑，
sample universe 38 檔、2015 年至今約 10.5 萬列日線已灌進資料庫。
下一步是階段 E：資料存取層（還原股價在這層即時計算）與資料品質 EDA。
細部規劃見 [`docs/phase1_plan.md`](docs/phase1_plan.md)。

## Data Pipeline

```
FinMind API
    ↓  ingestion/sources/      各資料來源的實作，只負責如實回報原始資料
    ↓  ingestion/downloader.py 決定抓誰、抓哪段（增量更新）
    ↓  ingestion/cleaner.py    格式：型別、日期寫法、去重、欄位順序
    ↓  ingestion/validator.py  內容：分成「可用」與「要人看一眼」兩份
    ↓  ingestion/loader.py     UPSERT 進資料庫，整檔一個交易
SQLite (database/quant.db)
    ↓  database/data_access.py 讀取時才計算還原股價（階段 E）
研究與回測
```

執行方式：

```bash
python -m ingestion.run_ingest --mode sample
```

**還原股價（adj_close）不存在資料庫裡**，`prices` 只存原始成交價，
除權息與減資事件分別存在 `dividend_events` 與 `corporate_actions`。
理由是後還原的調整因子會隨著新事件改變，存下來的值在增量更新下必定過期；
詳見 [`database/schema.sql`](database/schema.sql) 裡 `prices` 的註解。

## Project Structure

```
Taiwan-Quant-Research/
│
├── data/               # 原始/處理後的資料
├── database/           # 資料庫 schema 與初始化腳本
├── ingestion/          # 資料下載與更新程式
├── features/           # 特徵工程
├── strategies/         # 交易策略邏輯
├── backtest/           # 回測引擎
├── models/             # 機器學習模型
├── portfolio/          # 投資組合建構
├── risk/               # 風險管理
├── dashboard/          # Web Dashboard
├── notebooks/          # 研究用 Jupyter Notebook
├── tests/              # 單元測試
├── docs/               # 技術文件與筆記
├── config/             # 設定檔
└── requirements.txt
```

## Setup

```bash
git clone https://github.com/timber666/Taiwan-Quant-Research.git
cd Taiwan-Quant-Research

# 建立虛擬環境（建議使用 Python 3.11）
python -m venv .venv
.venv\Scripts\Activate.ps1     # Windows

# 安裝套件
pip install -r requirements.txt
```