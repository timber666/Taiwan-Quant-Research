# Taiwan Quant Research Platform

> **本專案已停止開發（2026-11-02），研究在 [stock-screener](https://github.com/timber666/stock-screener) 延續。**
> 本專案完成到 Phase 1（資料基礎建設）；其中驗證過的還原權息計算與測試案例已移植到 stock-screener，
> 後續的因子研究、組合回測等規劃見該專案的 [RESEARCH_PLAN.md](https://github.com/timber666/stock-screener/blob/main/RESEARCH_PLAN.md)。
> 本 repo 保留作為參考，不再更新。

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
- [x] Phase 1：建立金融資料基礎設施
- [ ] Phase 2：Backtesting Engine
- [ ] Phase 3：Factor Research
- [ ] Phase 4：Machine Learning
- [ ] Phase 5：Portfolio + Risk Management
- [ ] Phase 6：Productization

目前狀態：**Phase 1 完成**。整條管線
（`Source → Downloader → Cleaner → Validator → Loader → SQLite → Data Access`）
已經跑通並通過驗收：sample universe 38 檔、2015 年至今約 10.5 萬列日線，
增量更新實測正確，資料品質報告見
[`notebooks/02_data_quality_eda.ipynb`](notebooks/02_data_quality_eda.ipynb)。
細部規劃見 [`docs/phase1_plan.md`](docs/phase1_plan.md)。

下一步是 Phase 2：回測引擎。

## Data Pipeline

```
FinMind API
    ↓  ingestion/sources/      各資料來源的實作，只負責如實回報原始資料
    ↓  ingestion/downloader.py 決定抓誰、抓哪段（增量更新）
    ↓  ingestion/cleaner.py    格式：型別、日期寫法、去重、欄位順序
    ↓  ingestion/validator.py  內容：分成「可用」與「要人看一眼」兩份
    ↓  ingestion/loader.py     UPSERT 進資料庫，整檔一個交易
SQLite (database/quant.db)
    ↓  database/data_access.py 讀取時才計算還原股價；回測只跟這一層打交道
研究與回測
```

執行方式：

```bash
python -m ingestion.run_ingest --mode sample     # 增量更新（只抓沒有的部分）
python -m ingestion.run_ingest --stock 2330      # 只跑單一檔，除錯用
python -m ingestion.run_ingest --full            # 忽略進度紀錄，整段歷史重抓
```

第一次執行會抓 2015 年至今、約 1 分鐘；之後每天執行只會補上新的交易日
（實測 38 檔 × 3 個交易日 = 114 列，不會重抓已有的資料）。

### 投資範圍（universe）

Phase 1 先鎖定 **38 檔上市普通股**，定義在
[`config/settings.py`](config/settings.py) 的 `SAMPLE_UNIVERSE`。

挑選方式：用 TWSE OpenAPI 的當日成交金額排名，交叉比對 FinMind 的產業分類，
**每個產業最多取 3 檔**——不這樣做的話整個 universe 會被電子權值股佔滿，
之後做因子研究時產業效應會蓋過因子本身。最後涵蓋 15 個產業。

先小範圍跑通是刻意的：全市場 1700+ 檔一次抓會打爆 API 免費額度、也難除錯。
放大到全市場的方式是 `--mode all`，但那需要先解決 API 呼叫次數的問題
（見 `docs/journal.md`）。

**還原股價（adj_close）不存在資料庫裡**，`prices` 只存原始成交價，
除權息與減資事件分別存在 `dividend_events` 與 `corporate_actions`。
理由是後還原的調整因子會隨著新事件改變，存下來的值在增量更新下必定過期；
詳見 [`database/schema.sql`](database/schema.sql) 裡 `prices` 的註解。

取用資料一律走 `database/data_access.py`，不要自己寫 SQL 查 `prices`：

```python
from database.data_access import get_prices, get_price_matrix, get_universe

df = get_prices("2330", "2024-01-01")     # 含 adj_close 的日線
mat = get_price_matrix(field="adj_close")  # index=date, columns=stock_id
```

## 文件

| 文件 | 內容 |
|---|---|
| [`docs/data_source.md`](docs/data_source.md) | 資料來源選型比較、已知限制、踩過的五個坑 |
| [`docs/data_dictionary.md`](docs/data_dictionary.md) | 六張表每個欄位的意義、單位、範例值 |
| [`docs/phase1_plan.md`](docs/phase1_plan.md) | Phase 1 的逐步執行規劃 |
| [`docs/journal.md`](docs/journal.md) | 開發日誌：做了什麼、卡在哪、怎麼解 |
| [`docs/notes.md`](docs/notes.md) | 學習筆記 |
| [`notebooks/02_data_quality_eda.ipynb`](notebooks/02_data_quality_eda.ipynb) | 資料品質報告（Phase 1 驗收品） |

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
