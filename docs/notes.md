# 學習筆記

紀錄專案裡遇到的、還不熟的概念，簡短記錄就好，之後隨時可以補充。

---

## 路徑處理：`Path(__file__).resolve().parent.parent`

**在哪裡**：`config/settings.py` 的 `BASE_DIR`

**是什麼**：把「這支檔案自己的路徑」轉成絕對路徑，再往上跳兩層資料夾，
算出專案根目錄。

**關鍵字**：
- `__file__`：目前這支 `.py` 檔案自己的路徑
- `Path(__file__)`：包成 `Path` 物件，才能用後面的 `.resolve()`、`/` 等操作
- `.resolve()`：轉成絕對路徑，並正規化掉 `..` 之類的東西
- `.parent`：往上一層資料夾（`settings.py` 在 `config/` 裡，`.parent` 就是
  `config/`；再 `.parent` 一次就是專案根目錄）
- `BASE_DIR / "database" / "quant.db"`：`Path` 把 `/` 重載成路徑拼接，
  會自動用對的作業系統分隔符號

**為什麼要用**：相對路徑（例如直接寫 `"database/quant.db"`）是相對於「執行
當下的工作目錄」，不是相對於程式檔案自己的位置——從不同資料夾或從
notebook 執行就會找錯檔案（這正是舊版 `init_db.py` 的 bug）。錨定在
`__file__` 上就跟從哪裡執行完全無關。

---

## 步驟 11：抽象基底類別（`ABC` / `abstractmethod`）

**在哪裡**：`ingestion/sources/base.py` 的 `PriceSource`

**是什麼**：一個只定義「要有哪些方法、回傳什麼格式」，但不寫實際邏輯的類別。
繼承它的子類別（例如之後的 `FinMindSource`）才要真正實作這些方法。

**關鍵字**：
- `class PriceSource(ABC)`：繼承 `ABC` 代表這是抽象類別，**不能直接被實例化**
  （`PriceSource()` 會直接報錯）
- `@abstractmethod`：標記這個方法子類別**必須**實作，漏寫的話，子類別在
  被建立實例的那一刻就會報錯，不用等程式真的跑到那一行

**為什麼要用**：上層程式（`downloader.py`）只呼叫 `source.fetch_prices(...)`，
不用管背後是 FinMind 還是 TWSE。以後要換資料源或加備援，只要多寫一個繼承
`PriceSource` 的 class，上層完全不用改。
