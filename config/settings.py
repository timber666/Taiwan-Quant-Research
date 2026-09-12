import os
from pathlib import Path
from dotenv import load_dotenv

# 專案根目錄的絕對路徑（不管在哪裡執行程式都能正確定位）
BASE_DIR = Path(__file__).resolve().parent.parent

# 資料庫路徑
DATABASE_PATH = BASE_DIR / "database" / "quant.db"

# 讀取金鑰
load_dotenv(BASE_DIR / ".env")
FINMIND_API_TOKEN = os.getenv("FINMIND_API_TOKEN")
