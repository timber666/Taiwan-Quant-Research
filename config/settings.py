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

# Phase 1 投資範圍（universe）
# 先鎖小範圍跑通整條 pipeline，之後再放大到全市場。
#
# 產生方式（2026-09-13）：TWSE OpenAPI `STOCK_DAY_ALL`（上市股票當日成交金額，免費）
# 依成交金額排名，交叉比對 FinMind `taiwan_stock_info` 的產業分類，每個產業最多取 3 檔
# 避免整個 universe 被電子業龍頭股占滿，取前 40 
# 目前僅涵蓋上市（TWSE），不含上櫃（TPEx，例如 6669 緯穎）。
UNIVERSE_MODE = "sample"  # "sample" | "all"
SAMPLE_UNIVERSE = [
    "2330", "2454", "3661",  # 半導體業
    "2308", "2492", "3026",  # 電子工業
    "2327", "4958", "6213",  # 電子零組件業
    "2409", "3008", "3406",  # 光電業
    "2317", "2360", "3665",  # 其他電子業
    "2345", "6442", "2412",  # 通信網路業
    "8996", "3167", "2049",  # 電機機械
    "2884", "2886", "2887",  # 金融保險
    "2609", "2615", "2603",  # 航運業
    "1303", "1326", "1301",  # 塑膠工業
    "3231",                  # 電腦及週邊設備業
    "6505",                  # 油電燃氣業
    "6446",                  # 生技醫療業
    "8033",                  # 其他
    "2027",                  # 鋼鐵工業
    "6861", "6919",          # 化學生技醫療
    "3029",                  # 資訊服務業
]
HISTORY_START_DATE = "2015-01-01"
