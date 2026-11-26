# 資料更新 Endpoint 使用說明

本專案的更新分成「dashboard 最新快照」與「日內排名軌跡」。要更新今天並補回開盤後漏掉的軌跡，使用 `rebuild_trajectory&refresh_snapshot=1`；只需要最新價格與排名時，使用 `intraday_snapshot`。

正式站：<https://futures-dashboard-app.zeabur.app>。本文所有日期與時段均使用台北時間。更新會寫入呼叫對象的伺服器快取；呼叫本機 `http://127.0.0.1:8000` 只會更新本機資料。

## Endpoint 一覽

| Endpoint | 方法 | 驗證 | 行為 |
| --- | --- | --- | --- |
| `/api/admin/refresh` | GET、POST | Bearer token | 依 `mode` 更新快照或重建軌跡，回傳執行摘要 |
| `/api/pool?refresh=1` | GET | 無 | 強制更新快照並嘗試寫入當下軌跡截點，回傳完整 dashboard JSON |
| `/api/pool` | GET | 無 | 依快取有效性讀取或重建快照，也可能寫入當下截點；不是純讀取 |
| `/api/intraday-trajectory` | GET | 無 | 讀取指定日期的軌跡快取，不呼叫 Fugle 重建 |
| `/api/admin/diagnostics` | GET、POST | Bearer token | 檢查服務設定；`probe=1` 另打一次 FinMind 交易日 API |
| `/health` | GET | 無 | 確認 Web 服務存活，不代表行情資料已更新 |

管理 endpoint 的參數一律放在 URL query string。POST 的 JSON body 不會被解析。更新是同步執行，回應前會等待工作完成，沒有 job ID 或背景狀態查詢 endpoint。

## 驗證與必要設定

管理 endpoint 必須帶以下 header：

```http
Authorization: Bearer <DASHBOARD_REFRESH_TOKEN>
```

伺服器優先使用 `DASHBOARD_REFRESH_TOKEN`，未設定時才使用 `DASHBOARD_ADMIN_TOKEN`。呼叫端必須提供相同值。FinMind 與 Fugle 金鑰是伺服器取得行情用的憑證，不能代替管理 token。

伺服器需要可用的 FinMind token 與 Fugle key。日內重建使用 Fugle futopt candles；目前支援的 Fugle 環境變數優先序為 `FUGLE_API_KEY`、`FUGLE_API_TOKEN`、`FUGLE_TOKEN`。不要把任何 token 放入 URL、文件或版本控制。

## 更新模式

| `mode` | 用途 | 實際行為 |
| --- | --- | --- |
| `intraday_snapshot` | 更新最新快照，省略 `mode` 時的預設 | 強制重建 dashboard，符合即時資料條件時寫入當下 15 分鐘截點 |
| `final_snapshot` | 盤後更新 | 與前一模式走相同快照流程；是否為 `final` 由日期、時段與來源資料完整性決定 |
| `rebuild_trajectory` | 回補今天日內軌跡 | 取得近月合約 K 線，重算各截點並取代該日期整份軌跡 |
| `auto` | 依伺服器台北時間選擇 | 14:00 前用 `intraday_snapshot`；14:00 至 14:44 用 `final_snapshot`；14:45 起用 `rebuild_trajectory` |

`final_snapshot` 不會把盤中資料強制變成盤後資料，必須檢查回傳的 `snapshot_stage` 與 `final_ready`。`auto` 只按時間選模式，不能用來確認當天是否為交易日。

相容別名：`intraday`、`pool`、`snapshot` 對應 `intraday_snapshot`；`final` 對應 `final_snapshot`；`rebuild`、`trajectory_rebuild` 對應 `rebuild_trajectory`。

## Query 參數

| 參數 | 適用範圍 | 值與預設 |
| --- | --- | --- |
| `mode` | admin refresh | 上表模式；預設 `intraday_snapshot`，不是 `auto` |
| `as_of` | refresh、pool、trajectory | `YYYY-MM-DD`；更新今天建議明確指定台北日期 |
| `min_atr_percent` | refresh、pool | 只接受 `2`、`2.5`、`3`、`3.5`、`4`、`4.5`、`5`；預設 `3`，其他值回退為 `3`；別名 `atr` |
| `refresh_snapshot` | `mode=rebuild_trajectory` | `1` 先強制更新 dashboard，再使用新的 watchlist 重建；預設不強制，但過期或不存在的快照仍可能重建 |
| `refresh` | pool | 必須是單一 `refresh=1` 才會強制更新；不適用於 admin refresh |
| `probe` | diagnostics | `1` 檢查 FinMind 交易日 API；預設只回傳本機設定摘要 |

`refresh_snapshot` 與 `probe` 也接受 `true`、`yes`、`on`，不分大小寫。`refresh` 沒有這個行為。

省略或無法解析 `as_of` 時，平日 08:45 起預設今天；盤前與週末則取較早的可用快取日期，沒有時回退至前一個平日。這段日期選擇不是完整的交易所休市日曆。不要依賴錯誤日期觸發 400。

`as_of` 只指定目標資料日期；目前 Fugle candles 呼叫沒有傳入歷史日期參數，因此不能靠改 `as_of` 回補任意過去交易日。歷史軌跡應讀取已保存的快取。

## 直接更新今天

### PowerShell

以下會同步更新 dashboard，再回補今天的日內軌跡。PowerShell 不會自動讀取專案 `.env`；若目前 shell 尚未設定管理 token，程式會使用隱藏輸入取得。

```powershell
$base = 'https://futures-dashboard-app.zeabur.app'
$asOf = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
    [DateTimeOffset]::UtcNow, 'Taipei Standard Time'
).ToString('yyyy-MM-dd')

if ([string]::IsNullOrWhiteSpace($env:DASHBOARD_REFRESH_TOKEN)) {
    $secureToken = Read-Host 'DASHBOARD_REFRESH_TOKEN' -AsSecureString
    $env:DASHBOARD_REFRESH_TOKEN = [System.Net.NetworkCredential]::new('', $secureToken).Password
}
$headers = @{ Authorization = "Bearer $env:DASHBOARD_REFRESH_TOKEN" }

$result = Invoke-RestMethod -Method Post -TimeoutSec 600 `
    -Uri "$base/api/admin/refresh?mode=rebuild_trajectory&as_of=$asOf&min_atr_percent=3&refresh_snapshot=1" `
    -Headers $headers
$result | ConvertTo-Json -Depth 8

if ($result.status -ne 'ok' -or $result.as_of_date -ne $asOf -or
    [int]$result.trajectory_snapshot_count -le 0) {
    throw '更新未產生指定日期的軌跡，請檢查回應。'
}
if ([int]$result.source.fugle_candle_fetch_error_count -gt 0) {
    Write-Warning '部分合約 K 線抓取失敗，軌跡可能不完整。'
}
```

只更新最新快照時，沿用上面的 `$base`、`$asOf`、`$headers`：

```powershell
Invoke-RestMethod -Method Post -TimeoutSec 600 -Headers $headers `
    -Uri "$base/api/admin/refresh?mode=intraday_snapshot&as_of=$asOf&min_atr_percent=3"
```

盤後把 `mode` 改為 `final_snapshot`。若剛更新過快照，只要回補軌跡，可省略 `refresh_snapshot=1`。

### Bash

先在環境變數提供 `DASHBOARD_REFRESH_TOKEN`，再執行：

```bash
: "${DASHBOARD_REFRESH_TOKEN:?請先設定管理 token}"
BASE_URL="https://futures-dashboard-app.zeabur.app"
AS_OF="$(TZ=Asia/Taipei date +%F)"

curl --fail-with-body --show-error --silent --max-time 600 \
  --request POST \
  --header "Authorization: Bearer ${DASHBOARD_REFRESH_TOKEN}" \
  "${BASE_URL}/api/admin/refresh?mode=rebuild_trajectory&as_of=${AS_OF}&min_atr_percent=3&refresh_snapshot=1"
```

### 沒有管理 token 時更新快照

目前仍提供公開更新入口：

```powershell
Invoke-RestMethod -TimeoutSec 600 `
    -Uri "$base/api/pool?as_of=$asOf&min_atr_percent=3&refresh=1"
```

這只能更新快照與當下截點，不能回補已錯過的日內軌跡。公開入口也會消耗伺服器的資料 API 額度，避免連續或平行觸發。

## 使用 GitHub 既有憑證執行

若本機沒有管理 token，可以讓現有 workflow 使用 repository secret：

```powershell
gh workflow run dashboard-refresh.yml --repo seanlty/quant-assistant --ref main `
    -f mode=rebuild_trajectory -f min_atr_percent=3
gh run list --repo seanlty/quant-assistant --workflow dashboard-refresh.yml --limit 5
# 將 RUN_ID 替換成此次工作 ID
gh run view RUN_ID --repo seanlty/quant-assistant --log
```

也可以到 [Dashboard Scheduled Refresh](https://github.com/seanlty/quant-assistant/actions/workflows/dashboard-refresh.yml) 選擇 Run workflow。只接受 `mode`、`min_atr_percent` 兩個輸入；手動 `rebuild_trajectory` 不會自動加 `refresh_snapshot=1` 或指定 `as_of`。若快照也需要強制更新，可先手動執行 `intraday_snapshot`，完成後再執行 `rebuild_trajectory`。

若 workflow 為停用狀態，dispatch 會回 HTTP 422。可用下列指令查看與切換狀態：

```powershell
gh api repos/seanlty/quant-assistant/actions/workflows/dashboard-refresh.yml --jq '{name,state}'
gh workflow enable dashboard-refresh.yml --repo seanlty/quant-assistant
# 執行並確認手動工作完成；若只為本次執行而暫時啟用，再恢復停用
gh workflow disable dashboard-refresh.yml --repo seanlty/quant-assistant
```

啟用 workflow 也會恢復檔案內既有的定期觸發。排程設定在 [dashboard-refresh.yml](../.github/workflows/dashboard-refresh.yml)，週一至週五台北時間：08:48 更新快照；09 至 13 時的每小時 03、18、33、45 分更新快照並重建軌跡；14:05、14:20、14:35 更新盤後快照；14:50 重建軌跡。這些時間是觸發設定，仍須查看實際 run 是否執行成功。

## 如何確認真的更新

管理更新的 HTTP 200 與 `status=ok` 只表示流程有產出，不保證每個合約都有完整資料。

| 檢查對象 | 必看欄位 | 判讀 |
| --- | --- | --- |
| 快照 | `as_of_date`、`generated_at`、`watchlist_count` | 日期正確、時間新、檔數大於 0 |
| 快照來源 | `/api/pool` 的 `source.stale_cache_fallback` | 若為 `true`，是更新失敗後暫用舊快取；admin 摘要不含此欄位 |
| 盤後快照 | `snapshot_stage`、`final_ready` | `final_pending` 或 `false` 表示尚未完成正式盤後資料 |
| 軌跡 | `trajectory_snapshot_count`、`trajectory_last_cutoff` | 應有當天可取得的截點；也要檢查第一個截點與中間缺口 |
| K 線來源 | `source.fugle_candle_fetch_error_count`、`source.fugle_candle_fetch_errors` | 錯誤數大於 0 表示部分請求失敗；錯誤清單最多列 8 個 |
| 保存結果 | 再查 `/api/intraday-trajectory?as_of=...` | 確認伺服器能讀回重建後的時間、截點與來源 |

讀回軌跡：

```powershell
$traj = Invoke-RestMethod -TimeoutSec 30 `
    -Uri "$base/api/intraday-trajectory?as_of=$asOf"
$snapshots = @($traj.snapshots)
[PSCustomObject]@{
    as_of_date = $traj.as_of_date
    updated_at = $traj.updated_at
    cache_hit = $traj.cache_hit
    snapshot_count = $snapshots.Count
    first_cutoff = ($snapshots | Select-Object -First 1).cutoff
    last_cutoff = ($snapshots | Select-Object -Last 1).cutoff
    candle_errors = $traj.source.fugle_candle_fetch_error_count
}
$snapshots | Select-Object cutoff, status, @{Name='row_count'; Expression={ @($_.rows).Count }}
```

若還需要檢查快照，查 `/api/pool?as_of=$asOf&min_atr_percent=3`，並查看 `generated_at` 與 `source`。此呼叫本身可能更新快照，亦可能把當前截點覆寫為 `fresh`；不要把它當成完全沒有副作用的驗證。

## 軌跡與快取限制

- 預設由 Fugle 5 分 K 產生 08:45 至 13:45、每 15 分鐘一格的排名，全天最多 21 格，每格保留成交量前 50 名。這是排名截面，不是逐筆或逐分鐘資料。
- 重建的最大截點目前按「最新 K 線時間加 K 線週期」計算，未以當下時間再次限制。因此盤中可能在 08:55 就出現標記 `09:00` 的截點；這不表示已取得 09:00 的完整行情。
- `rebuild_trajectory` 取代該日期整份軌跡；部分請求失敗但仍有截點時，也可能成功寫入部分資料。快照更新則只寫入或取代當下那一格。
- 軌跡檔位於 `data/cache/intraday_trajectory/trajectory_asofYYYY-MM-DD.json`，目前保留最近 5 個日期檔案。不同 ATR 門檻共用同一日期的軌跡檔，並非各自保存。
- Dashboard 快照依日期、快取種類與篩選條件分開保存於 `data/cache/`。Zeabur 需持久化這個目錄；部署與 Volume 設定見 [排程部署說明](zeabur_scheduled_refresh.md)。
- 若伺服器設定 `FUGLE_MAX_CANDLE_SYMBOLS` 大於 0，會限制抓取的合約數；`FUGLE_CANDLE_WORKERS` 控制併發數，預設 12、最高 24；`FUGLE_TRAJECTORY_CANDLE_MINUTES` 預設 5。這些是伺服器環境設定，不是 URL 參數。

## 錯誤處理

| 回應或現象 | 處理方式 |
| --- | --- |
| 401 `missing bearer token` / `invalid bearer token` | 檢查 header 與伺服器管理 token 是否一致 |
| 503 `DASHBOARD_REFRESH_TOKEN is not configured` | 在伺服器設定管理 token，再依部署方式重啟服務 |
| 400 `unsupported refresh mode` | 使用本文列出的模式 |
| 405 `method not allowed` | 管理路由接受 GET、POST；其他方法也可能先被 HTTP server 拒絕 |
| 500 且 `status=failed` | 讀取 `error`；常見原因為 Fugle key 缺失、watchlist 空白、無法對應近月合約或沒有符合日期的 candles |
| 200 但 `stale_cache_fallback=true` | 快照更新未成功；檢查 `stale_cache_reason` 與資料時間 |
| 200 但 candle 錯誤數大於 0 | 軌跡可能有缺漏；目前摘要僅保留例外類型，需另查失敗合約的 HTTP 狀態，不能僅憑 `HTTPError` 判定原因 |
| 連線逾時或代理 502 / 504 | 先讀回 trajectory 確認是否已更新，避免重複發動整批請求 |
| FinMind 403 | 透過 diagnostics 確認實際訊息；若為封鎖或額度問題，先停止連續重試再處理來源限制 |

診斷範例：

```powershell
Invoke-RestMethod -TimeoutSec 30 -Headers $headers `
    -Uri "$base/api/admin/diagnostics?probe=1" | ConvertTo-Json -Depth 8
```

重點看 `cache_schema_version`、`finmind_token.source_env`、`finmind_trading_date_probe.http_status` 與 `msg`。完整排查見 [日內軌跡重建排查](intraday_trajectory_rebuild_runbook.md)。

## 2026 年 10 月 20 日執行紀錄

- 公開快照更新成功：`as_of_date=2026-10-20`、`generated_at=08:55:03 GMT+8`、`watchlist_count=251`、`snapshot_stage=intraday`，沒有 stale fallback 標記。
- 接著以既有 GitHub secret 呼叫 `mode=rebuild_trajectory`，回應 `status=ok`，更新時間 `08:55:32 GMT+8`，產生 2 格、最後標記 `09:00`、最後一格 50 檔。該時間標籤有前述未完成 K 線限制。
- 來源包含 200 筆 K 線，26 筆合約請求為 `HTTPError`，因此本次結果屬部分資料。正式站讀回確認有 `08:45`、`09:00` 兩格；讀回時第一格已被即時快照寫為 `fresh`。
- [本次 GitHub Actions 執行紀錄](https://github.com/seanlty/quant-assistant/actions/runs/39350494095) 的結論為 success。workflow 原先為 `disabled_inactivity`，暫時啟用執行後已恢復停用，狀態為 `disabled_manually`；後續定時更新仍需重新啟用。

## 程式依據

路由、驗證、參數解析、更新模式與快取行為位於 [src/web_dashboard.py](../src/web_dashboard.py)；Fugle HTTP 呼叫位於 [src/data_sources.py](../src/data_sources.py)。本說明核對版本為 `05282e1`。
