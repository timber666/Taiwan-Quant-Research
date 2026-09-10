import os
from pathlib import Path

# 專案根目錄的絕對路徑（不管在哪裡執行程式都能正確定位）
BASE_DIR = Path(__file__).resolve().parent.parent

# 資料庫路徑
DATABASE_PATH = BASE_DIR / "database" / "quant.db"

# 之後 Phase 1 接資料 API 時，金鑰之類的敏感資訊會從這裡讀取
# 範例（先寫著，之後有需要再實際使用）：
# API_KEY = os.getenv("FINMIND_API_KEY")