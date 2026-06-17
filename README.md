# Quant Assistant

使用 FinMind 與 Fugle API 建構即時股票期貨監控 dashboard。

## 專案結構

```text
quant-assistant/
├─ src/
│  ├─ __init__.py
├─ data/
│  ├─ raw/
│  └─ processed/
├─ notebooks/
├─ docs/
├─ tests/
├─ .env.example
├─ .gitignore
├─ pythonanywhere_wsgi.py
├─ README.md
└─ requirements.txt
```

## 快速開始

```powershell
cd quant-assistant
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m src.web_dashboard
```

## 環境變數

請將 API key 放在 `.env`，不要提交到 git：

```env
FINMIND_API_TOKEN=your_finmind_token
FUGLE_API_KEY=your_fugle_api_key
```

`FINMIND_API_KEY` 也可作為 FinMind token 變數名稱。

可選設定：

```env
DASHBOARD_CACHE_SECONDS=0
USE_FUGLE_INTRADAY_QUOTE=1
USE_FUGLE_POST_CLOSE_QUOTE=1
USE_TAIFEX_STOCK_LISTS=0
```

這會控制交易時段 `/api/pool` 共用快取秒數。頁面會自動讀取同一份快照，避免每位使用者各自強制重打外部 API。

## 目錄用途

- `src/`：dashboard 入口與資料串接邏輯。
- `data/raw/`：原始行情資料暫存，不提交到 repo。
- `data/processed/`：清理後或特徵處理後的資料，不提交到 repo。
- `notebooks/`：研究、驗證策略與資料探索。
- `docs/`：API 串接、部署、資料欄位說明。
- `tests/`：單元測試與 smoke tests。

## 每日股期股池

目前已支援以下條件：

1. 過去 5 個交易日，每天股票期貨成交口數合計都在前 50 名內。口數使用同一標的下所有股票期貨產品與月份加總，不只小型股期。
2. 有小型個股期貨合約，也就是 TAIFEX 契約單位為 100 股。
3. 最新價格在 500 到 5000 之間。
4. 過去 20 個交易日 ATR% 大於等於 3%。

CLI 範例：

```powershell
python -m src.generate_pool `
  --prices data/raw/taiwan_stock_price.csv `
  --contracts data/raw/taifex_stock_futures.csv `
  --as-of 2026-06-12 `
  --output data/processed/stock_futures_pool_20260612.csv
```

更多說明請看 `docs/stock_pool.md`。

## Web Dashboard

每日股期股池 dashboard 使用標準 Python HTTP server 與原生 HTML/CSS：

```powershell
python -m src.web_dashboard --host 127.0.0.1 --port 8000
```

啟動後開啟 `http://127.0.0.1:8000`。首頁會先從 Fugle 取得股票期貨產品與契約清單，用 `endDate` 找出近月/次月；盤中 quote 一律取近月契約。歷史期貨口數與股票價格/ATR 再回頭從 FinMind 取得，並且只抓可能入池的股票價格資料。JSON 版本可用 `http://127.0.0.1:8000/api/pool` 取得。

## PythonAnywhere 部署

專案已提供 WSGI 入口 `pythonanywhere_wsgi.py`，可直接給 PythonAnywhere Web app 載入：

```python
import sys

project_home = "/home/<your-username>/StockFutures-Dashboard/quant-assistant"
if project_home not in sys.path:
    sys.path.insert(0, project_home)

from pythonanywhere_wsgi import application
```

完整步驟請看 `docs/pythonanywhere.md`。

若部署環境只跑 Web dashboard，可使用精簡依賴：

```bash
pip install -r requirements-web.txt
```

## 下一步建議

1. 建立每日排程，收盤後下載 FinMind 日行情並輸出股池 CSV。
2. 加入 K 線圖、股期價差與成交口數排名變化。
3. 將盤中 quote 快取落地，降低自動更新時的 API 請求量。
