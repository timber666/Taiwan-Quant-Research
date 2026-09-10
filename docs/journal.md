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