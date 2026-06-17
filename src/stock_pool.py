"""Stock futures pool screening logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

import pandas as pd


PRICE_REQUIRED_COLUMNS = ("date", "stock_id", "Trading_Volume", "max", "min", "close")
OUTPUT_COLUMNS = (
    "date",
    "stock_id",
    "stock_name",
    "futures_id",
    "close",
    "atr_20",
    "atr_20_percent",
    "avg_volume_5d",
    "best_volume_rank_5d",
    "worst_volume_rank_5d",
    "volume_rank_5d",
    "volume_top_days",
    "volume_window",
    "atr_window",
)


@dataclass(frozen=True)
class StockPoolCriteria:
    volume_days: int = 5
    volume_top_n: int = 50
    atr_days: int = 20
    min_price: float = 500.0
    max_price: float = 5000.0
    min_atr_percent: float = 3.0


def build_stock_futures_pool(
    price_data: pd.DataFrame,
    futures_contracts: pd.DataFrame,
    as_of_date: Optional[object] = None,
    criteria: StockPoolCriteria = StockPoolCriteria(),
    futures_volume_data: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Build the daily small stock futures candidate list."""
    prices = normalize_stock_prices(price_data)
    contracts = normalize_futures_contracts(futures_contracts)
    volume_source = normalize_futures_volume_data(futures_volume_data, contracts) if futures_volume_data is not None else prices
    latest_date = get_last_trading_dates(volume_source, as_of_date, 1)[0]
    volume_dates = get_last_trading_dates(volume_source, as_of_date, criteria.volume_days)
    atr_dates = get_last_trading_dates(prices, as_of_date, criteria.atr_days + 1)

    volume_candidates = find_persistent_top_volume_stocks(volume_source, volume_dates, criteria)
    atr_metrics = calculate_atr_metrics(prices, atr_dates, criteria)
    mini_contracts = get_small_stock_futures_contracts(contracts)

    if volume_candidates.empty or atr_metrics.empty or mini_contracts.empty:
        return empty_pool()

    contract_info = (
        mini_contracts.groupby("stock_id", as_index=False)
        .agg(
            stock_name=("stock_name", _first_non_empty),
            futures_id=("futures_id", _join_unique),
            contract_size=("contract_size", "first"),
        )
        .drop(columns=["contract_size"])
    )

    pool = (
        volume_candidates.merge(atr_metrics, on="stock_id", how="inner")
        .merge(contract_info, on="stock_id", how="inner")
    )
    price_in_range = (pool["close"] >= criteria.min_price) & (pool["close"] <= criteria.max_price)
    pool = pool[price_in_range & (pool["atr_20_percent"] >= criteria.min_atr_percent)].copy()

    if pool.empty:
        return empty_pool()

    pool["date"] = _format_date(latest_date)
    pool["volume_window"] = _format_date_range(volume_dates)
    pool["atr_window"] = _format_date_range(atr_dates[1:])
    pool = pool.sort_values(
        ["atr_20_percent", "avg_volume_5d", "worst_volume_rank_5d"],
        ascending=[False, False, True],
    )
    return pool.loc[:, OUTPUT_COLUMNS].reset_index(drop=True)


def normalize_stock_prices(price_data: pd.DataFrame) -> pd.DataFrame:
    """Normalize FinMind-style daily price data into the columns used by the screener."""
    aliases = {
        "Date": "date",
        "日期": "date",
        "stock_no": "stock_id",
        "證券代號": "stock_id",
        "volume": "Trading_Volume",
        "trading_volume": "Trading_Volume",
        "成交股數": "Trading_Volume",
        "high": "max",
        "最高價": "max",
        "low": "min",
        "最低價": "min",
        "Close": "close",
        "收盤價": "close",
        "name": "stock_name",
        "證券名稱": "stock_name",
    }
    df = _rename_aliases(price_data, aliases)
    missing = [column for column in PRICE_REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError("price_data is missing required columns: {}".format(", ".join(missing)))

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["stock_id"] = df["stock_id"].map(_clean_stock_id)
    for column in ("Trading_Volume", "max", "min", "close"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=list(PRICE_REQUIRED_COLUMNS))
    return df.sort_values(["date", "stock_id"]).reset_index(drop=True)


def normalize_futures_contracts(futures_contracts: pd.DataFrame) -> pd.DataFrame:
    """Normalize TAIFEX-style contract rows."""
    aliases = {
        "商品代碼": "futures_id",
        "stock_future_id": "futures_id",
        "證券代號": "stock_id",
        "underlying_stock_id": "stock_id",
        "標的證券簡稱": "stock_name",
        "name": "stock_name",
        "標準型證券股數": "contract_size",
        "標準型證券股數/受益權單位": "contract_size",
        "standard_contract_size": "contract_size",
        "unit": "contract_size",
    }
    df = _rename_aliases(futures_contracts, aliases)
    missing = [column for column in ("stock_id", "contract_size") if column not in df.columns]
    if missing:
        raise ValueError("futures_contracts is missing required columns: {}".format(", ".join(missing)))

    df = df.copy()
    if "futures_id" not in df.columns:
        df["futures_id"] = ""
    if "stock_name" not in df.columns:
        df["stock_name"] = ""
    df["stock_id"] = df["stock_id"].map(_clean_stock_id)
    df["contract_size"] = df["contract_size"].map(_parse_number)
    df = df.dropna(subset=["stock_id", "contract_size"])
    df["contract_size"] = df["contract_size"].astype(int)
    return df.reset_index(drop=True)


def normalize_futures_volume_data(volume_data: pd.DataFrame, futures_contracts: pd.DataFrame) -> pd.DataFrame:
    """Normalize small stock futures volume rows into date, stock_id, and Trading_Volume."""
    aliases = {
        "Date": "date",
        "日期": "date",
        "stock_no": "stock_id",
        "證券代號": "stock_id",
        "underlying_stock_id": "stock_id",
        "futures_code": "futures_id",
        "商品代碼": "futures_id",
        "口數": "Trading_Volume",
        "volume": "Trading_Volume",
        "total_volume": "Trading_Volume",
        "成交口數": "Trading_Volume",
    }
    df = _rename_aliases(volume_data, aliases)
    if "date" not in df.columns:
        raise ValueError("futures_volume_data is missing required column: date")
    if "Trading_Volume" not in df.columns:
        raise ValueError("futures_volume_data is missing required column: Trading_Volume")

    df = df.copy()
    if "stock_id" not in df.columns:
        if "futures_id" not in df.columns:
            raise ValueError("futures_volume_data needs stock_id or futures_id")
        contract_map = normalize_futures_contracts(futures_contracts)[["futures_id", "stock_id"]].drop_duplicates()
        df = df.merge(contract_map, on="futures_id", how="left")

    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["stock_id"] = df["stock_id"].map(_clean_stock_id)
    df["Trading_Volume"] = pd.to_numeric(df["Trading_Volume"], errors="coerce")
    df = df.dropna(subset=["date", "stock_id", "Trading_Volume"])
    df = (
        df.groupby(["date", "stock_id"], as_index=False)
        .agg(Trading_Volume=("Trading_Volume", "sum"))
        .sort_values(["date", "stock_id"])
    )
    return df.reset_index(drop=True)


def get_small_stock_futures_contracts(futures_contracts: pd.DataFrame) -> pd.DataFrame:
    """Return individual stock futures with 100 shares per contract."""
    contracts = normalize_futures_contracts(futures_contracts)
    is_stock_id = contracts["stock_id"].str.match(r"^\d{4}$", na=False)
    return contracts[(contracts["contract_size"] == 100) & is_stock_id].copy()


def get_last_trading_dates(
    prices: pd.DataFrame,
    as_of_date: Optional[object],
    count: int,
) -> List[pd.Timestamp]:
    if count <= 0:
        raise ValueError("count must be positive")
    date_limit = _normalize_optional_date(as_of_date)
    dates = sorted(pd.to_datetime(prices["date"]).dt.normalize().dropna().unique())
    dates = [pd.Timestamp(value) for value in dates]
    if date_limit is not None:
        dates = [value for value in dates if value <= date_limit]
    if len(dates) < count:
        raise ValueError("not enough trading dates: need {}, got {}".format(count, len(dates)))
    return dates[-count:]


def find_persistent_top_volume_stocks(
    prices: pd.DataFrame,
    volume_dates: Sequence[pd.Timestamp],
    criteria: StockPoolCriteria,
) -> pd.DataFrame:
    window = prices[prices["date"].isin(volume_dates)].copy()
    if window.empty:
        return pd.DataFrame(columns=["stock_id", "avg_volume_5d", "best_volume_rank_5d", "worst_volume_rank_5d", "volume_rank_5d", "volume_top_days"])

    window["volume_rank"] = window.groupby("date")["Trading_Volume"].rank(
        method="min",
        ascending=False,
    )
    top = window[window["volume_rank"] <= criteria.volume_top_n].copy()
    if top.empty:
        return pd.DataFrame(columns=["stock_id", "avg_volume_5d", "best_volume_rank_5d", "worst_volume_rank_5d", "volume_rank_5d", "volume_top_days"])

    rank_history = (
        top.sort_values(["stock_id", "date"])
        .groupby("stock_id")["volume_rank"]
        .apply(lambda values: [float(value) for value in values])
        .reset_index(name="volume_rank_5d")
    )

    result = (
        top.groupby("stock_id", as_index=False)
        .agg(
            volume_top_days=("date", "nunique"),
            avg_volume_5d=("Trading_Volume", "mean"),
            best_volume_rank_5d=("volume_rank", "min"),
            worst_volume_rank_5d=("volume_rank", "max"),
        )
        .merge(rank_history, on="stock_id", how="left")
    )
    result = result[result["volume_top_days"] == len(volume_dates)]
    return result.reset_index(drop=True)


def calculate_atr_metrics(
    prices: pd.DataFrame,
    atr_dates: Sequence[pd.Timestamp],
    criteria: StockPoolCriteria,
) -> pd.DataFrame:
    if len(atr_dates) < criteria.atr_days + 1:
        raise ValueError("ATR calculation needs atr_days + 1 trading dates")

    window = prices[prices["date"].isin(atr_dates)].copy()
    if window.empty:
        return pd.DataFrame(columns=["stock_id", "close", "atr_20", "atr_20_percent"])

    complete_day_counts = window.groupby("stock_id")["date"].nunique()
    complete_stock_ids = complete_day_counts[complete_day_counts == len(atr_dates)].index
    window = window[window["stock_id"].isin(complete_stock_ids)].copy()
    if window.empty:
        return pd.DataFrame(columns=["stock_id", "close", "atr_20", "atr_20_percent"])

    window = window.sort_values(["stock_id", "date"])
    window["prev_close"] = window.groupby("stock_id")["close"].shift(1)
    high_low = window["max"] - window["min"]
    high_prev_close = (window["max"] - window["prev_close"]).abs()
    low_prev_close = (window["min"] - window["prev_close"]).abs()
    window["true_range"] = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)

    tr_dates = list(atr_dates)[1:]
    tr_window = window[window["date"].isin(tr_dates)].copy()
    atr = (
        tr_window.groupby("stock_id", as_index=False)
        .agg(atr_20=("true_range", "mean"), atr_days=("date", "nunique"))
    )
    atr = atr[atr["atr_days"] == criteria.atr_days].drop(columns=["atr_days"])

    latest_date = list(atr_dates)[-1]
    latest = window[window["date"] == latest_date][["stock_id", "close"]].copy()
    result = atr.merge(latest, on="stock_id", how="inner")
    result["atr_20_percent"] = result["atr_20"] / result["close"] * 100.0
    return result[["stock_id", "close", "atr_20", "atr_20_percent"]].reset_index(drop=True)


def empty_pool() -> pd.DataFrame:
    return pd.DataFrame(columns=list(OUTPUT_COLUMNS))


def _rename_aliases(df: pd.DataFrame, aliases: dict) -> pd.DataFrame:
    renamed = df.copy()
    stripped_columns = {column: str(column).strip() for column in renamed.columns}
    renamed = renamed.rename(columns=stripped_columns)
    for source, target in aliases.items():
        if source in renamed.columns and target not in renamed.columns:
            renamed = renamed.rename(columns={source: target})
    return renamed


def _normalize_optional_date(value: Optional[object]) -> Optional[pd.Timestamp]:
    if value is None:
        return None
    return pd.Timestamp(value).normalize()


def _clean_stock_id(value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if text.isdigit() and len(text) < 4:
        return text.zfill(4)
    return text


def _parse_number(value: object) -> Optional[float]:
    if pd.isna(value):
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        digits = "".join(ch for ch in text if ch.isdigit())
        return float(digits) if digits else None


def _first_non_empty(values: Iterable[object]) -> str:
    for value in values:
        if pd.notna(value) and str(value).strip():
            return str(value).strip()
    return ""


def _join_unique(values: Iterable[object]) -> str:
    unique_values = sorted({str(value).strip() for value in values if pd.notna(value) and str(value).strip()})
    return ", ".join(unique_values)


def _format_date(value: pd.Timestamp) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _format_date_range(values: Sequence[pd.Timestamp]) -> str:
    if not values:
        return ""
    return "{}~{}".format(_format_date(values[0]), _format_date(values[-1]))
