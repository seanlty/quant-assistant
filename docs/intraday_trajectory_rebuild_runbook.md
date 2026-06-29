# 盤中排名軌跡重建排查

此文件用於排查 Zeabur 上的 dashboard 在盤中才開頁面時，排名軌跡沒有從 08:45 起完整累積的情況。

頁面本身呼叫 `/api/pool` 時只會 append 當下截點，不能回補開盤後已經錯過的截點。要回補整天日內軌跡，請呼叫受保護的 `rebuild_trajectory` admin endpoint。

## 快速重建指令

PowerShell：

```powershell
$env:DASHBOARD_REFRESH_TOKEN = "<Zeabur DASHBOARD_REFRESH_TOKEN>"
$base = "https://futures-dashboard-app.zeabur.app"
$asOf = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
  [DateTimeOffset]::UtcNow,
  "Taipei Standard Time"
).ToString("yyyy-MM-dd")

Invoke-RestMethod `
  -Uri "$base/api/admin/refresh?mode=rebuild_trajectory&as_of=$asOf&min_atr_percent=3&refresh_snapshot=1" `
  -Headers @{ Authorization = "Bearer $env:DASHBOARD_REFRESH_TOKEN" } |
  ConvertTo-Json -Depth 8
```

Bash：

```bash
export DASHBOARD_REFRESH_TOKEN="<Zeabur DASHBOARD_REFRESH_TOKEN>"
BASE_URL="https://futures-dashboard-app.zeabur.app"
AS_OF="$(TZ=Asia/Taipei date +%F)"

curl --fail-with-body --show-error --silent --location \
  --header "Authorization: Bearer ${DASHBOARD_REFRESH_TOKEN}" \
  "${BASE_URL}/api/admin/refresh?mode=rebuild_trajectory&as_of=${AS_OF}&min_atr_percent=3&refresh_snapshot=1"
```

成功時，回傳應包含：

```json
{
  "status": "ok",
  "mode": "rebuild_trajectory",
  "trajectory_snapshot_count": 1,
  "trajectory_last_cutoff": "09:30"
}
```

`trajectory_snapshot_count` 實際數字會依當天可取得的 Fugle 5 分 K 截點數增加。盤中 09:30 附近可能只有數個截點；盤後重建通常會接近完整 08:45 到 13:45。

## 先判斷目前壞在哪

### 1. 檢查 dashboard snapshot 是否更新

```powershell
$base = "https://futures-dashboard-app.zeabur.app"
$pool = Invoke-RestMethod -Uri "$base/api/pool?min_atr_percent=3"
$pool | Select-Object generated_at, as_of_date, watchlist_count
$pool.source | Select-Object snapshot_stage, final_ready, cache_kind, realtime_quote_enabled
```

判斷：

- `as_of_date` 應該是今天。
- 盤中時 `snapshot_stage` 應該是 `intraday`。
- `watchlist_count` 不應該是 0。

如果 `/api/pool` 沒更新，先處理 snapshot 更新問題，不要直接查軌跡。

### 2. 檢查排名軌跡快取

```powershell
$base = "https://futures-dashboard-app.zeabur.app"
$asOf = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
  [DateTimeOffset]::UtcNow,
  "Taipei Standard Time"
).ToString("yyyy-MM-dd")
$traj = Invoke-RestMethod -Uri "$base/api/intraday-trajectory?as_of=$asOf"
$snapshots = @($traj.snapshots)

[PSCustomObject]@{
  as_of_date = $traj.as_of_date
  snapshot_count = $snapshots.Count
  first_cutoff = if ($snapshots.Count) { $snapshots[0].cutoff } else { "" }
  last_cutoff = if ($snapshots.Count) { $snapshots[$snapshots.Count - 1].cutoff } else { "" }
  cache_hit = $traj.cache_hit
}
```

判斷：

- 只有 1 個截點，且是你開頁面當下附近時間，代表瀏覽器只 append 了當下截點，沒有回補。
- `last_cutoff` 明顯落後，例如現在 10:45 但最後還在 09:30，代表排程或外部 cron 沒持續跑。
- `snapshot_count` 是 0，代表該日期沒有可用 server-side trajectory cache。

## 應該打哪一種 mode

| 情境 | 指令 mode | 用途 |
| --- | --- | --- |
| 盤中只缺最新一格 | `intraday_snapshot` | 強制刷新 dashboard，append 當下 15 分鐘截點 |
| 盤中才開頁面，需要補 08:45 到現在 | `rebuild_trajectory` | 用 Fugle 5 分 K 重建當天軌跡 |
| 14:00 後要更新盤後 snapshot | `final_snapshot` | 建立盤後 final / final_pending snapshot |
| 盤後要補完整一天軌跡 | `rebuild_trajectory` | 用 Fugle 5 分 K 重建全日軌跡 |

盤中補當下截點：

```bash
curl --fail-with-body --show-error --silent --location \
  --header "Authorization: Bearer ${DASHBOARD_REFRESH_TOKEN}" \
  "https://futures-dashboard-app.zeabur.app/api/admin/refresh?mode=intraday_snapshot&min_atr_percent=3"
```

盤中或盤後完整重建：

```bash
curl --fail-with-body --show-error --silent --location \
  --header "Authorization: Bearer ${DASHBOARD_REFRESH_TOKEN}" \
  "https://futures-dashboard-app.zeabur.app/api/admin/refresh?mode=rebuild_trajectory&min_atr_percent=3&refresh_snapshot=1"
```

`refresh_snapshot=1` 會先強制刷新 dashboard snapshot，再用新的 watchlist 重建 trajectory。若懷疑 `/api/pool` stale，建議加上。

## 用 GitHub Actions 手動跑

如果不想在本機放 token，可以從 GitHub Actions 手動觸發：

```bash
gh workflow run dashboard-refresh.yml \
  -f mode=rebuild_trajectory \
  -f min_atr_percent=3

gh run list --workflow dashboard-refresh.yml --limit 5
```

GitHub UI 路徑：

```text
GitHub repository -> Actions -> Dashboard Scheduled Refresh -> Run workflow
```

選：

```text
mode = rebuild_trajectory
min_atr_percent = 3
```

## 常見錯誤

### 401 missing bearer token / invalid bearer token

原因：

- 沒帶 `Authorization: Bearer ...`
- 本機 token 與 Zeabur `DASHBOARD_REFRESH_TOKEN` 不一致

處理：

- 確認 Zeabur service env 有 `DASHBOARD_REFRESH_TOKEN`
- 確認 GitHub Actions secret `DASHBOARD_REFRESH_TOKEN` 與 Zeabur 相同

### 503 DASHBOARD_REFRESH_TOKEN is not configured

原因：

- Zeabur 環境變數沒有設 `DASHBOARD_REFRESH_TOKEN`

處理：

- 到 Zeabur service env 補上後 redeploy / restart。

### FUGLE_API_KEY or FUGLE_API_TOKEN is required for trajectory rebuild

原因：

- `rebuild_trajectory` 需要 Fugle 5 分 K，Zeabur 沒有 Fugle token。

處理：

- 確認 Zeabur env 有 `FUGLE_API_KEY` 或 `FUGLE_API_TOKEN`。

### dashboard watchlist is empty; cannot rebuild trajectory

原因：

- `/api/pool` 沒有可用 watchlist。

處理：

1. 先查 `/api/pool?min_atr_percent=3`。
2. 再用 `rebuild_trajectory&refresh_snapshot=1`。

### Fugle candles did not produce any intraday trajectory snapshots

可能原因：

- `as_of` 不是交易日。
- Fugle 5 分 K 尚無資料或 token 無權限。
- 近月 ticker 對應失敗。

處理：

1. 確認 `as_of` 是台灣交易日。
2. 確認 `FUGLE_API_KEY` 可取 futopt candles。
3. 檢查回傳 `source.fugle_candle_fetch_error_count` 與 `source.fugle_candle_fetch_errors`。

## 排程檢查

正式排程在：

```text
.github/workflows/dashboard-refresh.yml
```

目前設計：

- `08:48`：建立第一份 intraday snapshot。
- `09:03, 09:18, 09:33, 09:45 ... 13:45`：盤中 append trajectory。
- `14:05, 14:20, 14:35`：盤後 final snapshot。
- `14:50`：`rebuild_trajectory` 補完整日內軌跡。

若盤中經常漏跑：

1. 到 GitHub Actions 看 `Dashboard Scheduled Refresh` 是否真的有 run。
2. 確認 workflow 在 default branch。
3. 確認 GitHub Actions 沒被 repo 設定停用。
4. 加外部 cron 備援，定時呼叫：

```text
GET https://futures-dashboard-app.zeabur.app/api/admin/refresh?mode=intraday_snapshot&min_atr_percent=3
Authorization: Bearer <DASHBOARD_REFRESH_TOKEN>
```

盤後再補：

```text
GET https://futures-dashboard-app.zeabur.app/api/admin/refresh?mode=rebuild_trajectory&min_atr_percent=3&refresh_snapshot=1
Authorization: Bearer <DASHBOARD_REFRESH_TOKEN>
```

## 2026-06-29 實際觀察

當天 09:33 左右直接查 live：

```text
/api/pool?min_atr_percent=3
generated_at = 2026-06-29 09:33:20 GMT+8
as_of_date = 2026-06-29
snapshot_stage = intraday
watchlist_count = 248

/api/intraday-trajectory
as_of_date = 2026-06-29
snapshot_count = 1
last_cutoff = 09:30
cache_hit = true
```

這種狀態表示 dashboard snapshot 有更新，但 trajectory 只留下開頁面當下附近截點；要補完整日內軌跡，請打 `rebuild_trajectory`。
