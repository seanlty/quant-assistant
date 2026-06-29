# Zeabur Scheduled Refresh

此專案的瀏覽器端自動更新只能在有人開著頁面時運作。要讓 Zeabur 服務在沒人開頁面時也更新，改由 GitHub Actions 定時呼叫 server endpoint。

## 已新增的 endpoint

受保護 endpoint：

```text
GET /api/admin/refresh?mode=<mode>&min_atr_percent=3
Authorization: Bearer <DASHBOARD_REFRESH_TOKEN>
```

支援模式：

- `intraday_snapshot`：強制刷新 dashboard snapshot，交易時段內會順便 append 當前排名軌跡截面。
- `final_snapshot`：強制刷新 dashboard snapshot，用於 14:00 後盤後快取。
- `rebuild_trajectory`：使用 Fugle 5 分 K 重建當日排名軌跡並寫入 `data/cache/intraday_trajectory/`。
- `auto`：依台北時間自動選擇模式，手動觸發時可用。

公開 fallback 仍存在：

```text
GET /api/pool?refresh=1&min_atr_percent=3
```

但正式排程請使用 `/api/admin/refresh`，避免把 refresh 能力裸露給所有人。

## GitHub Actions

Workflow 已新增於：

```text
.github/workflows/dashboard-refresh.yml
```

排程使用台北時間：

- `08:48`：開盤後建立今日第一份 intraday 快取。
- `09:03, 09:18, 09:33, 09:45 ... 13:45`：盤中每 15 分鐘附近先刷新 snapshot，再多跑一次 `rebuild_trajectory&refresh_snapshot=1` 補漏。
- `14:05, 14:20, 14:35`：盤後重打 final snapshot。
- `14:50`：用 Fugle 5 分 K 再重建整天 ranking trajectory，補掉任何漏跑截面。

GitHub repository 需要設定：

- Secret：`DASHBOARD_REFRESH_TOKEN`
- Variable，可省略：`DASHBOARD_BASE_URL=https://futures-dashboard-app.zeabur.app`

Secret 設定路徑：

```text
GitHub repository -> Settings -> Secrets and variables -> Actions -> New repository secret
```

## Zeabur 環境變數

Zeabur service 需要設定同一組 token：

```env
FINMIND_API_TOKEN=...
FUGLE_API_KEY=...
DASHBOARD_REFRESH_TOKEN=<same value as GitHub secret>
USE_FUGLE_INTRADAY_QUOTE=1
USE_FUGLE_POST_CLOSE_QUOTE=1
```

建議 token 使用長隨機字串。GitHub Actions 會用：

```http
Authorization: Bearer <DASHBOARD_REFRESH_TOKEN>
```

## Zeabur Volume

目前 dashboard cache 預設寫在專案根目錄下：

```text
data/cache/
```

Zeabur 需要把 Volume 掛到服務內的這個 cache 目錄。Nixpacks/常見容器工作目錄通常是：

```text
/app/data/cache
```

若不確定實際路徑，先在 Zeabur 的 command execution 跑：

```bash
pwd
```

再掛到：

```bash
<pwd>/data/cache
```

注意：Zeabur 掛 Volume 後，掛載目錄原本內容會被清空或遮蔽。若 `data/cache/` 已有重要快取，先下載備份；掛好 Volume 後可手動跑一次：

```bash
curl -H "Authorization: Bearer $DASHBOARD_REFRESH_TOKEN" \
  "https://futures-dashboard-app.zeabur.app/api/admin/refresh?mode=rebuild_trajectory&min_atr_percent=3&refresh_snapshot=1"
```

## 手動驗證

部署後可先手動測：

```bash
curl -H "Authorization: Bearer $DASHBOARD_REFRESH_TOKEN" \
  "https://futures-dashboard-app.zeabur.app/api/admin/refresh?mode=intraday_snapshot&min_atr_percent=3"
```

預期 JSON：

```json
{
  "status": "ok",
  "mode": "intraday_snapshot",
  "as_of_date": "2026-06-22"
}
```

盤後重建：

```bash
curl -H "Authorization: Bearer $DASHBOARD_REFRESH_TOKEN" \
  "https://futures-dashboard-app.zeabur.app/api/admin/refresh?mode=rebuild_trajectory&min_atr_percent=3"
```

若成功，回傳的 `trajectory_snapshot_count` 會大於 0，`data/cache/intraday_trajectory/trajectory_asofYYYY-MM-DD.json` 會被更新。

## 參考文件

- GitHub Actions schedule：<https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule>
- GitHub Actions secrets：<https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets>
- Fugle intraday candles：<https://developer.fugle.tw/docs/data-futopt/http-api/intraday/candles/>
- Zeabur Volumes：<https://zeabur.com/docs/en-US/operations/data/volumes>
