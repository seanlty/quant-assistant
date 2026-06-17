# PythonAnywhere 部署

本專案已提供 WSGI 入口，可以直接部署到 PythonAnywhere Web app。

## 1. 上傳專案

在 PythonAnywhere Bash console：

```bash
cd ~
git clone <your-repo-url> StockFutures-Dashboard
cd ~/StockFutures-Dashboard/quant-assistant
```

如果不是用 git，也可以用 PythonAnywhere Files 頁面上傳整個 `quant-assistant` 目錄。

## 2. 建立 virtualenv

```bash
cd ~/StockFutures-Dashboard/quant-assistant
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

若只部署 Web dashboard，可以改裝精簡依賴：

```bash
pip install -r requirements-web.txt
```

PythonAnywhere Web tab 的 Virtualenv 路徑填：

```text
/home/<your-username>/StockFutures-Dashboard/quant-assistant/.venv
```

## 3. 設定 API key

在專案根目錄建立 `.env`：

```bash
cd ~/StockFutures-Dashboard/quant-assistant
cp .env.example .env
nano .env
```

內容範例：

```env
FINMIND_API_TOKEN=your_finmind_token
FUGLE_API_KEY=your_fugle_api_key
DASHBOARD_CACHE_SECONDS=0
USE_FUGLE_INTRADAY_QUOTE=1
USE_FUGLE_POST_CLOSE_QUOTE=1
USE_TAIFEX_STOCK_LISTS=0
```

## 4. 設定 Web app

在 PythonAnywhere Web tab：

1. 建立 Manual configuration web app。
2. Python 版本選擇和 virtualenv 相同的版本。
3. Virtualenv 填入 `.venv` 路徑。
4. 點開 WSGI configuration file。
5. 將檔案內容改成：

```python
import sys

project_home = "/home/<your-username>/StockFutures-Dashboard/quant-assistant"
if project_home not in sys.path:
    sys.path.insert(0, project_home)

from pythonanywhere_wsgi import application
```

儲存後回到 Web tab 按 Reload。

## 5. 測試

網站首頁：

```text
https://<your-username>.pythonanywhere.com/
```

健康檢查：

```text
https://<your-username>.pythonanywhere.com/health
```

JSON API：

```text
https://<your-username>.pythonanywhere.com/api/pool
```

頁面會自動讀取 shared cache；多人同時開啟時會看到同一份快照，不需要手動強制更新。

## 注意事項

- PythonAnywhere 免費方案對外連線網域可能有限制；若 API 無法連線，需要確認 FinMind 與 Fugle 是否可從該方案存取。
- 若 `DASHBOARD_CACHE_SECONDS=0`，`/api/pool` 會每次請求重建 live 資料；若要降低外部 API 壓力，可改成 30 或 900 秒。
- 每次修改程式碼或 `.env` 後，需要到 Web tab 按 Reload。
