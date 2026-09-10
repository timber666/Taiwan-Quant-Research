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
- [ ] Phase 1：建立金融資料基礎設施
- [ ] Phase 2：Backtesting Engine
- [ ] Phase 3：Factor Research
- [ ] Phase 4：Machine Learning
- [ ] Phase 5：Portfolio + Risk Management
- [ ] Phase 6：Productization

目前狀態：Phase 0 完成，準備進入 Phase 1。

## Project Structure

```
Taiwan-Quant-Research/
│
├── data/          # 原始/處理後的資料
├── database/      # 資料庫 schema 與初始化腳本
├── ingestion/      # 資料下載與更新程式
├── features/       # 特徵工程
├── strategies/      # 交易策略邏輯
├── backtest/       # 回測引擎
├── models/         # 機器學習模型
├── portfolio/       # 投資組合建構
├── risk/          # 風險管理
├── dashboard/       # Web Dashboard
├── notebooks/       # 研究用 Jupyter Notebook
├── tests/         # 單元測試
├── docs/          # 技術文件與筆記
├── config/         # 設定檔
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