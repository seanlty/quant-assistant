# 每日股期股池

## 篩選條件

1. 最近 5 個交易日，每一天的股票期貨成交口數合計排名都在前 50 名內。口數以同一標的下所有股票期貨產品與月份加總，不只小型股期。
2. TAIFEX 契約單位為 100 股，也就是小型個股期貨。
3. 最新收盤價介於 500 到 5000。
4. 最近 20 個交易日 ATR% 大於等於 3%。

## ATR% 公式

```text
TR = max(
  high - low,
  abs(high - previous_close),
  abs(low - previous_close)
)

ATR_20 = mean(TR over latest 20 trading days)
ATR_20% = ATR_20 / latest_close * 100
```

計算 ATR_20 需要 21 個交易日，因為第一天只用來提供前一日收盤價。

## 輸入資料

`prices` 使用 FinMind `TaiwanStockPrice` 欄位，負責價格與 ATR：

- `date`
- `stock_id`
- `Trading_Volume`
- `max`
- `min`
- `close`

`futures_volume_data` 使用 FinMind `TaiwanFuturesDaily` 或 Fugle near-month quote 正規化後的欄位，負責成交口數篩選：

- `date`
- `stock_id`
- `Trading_Volume`

`contracts` 使用 TAIFEX 契約資料欄位：

- `futures_id`
- `stock_id`
- `stock_name`
- `contract_size`

## CLI

```powershell
python -m src.generate_pool `
  --prices data/raw/taiwan_stock_price.csv `
  --contracts data/raw/taifex_stock_futures.csv `
  --as-of 2026-06-12 `
  --output data/processed/stock_futures_pool_20260612.csv
```

若省略 `--contracts`，程式會從 TAIFEX 下載最新契約清單。
