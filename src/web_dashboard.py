"""Small HTTP dashboard for the daily stock futures pool."""

from __future__ import annotations

import argparse
import hmac
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
from dotenv import load_dotenv

try:
    from .data_sources import (
        FINMIND_DATA_URL,
        fetch_finmind_futopt_daily_info,
        fetch_finmind_futures_daily,
        fetch_finmind_stock_prices,
        fetch_fugle_futopt_candles,
        fetch_fugle_futopt_products,
        fetch_fugle_futopt_quote,
        fetch_fugle_futopt_tickers,
        fetch_taifex_stock_futures_contracts,
    )
    from .stock_pool import StockPoolCriteria, build_stock_futures_pool, normalize_futures_contracts, normalize_stock_prices
except ImportError:  # pragma: no cover
    from data_sources import (
        FINMIND_DATA_URL,
        fetch_finmind_futopt_daily_info,
        fetch_finmind_futures_daily,
        fetch_finmind_stock_prices,
        fetch_fugle_futopt_candles,
        fetch_fugle_futopt_products,
        fetch_fugle_futopt_quote,
        fetch_fugle_futopt_tickers,
        fetch_taifex_stock_futures_contracts,
    )
    from stock_pool import StockPoolCriteria, build_stock_futures_pool, normalize_futures_contracts, normalize_stock_prices


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_TRADING_DATE_BUFFER = 10
DEFAULT_LIVE_CACHE_SECONDS = 30
CLOSING_REFRESH_MINUTES = (13 * 60 + 30, 13 * 60 + 45, 14 * 60)
POST_CLOSE_QUOTE_START_MINUTE = 14 * 60
TAIPEI_TZ = timezone(timedelta(hours=8))
DEFAULT_CRITERIA = StockPoolCriteria()
MIN_ATR_PERCENT_OPTIONS = tuple(value / 10 for value in range(20, 51, 5))
TODAY_OVERVIEW_TOP_N = 50
DASHBOARD_CACHE_SCHEMA_VERSION = 4
CACHE_KIND_FINAL = "final"
CACHE_KIND_INTRADAY = "intraday"
INTRADAY_OPEN_MINUTES = 8 * 60 + 45
INTRADAY_CLOSE_MINUTES = 13 * 60 + 45
INTRADAY_BUCKET_MINUTES = 15
INTRADAY_TRAJECTORY_TOP_N = 50
INTRADAY_TRAJECTORY_RETENTION_DAYS = 5
CONTRACT_METADATA_CACHE_SCHEMA_VERSION = 1
DEFAULT_CONTRACT_METADATA_CACHE_SECONDS = 3 * 24 * 60 * 60
FINAL_MIN_PRODUCT_COVERAGE = 0.7
FINAL_MIN_PRODUCTS = 20
ADMIN_REFRESH_TOKEN_ENV = "DASHBOARD_REFRESH_TOKEN"
FUTURES_PRODUCT_HISTORY_COLUMNS = [
    "date",
    "stock_id",
    "stock_name",
    "futures_id",
    "finmind_futures_id",
    "fugle_product_id",
    "contract_type",
    "contract_type_label",
    "contract_size",
    "contract_date",
    "open",
    "max",
    "min",
    "close",
    "spread",
    "spread_per",
    "open_interest",
    "trading_session",
    "source",
    "has_latest_trade",
    "Trading_Volume",
]
FUTURES_POOL_COLUMNS = [
    "date",
    "stock_id",
    "stock_name",
    "futures_id",
    "finmind_futures_id",
    "contract_type",
    "contract_type_label",
    "contract_date",
    "close",
    "spread",
    "spread_per",
    "atr_20",
    "atr_20_percent",
    "avg_volume_5d",
    "best_volume_rank_5d",
    "worst_volume_rank_5d",
    "volume_rank_5d",
    "volume_top_days",
    "volume_window",
    "atr_window",
]
NEW_ENTRY_COLUMNS = [
    "date",
    "previous_date",
    "stock_id",
    "stock_name",
    "futures_id",
    "finmind_futures_id",
    "contract_type",
    "contract_type_label",
    "contract_date",
    "close",
    "current_volume",
    "previous_volume",
    "current_rank",
    "previous_rank",
]


@dataclass(frozen=True)
class DashboardSnapshot:
    generated_at: str
    as_of_date: str
    row_count: int
    active_row_count: int
    new_entry_count: int
    watchlist_count: int
    volume_window: str
    atr_window: str
    criteria: Dict[str, object]
    rows: List[Dict[str, object]]
    active_rows: List[Dict[str, object]]
    new_entry_rows: List[Dict[str, object]]
    watchlist_rows: List[Dict[str, object]]
    source: Dict[str, object]


def load_environment() -> None:
    load_dotenv(os.path.join(_project_root(), ".env"))


def get_finmind_token() -> Optional[str]:
    return os.getenv("FINMIND_API_TOKEN") or os.getenv("FINMIND_API_KEY") or os.getenv("FINMIND_TOKEN")


def get_fugle_token() -> Optional[str]:
    return os.getenv("FUGLE_API_KEY") or os.getenv("FUGLE_API_TOKEN") or os.getenv("FUGLE_TOKEN")


def get_admin_refresh_token() -> Optional[str]:
    return os.getenv(ADMIN_REFRESH_TOKEN_ENV) or os.getenv("DASHBOARD_ADMIN_TOKEN")


def taipei_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(TAIPEI_TZ)


def format_taipei_datetime(value: Optional[datetime] = None) -> str:
    current = value or taipei_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI_TZ)
    return current.astimezone(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S") + " GMT+8"


def is_taipei_trading_session(value: Optional[datetime] = None) -> bool:
    current = value or taipei_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI_TZ)
    current = current.astimezone(TAIPEI_TZ)
    if current.weekday() >= 5:
        return False
    minutes = current.hour * 60 + current.minute
    return 8 * 60 + 45 <= minutes <= 13 * 60 + 45


def is_taipei_post_close_quote_window(value: Optional[datetime] = None) -> bool:
    current = value or taipei_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI_TZ)
    current = current.astimezone(TAIPEI_TZ)
    if current.weekday() >= 5:
        return False
    minutes = current.hour * 60 + current.minute
    return minutes >= POST_CLOSE_QUOTE_START_MINUTE


def _previous_weekday(value: date) -> date:
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _latest_cached_as_of_date_before(cache_dir: Optional[str], before_day: date) -> Optional[date]:
    if not cache_dir or not os.path.isdir(cache_dir):
        return None
    dates = []
    for filename in os.listdir(cache_dir):
        if not filename.endswith(".json"):
            continue
        prefix = ""
        if filename.startswith("dashboard_asof"):
            prefix = "dashboard_asof"
        elif filename.startswith("trajectory_asof"):
            prefix = "trajectory_asof"
        if not prefix:
            continue
        date_text = filename[len(prefix):len(prefix) + 10]
        cached_day = _normalize_date(date_text)
        if cached_day is None or cached_day >= before_day:
            continue
        suffix = filename[len(prefix) + 10:]
        if suffix.startswith("_intraday_"):
            continue
        dates.append(cached_day)
    return max(dates) if dates else None


def resolve_dashboard_as_of_date(
    as_of_date: Optional[object] = None,
    now: Optional[datetime] = None,
    cache_dir: Optional[str] = None,
) -> date:
    explicit_as_of = _normalize_date(as_of_date)
    if explicit_as_of is not None:
        return explicit_as_of

    current = now or taipei_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI_TZ)
    current = current.astimezone(TAIPEI_TZ)
    today = current.date()
    minutes = current.hour * 60 + current.minute
    if current.weekday() < 5 and minutes >= INTRADAY_OPEN_MINUTES:
        return today

    return _latest_cached_as_of_date_before(cache_dir, today) or _previous_weekday(today)


def latest_closing_refresh_at(value: datetime, as_of_date: Optional[object]) -> Optional[datetime]:
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI_TZ)
    current = current.astimezone(TAIPEI_TZ)
    normalized_as_of = _normalize_date(as_of_date)
    if normalized_as_of != current.date() or current.weekday() >= 5:
        return None

    current_minutes = current.hour * 60 + current.minute
    due_minutes = [minutes for minutes in CLOSING_REFRESH_MINUTES if current_minutes >= minutes]
    if not due_minutes:
        return None
    return datetime(
        normalized_as_of.year,
        normalized_as_of.month,
        normalized_as_of.day,
        tzinfo=TAIPEI_TZ,
    ) + timedelta(minutes=due_minutes[-1])


def is_snapshot_fresh(
    loaded_at: datetime,
    now: datetime,
    ttl: int,
    historical: bool,
    trading_session: bool,
    closing_refresh_at: Optional[datetime],
) -> bool:
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI_TZ)
    now = now.astimezone(TAIPEI_TZ)
    if loaded_at.tzinfo is None:
        loaded_at = loaded_at.replace(tzinfo=TAIPEI_TZ)
    loaded_at = loaded_at.astimezone(TAIPEI_TZ)
    if closing_refresh_at is not None and loaded_at < closing_refresh_at:
        return False
    age = (now - loaded_at).total_seconds()
    return age < ttl or (not historical and not trading_session)


def is_snapshot_cache_compatible(snapshot: DashboardSnapshot) -> bool:
    for rows in (snapshot.rows, snapshot.active_rows):
        if any("spread" not in row for row in rows):
            return False
        if any("volume_rank_5d" not in row for row in rows):
            return False
    if snapshot.source.get("cache_schema_version") != DASHBOARD_CACHE_SCHEMA_VERSION:
        return False
    if snapshot.source.get("rank_sequence_fallback"):
        return False
    for field in ("snapshot_stage", "final_ready", "final_readiness_reason"):
        if field not in snapshot.source:
            return False
    return True


def migrate_cached_snapshot(snapshot: DashboardSnapshot) -> DashboardSnapshot:
    if is_snapshot_cache_compatible(snapshot):
        return snapshot
    payload = asdict(snapshot)
    spread_by_product = {}
    rank_sequence_fallback = False
    for row in payload.get("watchlist_rows", []):
        spread = row.get("spread")
        for key_name in ("finmind_futures_id", "futures_id"):
            key = _string_value(row.get(key_name)).strip()
            if key:
                spread_by_product[key] = spread

    for rows_key in ("rows", "active_rows"):
        for row in payload.get(rows_key, []):
            if "spread" not in row:
                spread = None
                for key_name in ("finmind_futures_id", "futures_id"):
                    key = _string_value(row.get(key_name)).strip()
                    if key in spread_by_product:
                        spread = spread_by_product[key]
                        break
                if spread is None:
                    spread = _derive_spread_from_percent(row.get("close"), row.get("spread_per"))
                row["spread"] = spread
            if "volume_rank_5d" not in row:
                row["volume_rank_5d"] = _fallback_rank_sequence_from_bounds(row)
                rank_sequence_fallback = True

    source = payload.get("source") or {}
    if "snapshot_stage" not in source:
        if source.get("realtime_quote_enabled"):
            source["snapshot_stage"] = "intraday"
        else:
            source["snapshot_stage"] = "final"
    if "final_ready" not in source:
        source["final_ready"] = source.get("snapshot_stage") == "final"
    if "final_readiness_reason" not in source:
        source["final_readiness_reason"] = (
            "交易時段使用 Fugle 即時層"
            if source.get("snapshot_stage") == "intraday"
            else "既有快取視為日資料快照"
        )
    source["cache_schema_version"] = DASHBOARD_CACHE_SCHEMA_VERSION
    if rank_sequence_fallback:
        source["rank_sequence_fallback"] = True
    else:
        source.pop("rank_sequence_fallback", None)
    payload["source"] = source
    return DashboardSnapshot(**payload)


def _derive_spread_from_percent(close: object, spread_per: object) -> Optional[float]:
    try:
        close_number = float(str(close).replace(",", ""))
        percent = float(str(spread_per).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None
    if pd.isna(close_number) or pd.isna(percent) or percent == -100:
        return None
    previous_close = close_number / (1 + percent / 100)
    return round(close_number - previous_close, 2)


def is_cached_snapshot_usable(
    snapshot: DashboardSnapshot,
    loaded_at: datetime,
    now: datetime,
    ttl: int,
    historical: bool,
    trading_session: bool,
    closing_refresh_at: Optional[datetime],
) -> bool:
    return is_snapshot_cache_compatible(snapshot) and is_snapshot_fresh(
        loaded_at,
        now,
        ttl,
        historical,
        trading_session,
        closing_refresh_at,
    )


def snapshot_with_source(snapshot: DashboardSnapshot, updates: Dict[str, object]) -> DashboardSnapshot:
    if all(snapshot.source.get(key) == value for key, value in updates.items()):
        return snapshot
    payload = asdict(snapshot)
    source = payload.get("source") or {}
    source.update(updates)
    payload["source"] = source
    return DashboardSnapshot(**payload)


def snapshot_final_ready(snapshot: DashboardSnapshot) -> bool:
    return bool(snapshot.source.get("final_ready"))


def final_readiness_from_daily_history(
    futures_history: pd.DataFrame,
    requested_date: Optional[object],
    stock_futures: pd.DataFrame,
) -> Tuple[bool, str, Dict[str, object]]:
    requested_day = _normalize_date(requested_date)
    metrics: Dict[str, object] = {
        "daily_final_product_rows": 0,
        "daily_final_expected_rows": 0,
        "daily_final_required_rows": 0,
    }
    if requested_day is None:
        return False, "missing requested trading date", metrics
    if futures_history.empty or "date" not in futures_history.columns:
        return False, "FinMind 今日股票期貨日資料尚未出現", metrics

    df = futures_history.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    if "finmind_futures_id" not in df.columns:
        df["finmind_futures_id"] = df.get("futures_id", pd.Series("", index=df.index))
    latest_rows = df[df["date"] == requested_day].copy()
    latest_count = int(latest_rows["finmind_futures_id"].astype(str).replace("", pd.NA).dropna().nunique())
    metrics["daily_final_product_rows"] = latest_count
    if latest_count == 0:
        return False, "FinMind 今日股票期貨日資料尚未出現", metrics

    prior = df[df["date"] < requested_day].copy()
    prior_counts = prior.groupby("date")["finmind_futures_id"].nunique() if not prior.empty else pd.Series(dtype="float64")
    expected_count = int(prior_counts.tail(5).median()) if not prior_counts.empty else 0
    if expected_count <= 0 and not stock_futures.empty and "finmind_futures_id" in stock_futures.columns:
        expected_count = int(stock_futures["finmind_futures_id"].astype(str).replace("", pd.NA).dropna().nunique())
    if expected_count <= 0:
        expected_count = latest_count

    coverage = _env_float("DASHBOARD_FINAL_MIN_PRODUCT_COVERAGE", FINAL_MIN_PRODUCT_COVERAGE)
    min_products = _env_int("DASHBOARD_FINAL_MIN_PRODUCTS", FINAL_MIN_PRODUCTS)
    required_count = max(min_products, int(round(expected_count * coverage)))
    required_count = max(1, min(expected_count, required_count))
    metrics["daily_final_expected_rows"] = expected_count
    metrics["daily_final_required_rows"] = required_count
    metrics["daily_final_min_coverage"] = coverage
    metrics["daily_final_low_coverage"] = latest_count < required_count
    if latest_count < required_count:
        return True, "FinMind 今日股票期貨日資料已取得：{} / {} 檔".format(latest_count, expected_count), metrics
    return True, "FinMind 今日股票期貨日資料完整：{} / {} 檔".format(latest_count, expected_count), metrics


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def fugle_connection_status(
    token: Optional[str],
    products: pd.DataFrame,
    tickers: pd.DataFrame,
    quotes: pd.DataFrame,
    trading_session: Optional[bool] = None,
) -> Dict[str, str]:
    if not token or os.getenv("USE_FUGLE_INTRADAY_QUOTE", "1") == "0":
        return {"status": "disabled", "text": "Fugle 未啟用"}
    if trading_session is False:
        return {"status": "idle", "text": "Fugle 非交易時段待命"}
    if quotes is not None and not quotes.empty:
        return {"status": "online", "text": "Fugle quote 正常"}
    if (products is not None and not products.empty) or (tickers is not None and not tickers.empty):
        return {"status": "warning", "text": "Fugle quote 無資料"}
    return {"status": "offline", "text": "Fugle 連線異常"}


def build_daily_pool_snapshot(
    end_date: Optional[object] = None,
    criteria: StockPoolCriteria = StockPoolCriteria(),
    timeout: int = 60,
    trading_date_buffer: int = DEFAULT_TRADING_DATE_BUFFER,
) -> DashboardSnapshot:
    """Fetch current API data and build the dashboard payload."""
    load_environment()
    token = get_finmind_token()
    if not token:
        raise RuntimeError("FINMIND_API_TOKEN or FINMIND_API_KEY is required in .env")
    fugle_token = get_fugle_token()

    now = taipei_now()
    today = now.date()
    end_day = resolve_dashboard_as_of_date(end_date, now=now)
    if end_day > today:
        end_day = today
    live_intraday = end_day == today and is_taipei_trading_session(now)
    historical_mode = end_day < today
    post_close_quote = (
        end_day == today
        and not live_intraday
        and is_taipei_post_close_quote_window(now)
        and os.getenv("USE_FUGLE_POST_CLOSE_QUOTE", "1") != "0"
    )
    use_fugle_quote_layer = live_intraday or post_close_quote

    fugle_products = pd.DataFrame()
    fugle_tickers = pd.DataFrame()
    stock_futures = _empty_stock_futures_map()
    contracts = pd.DataFrame()
    contract_source = ""
    contract_metadata_source = ""
    contract_metadata_cache_hit = False
    cached_contract_metadata_age_seconds = None
    if use_fugle_quote_layer:
        fugle_products = fetch_fugle_stock_futures_products(fugle_token, timeout=timeout)
        fugle_tickers = fetch_fugle_stock_futures_tickers(fugle_token, timeout=timeout)
        stock_futures = build_stock_futures_contract_map_from_fugle(fugle_products)
        contracts = build_contracts_from_stock_futures(stock_futures)
        if not stock_futures.empty:
            contract_source = "Fugle intraday products"
            contract_metadata_source = contract_source
            contract_metadata_cache.store(
                source=contract_source,
                stock_futures=stock_futures,
                contracts=contracts,
                fugle_products=fugle_products,
            )
    if stock_futures.empty:
        cached_metadata = contract_metadata_cache.read(allow_stale=True)
        if cached_metadata is not None:
            stock_futures = cached_metadata["stock_futures"]
            contracts = cached_metadata["contracts"]
            if contracts.empty:
                contracts = build_contracts_from_stock_futures(stock_futures)
            contract_metadata_source = str(cached_metadata.get("source") or "contract metadata cache")
            contract_source = "Cached {}".format(contract_metadata_source)
            contract_metadata_cache_hit = True
            cached_contract_metadata_age_seconds = cached_metadata.get("age_seconds")
    if stock_futures.empty:
        if os.getenv("USE_TAIFEX_STOCK_LISTS", "1") == "0":
            raise RuntimeError("No cached stock-futures contract metadata and USE_TAIFEX_STOCK_LISTS=0")
        contracts = fetch_taifex_stock_futures_contracts(timeout=timeout)
        product_info = fetch_finmind_futopt_daily_info(token=token, timeout=timeout)
        stock_futures = build_stock_futures_contract_map(contracts, product_info)
        if not stock_futures.empty:
            contract_source = "TAIFEX stockLists"
            contract_metadata_source = contract_source
            contract_metadata_cache.store(
                source=contract_source,
                stock_futures=stock_futures,
                contracts=contracts,
                fugle_products=pd.DataFrame(),
            )

    high_price_criteria = StockPoolCriteria(
        volume_days=criteria.volume_days,
        volume_top_n=criteria.volume_top_n,
        atr_days=criteria.atr_days,
        min_price=500.0,
        max_price=5000.0,
        min_atr_percent=criteria.min_atr_percent,
    )
    active_criteria = StockPoolCriteria(
        volume_days=criteria.volume_days,
        volume_top_n=criteria.volume_top_n,
        atr_days=criteria.atr_days,
        min_price=0.0,
        max_price=200.0,
        min_atr_percent=criteria.min_atr_percent,
    )

    required_days = max(criteria.volume_days, criteria.atr_days + 1)
    candidate_dates = fetch_recent_finmind_trading_dates(
        token=token,
        end_day=end_day,
        count=required_days + trading_date_buffer,
        timeout=timeout,
    )
    futures_daily, used_futures_dates = fetch_recent_finmind_futures_daily_history(
        token=token,
        trading_dates=candidate_dates[-(required_days + trading_date_buffer):],
        required_days=required_days,
        timeout=timeout,
    )
    futures_history = build_futures_product_history(futures_daily, stock_futures)
    near_month_tickers = pd.DataFrame()
    fugle_quotes = pd.DataFrame()
    usable_fugle_quotes = pd.DataFrame()
    fugle_quote_history = pd.DataFrame()
    if use_fugle_quote_layer:
        fugle_tickers, near_month_tickers = add_fugle_contract_months(fugle_tickers, stock_futures)
        fugle_quotes = fetch_fugle_near_month_quotes(
            token=fugle_token,
            near_month_tickers=near_month_tickers,
            stock_futures=stock_futures,
            timeout=timeout,
        )
        usable_fugle_quotes = fugle_quotes
        if post_close_quote and not usable_fugle_quotes.empty and "date" in usable_fugle_quotes.columns:
            quote_dates = pd.to_datetime(usable_fugle_quotes["date"], errors="coerce").dt.date
            usable_fugle_quotes = usable_fugle_quotes[quote_dates == end_day].copy()
        fugle_quote_history = build_fugle_quote_volume_history(usable_fugle_quotes, stock_futures)
        if post_close_quote and not fugle_quote_history.empty:
            quote_dates = pd.to_datetime(fugle_quote_history["date"], errors="coerce").dt.date
            fugle_quote_history = fugle_quote_history[quote_dates == end_day].copy()
        if not fugle_quote_history.empty:
            futures_history = merge_realtime_volume_history(futures_history, fugle_quote_history)
    latest_quotes = build_stock_futures_latest_quotes(futures_history, stock_futures, usable_fugle_quotes)
    latest_quotes = enrich_latest_quotes_with_daily_prices(latest_quotes, futures_daily, stock_futures)

    high_price_pool = build_futures_strategy_pool(
        futures_history,
        product_kind="small",
        criteria=high_price_criteria,
    )
    active_pool = build_futures_strategy_pool(
        futures_history,
        product_kind="regular",
        criteria=active_criteria,
    )
    new_entry_pool = build_new_entry_pool(futures_history, criteria)

    price_as_of_date = futures_history["date"].max()
    as_of_date = latest_quotes["date"].max() if not latest_quotes.empty else price_as_of_date
    rows = pool_to_records(high_price_pool)
    active_rows = pool_to_records(active_pool)
    new_entry_rows = new_entry_to_records(new_entry_pool)
    watchlist_rows = watchlist_to_records(latest_quotes)

    first_row = rows[0] if rows else {}
    first_active_row = active_rows[0] if active_rows else {}
    volume_window = str(first_row.get("volume_window", "")) or _date_window(futures_history, criteria.volume_days)
    atr_window = str(first_row.get("atr_window", "")) or str(first_active_row.get("atr_window", "")) or _date_window(futures_history, criteria.atr_days)
    fugle_status = fugle_connection_status(
        token=fugle_token,
        products=fugle_products,
        tickers=fugle_tickers,
        quotes=usable_fugle_quotes,
        trading_session=use_fugle_quote_layer,
    )
    effective_as_of_date = pd.Timestamp(as_of_date).strftime("%Y-%m-%d")
    post_close_quote_ready = post_close_quote and not fugle_quote_history.empty
    if live_intraday and not fugle_quote_history.empty:
        futures_volume_source = "FinMind TaiwanFuturesDaily + Fugle near-month intraday quote"
    elif post_close_quote_ready:
        futures_volume_source = "FinMind TaiwanFuturesDaily + Fugle near-month close quote"
    else:
        futures_volume_source = "FinMind TaiwanFuturesDaily"
    final_ready = not live_intraday
    final_reason = "歷史日資料快照" if historical_mode else "FinMind 今日股票期貨日資料完整"
    final_metrics: Dict[str, object] = {}
    if live_intraday:
        final_ready = False
        final_reason = "交易時段使用 Fugle 即時層，尚未寫入盤後正式快照"
    elif end_day == today and post_close_quote_ready:
        fugle_product_rows = int(fugle_quote_history["finmind_futures_id"].astype(str).replace("", pd.NA).dropna().nunique())
        expected_rows = int(stock_futures["finmind_futures_id"].astype(str).replace("", pd.NA).dropna().nunique()) if "finmind_futures_id" in stock_futures.columns else fugle_product_rows
        final_ready = True
        final_reason = "Fugle 近月 quote 收盤快取已取得：{} / {} 檔".format(fugle_product_rows, expected_rows)
        final_metrics = {
            "fugle_final_product_rows": fugle_product_rows,
            "fugle_final_expected_rows": expected_rows,
        }
    elif end_day == today:
        final_ready, final_reason, final_metrics = final_readiness_from_daily_history(futures_history, end_day, stock_futures)
    snapshot_stage = "intraday" if live_intraday else ("final" if final_ready else "final_pending")
    return DashboardSnapshot(
        generated_at=format_taipei_datetime(),
        as_of_date=effective_as_of_date,
        row_count=len(rows),
        active_row_count=len(active_rows),
        new_entry_count=len(new_entry_rows),
        watchlist_count=len(watchlist_rows),
        volume_window=volume_window,
        atr_window=atr_window,
        criteria=criteria_to_dict(criteria),
        rows=rows,
        active_rows=active_rows,
        new_entry_rows=new_entry_rows,
        watchlist_rows=watchlist_rows,
        source={
            "requested_as_of_date": end_day.isoformat(),
            "effective_as_of_date": effective_as_of_date,
            "historical_mode": historical_mode,
            "realtime_quote_enabled": live_intraday,
            "snapshot_stage": snapshot_stage,
            "final_ready": final_ready,
            "final_readiness_reason": final_reason,
            "futures_trading_dates": [value.isoformat() for value in used_futures_dates],
            "price_rows": int(len(futures_daily)),
            "price_as_of_date": pd.Timestamp(price_as_of_date).strftime("%Y-%m-%d"),
            "futures_rows": int(len(futures_daily)),
            "stock_futures_products": int(len(stock_futures)),
            "fugle_product_rows": int(len(fugle_products)),
            "fugle_ticker_rows": int(len(fugle_tickers)),
            "fugle_near_month_rows": int(len(near_month_tickers)),
            "fugle_quote_rows": int(len(fugle_quotes)),
            "fugle_usable_quote_rows": int(len(usable_fugle_quotes)),
            "fugle_quote_history_rows": int(len(fugle_quote_history)),
            "fugle_quote_mode": "intraday" if live_intraday else ("post_close_final" if post_close_quote else ""),
            "fugle_post_close_quote_enabled": post_close_quote,
            "fugle_post_close_quote_ready": post_close_quote_ready,
            "fugle_connection_status": fugle_status["status"],
            "fugle_connection_text": fugle_status["text"],
            "new_entry_rows": int(len(new_entry_rows)),
            "futures_quote_rows": int(len(latest_quotes)),
            "futures_volume_source": futures_volume_source,
            "cache_schema_version": DASHBOARD_CACHE_SCHEMA_VERSION,
            "contract_rows": int(len(contracts)),
            "finmind_dataset": "TaiwanFuturesDaily",
            "contract_source": contract_source or "Unknown",
            "contract_metadata_source": contract_metadata_source or contract_source or "Unknown",
            "contract_metadata_cache_hit": contract_metadata_cache_hit,
            "contract_metadata_age_seconds": cached_contract_metadata_age_seconds,
            "near_month_end_date": _first_text(near_month_tickers.get("endDate", [])) if not near_month_tickers.empty else "",
            "next_month_end_date": _first_text(fugle_tickers.loc[fugle_tickers.get("month_bucket", "") == "next", "endDate"]) if "month_bucket" in fugle_tickers.columns else "",
            **final_metrics,
        },
    )


def fetch_recent_finmind_trading_dates(
    token: str,
    end_day: date,
    count: int,
    timeout: int = 60,
) -> List[date]:
    headers = {"Authorization": "Bearer {}".format(token)}
    response = requests.get(
        FINMIND_DATA_URL,
        params={"dataset": "TaiwanStockTradingDate"},
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != 200:
        raise RuntimeError("FinMind trading-date error: {}".format(payload.get("msg", payload)))

    data = pd.DataFrame(payload.get("data", []))
    if data.empty or "date" not in data.columns:
        raise RuntimeError("FinMind returned no trading dates")

    trading_dates = pd.to_datetime(data["date"]).dt.date
    trading_dates = sorted(value for value in trading_dates if value <= end_day)
    if len(trading_dates) < count:
        raise RuntimeError("not enough FinMind trading dates: need {}, got {}".format(count, len(trading_dates)))
    return trading_dates[-count:]


def fetch_recent_finmind_price_history(
    token: str,
    trading_dates: List[date],
    required_days: int,
    timeout: int = 60,
) -> Tuple[pd.DataFrame, List[date]]:
    frames = []
    used_dates = []
    for trading_day in reversed(trading_dates):
        day_prices = fetch_finmind_stock_prices(
            start_date=trading_day.isoformat(),
            token=token,
            timeout=timeout,
        )
        if day_prices.empty:
            continue

        frames.append(day_prices)
        used_dates.append(trading_day)
        if len(frames) == required_days:
            break

    if len(frames) < required_days:
        raise RuntimeError(
            "FinMind returned prices for only {} of {} required trading dates".format(
                len(frames),
                required_days,
            )
        )

    return pd.concat(list(reversed(frames)), ignore_index=True), list(reversed(used_dates))


def fetch_recent_finmind_price_history_for_stock_ids(
    token: str,
    trading_dates: List[date],
    stock_ids: Sequence[str],
    required_days: int,
    timeout: int = 60,
) -> Tuple[pd.DataFrame, List[date]]:
    query_dates = trading_dates
    if not query_dates:
        raise RuntimeError("no FinMind trading dates for price history")

    start_date = query_dates[0].isoformat()
    end_date = query_dates[-1].isoformat()
    ids = sorted({str(stock_id).strip() for stock_id in stock_ids if str(stock_id).strip()})
    if not ids:
        raise RuntimeError("no stock ids available for FinMind price history")

    frames = []
    max_workers = max(1, int(os.getenv("FINMIND_PRICE_WORKERS", "8") or "8"))
    max_workers = min(max_workers, 16, len(ids))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                fetch_finmind_stock_prices,
                start_date=start_date,
                end_date=end_date,
                token=token,
                data_id=stock_id,
                timeout=timeout,
            ): stock_id
            for stock_id in ids
        }
        for future in as_completed(future_map):
            try:
                prices = future.result()
            except Exception:
                continue
            if not prices.empty:
                frames.append(prices)

    if not frames:
        raise RuntimeError("FinMind returned no price rows for selected stock futures universe")

    prices = pd.concat(frames, ignore_index=True)
    if "date" not in prices.columns:
        raise RuntimeError("FinMind selected price rows are missing date")

    available_dates = sorted(pd.to_datetime(prices["date"]).dt.date.dropna().unique())
    available_dates = [value for value in available_dates if value <= query_dates[-1]]
    if len(available_dates) < required_days:
        raise RuntimeError("FinMind selected prices have only {} of {} required dates".format(len(available_dates), required_days))

    used_dates = available_dates[-required_days:]
    prices = prices[pd.to_datetime(prices["date"]).dt.date.isin(used_dates)].copy()
    return prices.reset_index(drop=True), used_dates


def fetch_recent_finmind_futures_daily_history(
    token: str,
    trading_dates: List[date],
    required_days: int,
    timeout: int = 60,
) -> Tuple[pd.DataFrame, List[date]]:
    frames = []
    used_dates = []
    for trading_day in reversed(trading_dates):
        day_rows = fetch_finmind_futures_daily(
            start_date=trading_day.isoformat(),
            end_date=trading_day.isoformat(),
            token=token,
            timeout=timeout,
        )
        if day_rows.empty:
            continue

        frames.append(day_rows)
        used_dates.append(trading_day)
        if len(frames) == required_days:
            break

    if len(frames) < required_days:
        raise RuntimeError(
            "FinMind returned futures daily rows for only {} of {} required trading dates".format(
                len(frames),
                required_days,
            )
        )

    return pd.concat(list(reversed(frames)), ignore_index=True), list(reversed(used_dates))


def fetch_fugle_stock_futures_products(token: Optional[str], timeout: int = 60) -> pd.DataFrame:
    if not token or os.getenv("USE_FUGLE_INTRADAY_QUOTE", "1") == "0":
        return pd.DataFrame()
    try:
        return fetch_fugle_futopt_products(api_key=token, timeout=timeout)
    except Exception:
        return pd.DataFrame()


def fetch_fugle_stock_futures_tickers(token: Optional[str], timeout: int = 60) -> pd.DataFrame:
    if not token or os.getenv("USE_FUGLE_INTRADAY_QUOTE", "1") == "0":
        return pd.DataFrame()
    try:
        return fetch_fugle_futopt_tickers(api_key=token, timeout=timeout)
    except Exception:
        return pd.DataFrame()


def fetch_fugle_near_month_quotes(
    token: Optional[str],
    near_month_tickers: pd.DataFrame,
    stock_futures: pd.DataFrame,
    timeout: int = 60,
) -> pd.DataFrame:
    if (
        not token
        or near_month_tickers.empty
        or stock_futures.empty
        or os.getenv("USE_FUGLE_INTRADAY_QUOTE", "1") == "0"
    ):
        return pd.DataFrame()

    tickers = near_month_tickers.dropna(subset=["symbol"]).copy()
    tickers = tickers.sort_values(["fugle_product_id", "endDate", "symbol"], na_position="last")
    symbols = tickers["symbol"].dropna().astype(str).drop_duplicates().tolist()
    max_symbols = int(os.getenv("FUGLE_MAX_QUOTE_SYMBOLS", "0") or "0")
    if max_symbols > 0:
        symbols = symbols[:max_symbols]

    product_by_symbol = tickers.drop_duplicates(subset=["symbol"]).set_index("symbol")["fugle_product_id"].to_dict()
    quote_rows = []
    max_workers = max(1, int(os.getenv("FUGLE_QUOTE_WORKERS", "12") or "12"))
    max_workers = min(max_workers, 24, max(1, len(symbols)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(fetch_fugle_futopt_quote, symbol=symbol, api_key=token, timeout=timeout): symbol
            for symbol in symbols
        }
        for future in as_completed(future_map):
            symbol = future_map[future]
            try:
                quote = future.result()
            except Exception:
                continue
            if quote:
                quote["_ticker_symbol"] = symbol
                quote["fugle_product_id"] = product_by_symbol.get(symbol)
                quote_rows.append(quote)

    if not quote_rows:
        return pd.DataFrame()
    return pd.DataFrame(quote_rows)


def fetch_fugle_near_month_candles(
    token: Optional[str],
    near_month_tickers: pd.DataFrame,
    stock_futures: pd.DataFrame,
    timeframe: str = "5",
    session: str = "REGULAR",
    timeout: int = 60,
) -> pd.DataFrame:
    if not token or near_month_tickers.empty or stock_futures.empty:
        return pd.DataFrame()

    tickers = near_month_tickers.dropna(subset=["symbol"]).copy()
    tickers = tickers.sort_values(["fugle_product_id", "endDate", "symbol"], na_position="last")
    symbols = tickers["symbol"].dropna().astype(str).drop_duplicates().tolist()
    max_symbols = _env_int("FUGLE_MAX_CANDLE_SYMBOLS", 0)
    if max_symbols > 0:
        symbols = symbols[:max_symbols]
    if not symbols:
        return pd.DataFrame()

    product_by_symbol = tickers.drop_duplicates(subset=["symbol"]).set_index("symbol")["fugle_product_id"].to_dict()
    candle_rows = []
    max_workers = max(1, _env_int("FUGLE_CANDLE_WORKERS", 12))
    max_workers = min(max_workers, 24, max(1, len(symbols)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                fetch_fugle_futopt_candles,
                symbol=symbol,
                api_key=token,
                timeframe=timeframe,
                session=session,
                timeout=timeout,
            ): symbol
            for symbol in symbols
        }
        for future in as_completed(future_map):
            symbol = future_map[future]
            try:
                payload = future.result()
            except Exception:
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
            for row in rows:
                if not isinstance(row, dict):
                    continue
                record = dict(row)
                record["_ticker_symbol"] = symbol
                record["fugle_product_id"] = product_by_symbol.get(symbol)
                candle_rows.append(record)

    if not candle_rows:
        return pd.DataFrame()
    return pd.DataFrame(candle_rows)


def build_stock_futures_contract_map_from_fugle(fugle_products: pd.DataFrame) -> pd.DataFrame:
    if fugle_products is None or fugle_products.empty:
        return _empty_stock_futures_map()
    result = pd.DataFrame(_records_from_fugle_products(fugle_products))
    if result.empty:
        return _empty_stock_futures_map()
    result["stock_id"] = result["stock_id"].astype(str).str.strip()
    result["contract_size"] = pd.to_numeric(result["contract_size"], errors="coerce")
    result = result.sort_values(["stock_id", "finmind_futures_id", "name_source_rank"], ascending=[True, True, True])
    result = result.drop_duplicates(subset=["stock_id", "finmind_futures_id"])
    return result.drop(columns=["name_source_rank"], errors="ignore").reset_index(drop=True)


def build_contracts_from_stock_futures(stock_futures: pd.DataFrame) -> pd.DataFrame:
    if stock_futures.empty:
        return pd.DataFrame(columns=["futures_id", "stock_id", "stock_name", "contract_size", "contract_type", "contract_type_label"])
    columns = [column for column in ["futures_id", "stock_id", "stock_name", "contract_size", "contract_type", "contract_type_label"] if column in stock_futures.columns]
    return (
        stock_futures.loc[:, columns]
        .drop_duplicates()
        .reset_index(drop=True)
    )


def add_fugle_contract_months(
    tickers: pd.DataFrame,
    stock_futures: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if tickers is None or tickers.empty or "symbol" not in tickers.columns:
        return pd.DataFrame(), pd.DataFrame()

    result = tickers.copy()
    product_codes = _product_codes(stock_futures)
    result["fugle_product_id"] = result["symbol"].map(lambda value: _match_product_code(value, product_codes))
    result = result.dropna(subset=["fugle_product_id"]).copy()
    if result.empty or "endDate" not in result.columns:
        return result, pd.DataFrame()

    result["endDate"] = pd.to_datetime(result["endDate"], errors="coerce").dt.date
    month_dates = sorted(value for value in result["endDate"].dropna().unique())
    near_date = month_dates[0] if month_dates else None
    next_date = month_dates[1] if len(month_dates) > 1 else None
    result["month_bucket"] = result["endDate"].map(
        lambda value: "near" if value == near_date else ("next" if value == next_date else "other")
    )
    near = result[result["month_bucket"] == "near"].copy()
    return result.reset_index(drop=True), near.reset_index(drop=True)


def build_stock_futures_contract_map(
    contracts: pd.DataFrame,
    product_info: pd.DataFrame,
    fugle_products: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Map stock futures products to their underlying stock ids."""
    records = []
    if fugle_products is not None and not fugle_products.empty:
        records.extend(_records_from_fugle_products(fugle_products))

    normalized_contracts = normalize_futures_contracts(contracts)
    stock_contracts = normalized_contracts[
        normalized_contracts["stock_id"].str.match(r"^\d{4}$", na=False)
        & normalized_contracts["futures_id"].astype(str).str.strip().ne("")
    ].copy()
    if not stock_contracts.empty:
        stock_contracts["finmind_futures_id"] = stock_contracts["futures_id"].astype(str).str.strip() + "F"
        stock_contracts["fugle_product_id"] = stock_contracts["finmind_futures_id"]
        for _, row in stock_contracts.iterrows():
            contract_size = _numeric_value(row.get("contract_size"))
            contract_type, contract_type_label = _contract_type_from_size(contract_size)
            records.append(
                {
                    "stock_id": row["stock_id"],
                    "stock_name": row.get("stock_name", ""),
                    "futures_id": row.get("futures_id", ""),
                    "finmind_futures_id": row["finmind_futures_id"],
                    "fugle_product_id": row["fugle_product_id"],
                    "contract_size": contract_size,
                    "contract_type": contract_type,
                    "contract_type_label": contract_type_label,
                    "name_source_rank": 1,
                }
            )

    result = pd.DataFrame(records)
    if result.empty:
        return _empty_stock_futures_map()

    product_codes = set()
    if not product_info.empty and "code" in product_info.columns and "type" in product_info.columns:
        futures_products = product_info[product_info["type"].astype(str) == "TaiwanFuturesDaily"].copy()
        product_codes = set(futures_products["code"].astype(str))
    if product_codes:
        result = result[result["finmind_futures_id"].isin(product_codes)].copy()
    if result.empty:
        return _empty_stock_futures_map()

    result["stock_id"] = result["stock_id"].astype(str).str.strip()
    result["contract_size"] = pd.to_numeric(result["contract_size"], errors="coerce")
    if "contract_type" not in result.columns or "contract_type_label" not in result.columns:
        type_values = result["contract_size"].map(_contract_type_from_size)
        result["contract_type"] = type_values.map(lambda value: value[0])
        result["contract_type_label"] = type_values.map(lambda value: value[1])
    if "name_source_rank" not in result.columns:
        result["name_source_rank"] = 0
    result = result.sort_values(["stock_id", "finmind_futures_id", "name_source_rank"], ascending=[True, True, True])
    result = result.drop_duplicates(subset=["stock_id", "finmind_futures_id"])
    return result.drop(columns=["name_source_rank"], errors="ignore").reset_index(drop=True)


def build_small_futures_contract_map(
    contracts: pd.DataFrame,
    product_info: pd.DataFrame,
) -> pd.DataFrame:
    stock_futures = build_stock_futures_contract_map(contracts, product_info)
    return stock_futures[stock_futures["contract_size"] == 100].reset_index(drop=True)


def build_stock_futures_volume_history(
    futures_daily: pd.DataFrame,
    stock_futures: pd.DataFrame,
) -> pd.DataFrame:
    """Aggregate FinMind TaiwanFuturesDaily rows into all stock-futures volume."""
    if futures_daily.empty or stock_futures.empty:
        return _empty_volume_history()

    df = futures_daily.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["finmind_futures_id"] = df["futures_id"].astype(str).str.strip()
    df = df.merge(stock_futures, on="finmind_futures_id", how="inner", suffixes=("_daily", ""))
    if df.empty:
        return _empty_volume_history()

    df = _exclude_spread_contracts(df)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    history = (
        df.groupby(["date", "stock_id"], as_index=False)
        .agg(
            stock_name=("stock_name", _first_text),
            futures_id=("futures_id", _join_unique_text),
            finmind_futures_id=("finmind_futures_id", _join_unique_text),
            Trading_Volume=("volume", "sum"),
        )
        .sort_values(["date", "Trading_Volume", "stock_id"], ascending=[True, False, True])
    )
    return history.reset_index(drop=True)


def build_futures_product_history(
    futures_daily: pd.DataFrame,
    stock_futures: pd.DataFrame,
) -> pd.DataFrame:
    """Build one main-contract daily K row per stock-futures product."""
    if futures_daily.empty or stock_futures.empty:
        return _empty_futures_product_history()
    if "date" not in futures_daily.columns or "futures_id" not in futures_daily.columns:
        return _empty_futures_product_history()

    df = futures_daily.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df["finmind_futures_id"] = df["futures_id"].astype(str).str.strip()
    df = df.merge(stock_futures, on="finmind_futures_id", how="inner", suffixes=("_daily", ""))
    if df.empty:
        return _empty_futures_product_history()

    df = _exclude_spread_contracts(df)
    if df.empty:
        return _empty_futures_product_history()

    for column in ("open", "max", "min", "close", "spread", "spread_per", "open_interest", "volume"):
        if column not in df.columns:
            df[column] = pd.NA
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["volume"] = df["volume"].fillna(0)
    df["contract_date"] = df.get("contract_date", pd.Series("", index=df.index)).map(_clean_contract_date)
    df["fugle_product_id"] = df.get("fugle_product_id", df["finmind_futures_id"]).fillna(df["finmind_futures_id"])
    if "contract_type" not in df.columns or "contract_type_label" not in df.columns:
        type_values = df.get("contract_size", pd.Series(pd.NA, index=df.index)).map(_contract_type_from_size)
        df["contract_type"] = type_values.map(lambda value: value[0])
        df["contract_type_label"] = type_values.map(lambda value: value[1])

    df["_session_rank"] = df.get("trading_session", pd.Series("", index=df.index)).map(_session_rank)
    df = df.sort_values(
        ["date", "finmind_futures_id", "contract_date", "_session_rank"],
        ascending=[True, True, True, True],
    )
    group_columns = [
        "date",
        "stock_id",
        "stock_name",
        "futures_id",
        "finmind_futures_id",
        "fugle_product_id",
        "contract_type",
        "contract_type_label",
        "contract_size",
        "contract_date",
    ]
    contract_rows = (
        df.groupby(group_columns, dropna=False, as_index=False)
        .agg(
            open=("open", _first_valid_value),
            max=("max", "max"),
            min=("min", "min"),
            close=("close", _last_valid_value),
            spread=("spread", _last_valid_value),
            spread_per=("spread_per", _last_valid_value),
            open_interest=("open_interest", _last_valid_value),
            trading_session=("trading_session", _last_valid_value),
            Trading_Volume=("volume", "sum"),
        )
        .reset_index(drop=True)
    )
    if contract_rows.empty:
        return _empty_futures_product_history()

    contract_rows["_volume_sort"] = pd.to_numeric(contract_rows["Trading_Volume"], errors="coerce").fillna(0)
    contract_rows = contract_rows.sort_values(
        ["date", "finmind_futures_id", "_volume_sort", "contract_date"],
        ascending=[True, True, False, True],
    )
    main_rows = contract_rows.drop_duplicates(subset=["date", "finmind_futures_id"], keep="first").copy()
    main_rows["source"] = "TaiwanFuturesDaily"
    main_rows["has_latest_trade"] = True
    for column in ("open", "max", "min"):
        main_rows[column] = main_rows[column].combine_first(main_rows["close"])
    return main_rows.loc[:, FUTURES_PRODUCT_HISTORY_COLUMNS].reset_index(drop=True)


def build_futures_strategy_pool(
    futures_history: pd.DataFrame,
    product_kind: str,
    criteria: StockPoolCriteria,
) -> pd.DataFrame:
    """Screen a strategy pool using futures product volume and futures product ATR."""
    if futures_history.empty:
        return _empty_futures_pool()

    df = futures_history.copy()
    required_columns = {"date", "finmind_futures_id", "Trading_Volume", "close", "max", "min"}
    if not required_columns.issubset(df.columns):
        return _empty_futures_pool()

    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    for column in ("Trading_Volume", "open", "max", "min", "close", "spread", "spread_per"):
        if column not in df.columns:
            df[column] = pd.NA
        df[column] = pd.to_numeric(df[column], errors="coerce")
    for column in ("open", "max", "min"):
        df[column] = df[column].combine_first(df["close"])
    if "contract_type" not in df.columns or "contract_type_label" not in df.columns:
        type_values = df.get("contract_size", pd.Series(pd.NA, index=df.index)).map(_contract_type_from_size)
        df["contract_type"] = type_values.map(lambda value: value[0])
        df["contract_type_label"] = type_values.map(lambda value: value[1])

    df = df.dropna(subset=["date", "finmind_futures_id", "Trading_Volume", "close", "max", "min"])
    if df.empty:
        return _empty_futures_pool()

    dates = [pd.Timestamp(value) for value in sorted(df["date"].dropna().unique())]
    required_days = max(criteria.volume_days, criteria.atr_days + 1)
    if len(dates) < required_days:
        return _empty_futures_pool()

    latest_date = dates[-1]
    volume_dates = dates[-criteria.volume_days:]
    atr_dates = dates[-(criteria.atr_days + 1):]
    product_key = "finmind_futures_id"

    volume_window = df[df["date"].isin(volume_dates)].copy()
    stock_volume_window = (
        volume_window.groupby(["date", "stock_id"], as_index=False)
        .agg(Trading_Volume=("Trading_Volume", "sum"))
        .sort_values(["date", "Trading_Volume", "stock_id"], ascending=[True, False, True])
    )
    stock_volume_window["volume_rank"] = stock_volume_window.groupby("date")["Trading_Volume"].rank(
        method="min",
        ascending=False,
    )
    top = stock_volume_window[stock_volume_window["volume_rank"] <= criteria.volume_top_n].copy()
    if top.empty:
        return _empty_futures_pool()
    rank_history = (
        top.sort_values(["stock_id", "date"])
        .groupby("stock_id")["volume_rank"]
        .apply(lambda values: [float(value) for value in values])
        .reset_index(name="volume_rank_5d")
    )
    volume_metrics = (
        top.groupby("stock_id", as_index=False)
        .agg(
            volume_top_days=("date", "nunique"),
            avg_volume_5d=("Trading_Volume", "mean"),
            best_volume_rank_5d=("volume_rank", "min"),
            worst_volume_rank_5d=("volume_rank", "max"),
        )
        .merge(rank_history, on="stock_id", how="left")
    )
    volume_metrics = volume_metrics[volume_metrics["volume_top_days"] == len(volume_dates)]
    if volume_metrics.empty:
        return _empty_futures_pool()

    typed = df[df["contract_type"].astype(str) == product_kind].copy()
    if typed.empty:
        return _empty_futures_pool()

    atr_window = typed[typed["date"].isin(atr_dates)].copy()
    complete_counts = atr_window.groupby(product_key)["date"].nunique()
    complete_products = complete_counts[complete_counts == len(atr_dates)].index
    atr_window = atr_window[atr_window[product_key].isin(complete_products)].copy()
    if atr_window.empty:
        return _empty_futures_pool()

    atr_window = atr_window.sort_values([product_key, "date"])
    atr_window["previous_close"] = atr_window.groupby(product_key)["close"].shift(1)
    high_low = atr_window["max"] - atr_window["min"]
    high_prev_close = (atr_window["max"] - atr_window["previous_close"]).abs()
    low_prev_close = (atr_window["min"] - atr_window["previous_close"]).abs()
    atr_window["true_range"] = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
    atr = (
        atr_window[atr_window["date"].isin(atr_dates[1:])]
        .groupby(product_key, as_index=False)
        .agg(atr_20=("true_range", "mean"), atr_days=("date", "nunique"))
    )
    atr = atr[atr["atr_days"] == criteria.atr_days].drop(columns=["atr_days"])
    if atr.empty:
        return _empty_futures_pool()

    latest = typed[typed["date"] == latest_date].copy()
    latest = latest.sort_values(["stock_id", "Trading_Volume", "contract_date"], ascending=[True, False, True])
    latest = latest.drop_duplicates(subset=["stock_id"], keep="first")
    latest_columns = [
        "date",
        "stock_id",
        "stock_name",
        "futures_id",
        "finmind_futures_id",
        "contract_type",
        "contract_type_label",
        "contract_date",
        "close",
        "spread",
        "spread_per",
    ]
    pool = (
        latest.loc[:, latest_columns]
        .merge(volume_metrics, on="stock_id", how="inner")
        .merge(atr, on=product_key, how="inner")
    )
    pool["atr_20_percent"] = pool["atr_20"] / pool["close"] * 100.0
    pool = pool[
        (pool["close"] >= criteria.min_price)
        & (pool["close"] <= criteria.max_price)
        & (pool["atr_20_percent"] >= criteria.min_atr_percent)
    ].copy()
    if pool.empty:
        return _empty_futures_pool()

    pool["date"] = latest_date.strftime("%Y-%m-%d")
    pool["volume_window"] = _format_date_range(volume_dates)
    pool["atr_window"] = _format_date_range(atr_dates[1:])
    pool = pool.sort_values(
        ["atr_20_percent", "avg_volume_5d", "worst_volume_rank_5d"],
        ascending=[False, False, True],
    )
    return pool.loc[:, FUTURES_POOL_COLUMNS].reset_index(drop=True)


def build_new_entry_pool(
    futures_history: pd.DataFrame,
    criteria: StockPoolCriteria,
) -> pd.DataFrame:
    """Find futures products entering the volume Top N from outside Top N."""
    if futures_history.empty:
        return _empty_new_entry_pool()

    required_columns = {"date", "finmind_futures_id", "Trading_Volume"}
    if not required_columns.issubset(futures_history.columns):
        return _empty_new_entry_pool()

    df = futures_history.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df["Trading_Volume"] = pd.to_numeric(df["Trading_Volume"], errors="coerce").fillna(0)
    df = df.dropna(subset=["date", "finmind_futures_id"])
    if df.empty:
        return _empty_new_entry_pool()

    dates = [pd.Timestamp(value) for value in sorted(df["date"].dropna().unique())]
    if len(dates) < 2:
        return _empty_new_entry_pool()

    latest_date = dates[-1]
    previous_date = dates[-2]
    latest = _rank_futures_products_by_volume(df[df["date"] == latest_date].copy())
    previous = _rank_futures_products_by_volume(df[df["date"] == previous_date].copy())
    if latest.empty:
        return _empty_new_entry_pool()

    latest_top = latest[latest["current_rank"] <= criteria.volume_top_n].copy()
    if latest_top.empty:
        return _empty_new_entry_pool()

    previous = previous.rename(
        columns={
            "current_rank": "previous_rank",
            "current_volume": "previous_volume",
        }
    )
    previous = previous.loc[:, ["finmind_futures_id", "previous_rank", "previous_volume"]]
    entrants = latest_top.merge(previous, on="finmind_futures_id", how="left")
    entrants = entrants[
        entrants["previous_rank"].isna() | (entrants["previous_rank"] > criteria.volume_top_n)
    ].copy()
    if entrants.empty:
        return _empty_new_entry_pool()

    entrants["date"] = latest_date.strftime("%Y-%m-%d")
    entrants["previous_date"] = previous_date.strftime("%Y-%m-%d")
    entrants = entrants.sort_values(["current_rank", "finmind_futures_id"], ascending=[True, True])
    return entrants.loc[:, NEW_ENTRY_COLUMNS].reset_index(drop=True)


def _rank_futures_products_by_volume(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame()
    ranked = rows.copy()
    if "contract_type" not in ranked.columns or "contract_type_label" not in ranked.columns:
        type_values = ranked.get("contract_size", pd.Series(pd.NA, index=ranked.index)).map(_contract_type_from_size)
        ranked["contract_type"] = type_values.map(lambda value: value[0])
        ranked["contract_type_label"] = type_values.map(lambda value: value[1])
    ranked = ranked.sort_values(["finmind_futures_id", "Trading_Volume"], ascending=[True, False])
    ranked = ranked.drop_duplicates(subset=["finmind_futures_id"], keep="first")
    ranked["current_volume"] = pd.to_numeric(ranked["Trading_Volume"], errors="coerce").fillna(0)
    ranked["current_rank"] = ranked["current_volume"].rank(method="min", ascending=False)
    for column in ("stock_id", "stock_name", "futures_id", "contract_date", "close"):
        if column not in ranked.columns:
            ranked[column] = pd.NA
    return ranked.reset_index(drop=True)


def build_small_futures_volume_history(
    futures_daily: pd.DataFrame,
    small_futures: pd.DataFrame,
) -> pd.DataFrame:
    return build_stock_futures_volume_history(futures_daily, small_futures)


def build_fugle_quote_volume_history(
    quote_rows: pd.DataFrame,
    stock_futures: pd.DataFrame,
) -> pd.DataFrame:
    quotes = _normalize_fugle_quote_rows(quote_rows, stock_futures)
    if quotes.empty:
        return _empty_futures_product_history()

    front = quotes.sort_values(
        ["date", "finmind_futures_id", "Trading_Volume", "contract_date"],
        ascending=[True, True, False, True],
    ).drop_duplicates(subset=["date", "finmind_futures_id"], keep="first")
    volume = (
        quotes.groupby(["date", "finmind_futures_id"], as_index=False)
        .agg(
            Trading_Volume=("Trading_Volume", "sum"),
        )
    )
    detail_columns = [column for column in FUTURES_PRODUCT_HISTORY_COLUMNS if column != "Trading_Volume"]
    history = front.loc[:, detail_columns].merge(volume, on=["date", "finmind_futures_id"], how="left")
    for column in ("open", "max", "min"):
        history[column] = history[column].combine_first(history["close"])
    history = history.sort_values(["date", "Trading_Volume", "finmind_futures_id"], ascending=[True, False, True])
    return history.reset_index(drop=True)


def merge_realtime_volume_history(futures_history: pd.DataFrame, realtime_history: pd.DataFrame) -> pd.DataFrame:
    if realtime_history.empty:
        return futures_history
    if futures_history.empty:
        return realtime_history

    key_columns = (
        ["date", "finmind_futures_id"]
        if "finmind_futures_id" in futures_history.columns and "finmind_futures_id" in realtime_history.columns
        else ["date", "stock_id"]
    )
    realtime_keys = set(tuple(row) for row in realtime_history[key_columns].astype(str).values.tolist())
    base = futures_history[
        ~futures_history[key_columns].astype(str).apply(lambda row: tuple(row), axis=1).isin(realtime_keys)
    ]
    return pd.concat([base, realtime_history], ignore_index=True).sort_values(
        ["date", "Trading_Volume"],
        ascending=[True, False],
    )


def candidate_stock_ids_from_futures_volume(
    futures_history: pd.DataFrame,
    as_of_date: Optional[object],
    criteria: StockPoolCriteria,
) -> List[str]:
    if futures_history.empty or "date" not in futures_history.columns or "Trading_Volume" not in futures_history.columns:
        return []

    df = futures_history.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    date_limit = pd.Timestamp(as_of_date).normalize() if as_of_date is not None else None
    dates = sorted(df["date"].dropna().unique())
    dates = [pd.Timestamp(value) for value in dates]
    if date_limit is not None:
        dates = [value for value in dates if value <= date_limit]
    if len(dates) < criteria.volume_days:
        return []

    volume_dates = dates[-criteria.volume_days:]
    window = df[df["date"].isin(volume_dates)].copy()
    window["Trading_Volume"] = pd.to_numeric(window["Trading_Volume"], errors="coerce").fillna(0)
    stock_window = (
        window.groupby(["date", "stock_id"], as_index=False)
        .agg(Trading_Volume=("Trading_Volume", "sum"))
        .sort_values(["date", "Trading_Volume", "stock_id"], ascending=[True, False, True])
    )
    stock_window["volume_rank"] = stock_window.groupby("date")["Trading_Volume"].rank(method="min", ascending=False)
    top = stock_window[stock_window["volume_rank"] <= criteria.volume_top_n].copy()
    if top.empty:
        return []
    counts = top.groupby("stock_id")["date"].nunique()
    return sorted(str(stock_id) for stock_id in counts[counts == len(volume_dates)].index)


def build_stock_futures_latest_quotes(
    futures_history: pd.DataFrame,
    stock_futures: pd.DataFrame,
    quote_rows: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Build latest stock-futures quote rows from Fugle, falling back to futures daily."""
    daily_quotes = _latest_quotes_from_futures_daily(futures_history)
    realtime = _latest_quotes_from_fugle_quotes(quote_rows, stock_futures)
    if realtime.empty:
        return daily_quotes
    if daily_quotes.empty:
        return realtime

    realtime_ids = set(realtime["stock_id"])
    combined = pd.concat(
        [realtime, daily_quotes[~daily_quotes["stock_id"].isin(realtime_ids)]],
        ignore_index=True,
    )
    return combined.sort_values(
        ["has_latest_trade", "Trading_Volume", "stock_id"],
        ascending=[False, False, True],
    ).reset_index(drop=True)


def build_small_futures_latest_quotes(
    futures_history: pd.DataFrame,
    small_futures: pd.DataFrame,
    snapshot_quotes: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    return build_stock_futures_latest_quotes(futures_history, small_futures)


def _latest_quotes_from_futures_daily(
    futures_history: pd.DataFrame,
) -> pd.DataFrame:
    if futures_history.empty:
        return pd.DataFrame()

    latest_date = futures_history["date"].max()
    rows = futures_history[futures_history["date"] == latest_date].copy()
    if rows.empty:
        return pd.DataFrame()

    rows["Trading_Volume"] = pd.to_numeric(rows["Trading_Volume"], errors="coerce").fillna(0)
    rows["_volume_sort"] = rows["Trading_Volume"]
    rows["_contract_sort"] = rows.get("contract_date", pd.Series("", index=rows.index)).astype(str)
    front = rows.sort_values(["stock_id", "_volume_sort", "_contract_sort"], ascending=[True, False, True])
    front = front.groupby("stock_id", as_index=False).first()
    agg_map = {
        "stock_name": ("stock_name", _first_text),
        "futures_id": ("futures_id", _join_unique_text),
        "finmind_futures_id": ("finmind_futures_id", _join_unique_text),
        "Trading_Volume": ("Trading_Volume", "sum"),
    }
    if "contract_type_label" in rows.columns:
        agg_map["contract_type_label"] = ("contract_type_label", _join_unique_text)
    if "contract_type" in rows.columns:
        agg_map["contract_type"] = ("contract_type", _join_unique_text)
    volume = rows.groupby(["date", "stock_id"], as_index=False).agg(**agg_map)
    detail_columns = [
        "stock_id",
        "contract_date",
        "open",
        "max",
        "min",
        "close",
        "spread",
        "spread_per",
        "open_interest",
        "trading_session",
        "source",
        "has_latest_trade",
    ]
    for column in detail_columns:
        if column not in front.columns:
            front[column] = pd.NA
    quote = volume.merge(front.loc[:, detail_columns], on="stock_id", how="left")
    quote["source"] = quote["source"].fillna("TaiwanFuturesDaily")
    quote["has_latest_trade"] = quote["has_latest_trade"].fillna(True)
    return quote.sort_values(["Trading_Volume", "stock_id"], ascending=[False, True]).reset_index(drop=True)


def enrich_latest_quotes_with_daily_prices(
    latest_quotes: pd.DataFrame,
    futures_daily: pd.DataFrame,
    stock_futures: pd.DataFrame,
) -> pd.DataFrame:
    if latest_quotes.empty or futures_daily.empty:
        return latest_quotes

    daily = futures_daily.copy()
    daily["date"] = pd.to_datetime(daily["date"]).dt.normalize()
    daily["finmind_futures_id"] = daily["futures_id"].astype(str).str.strip()
    daily = daily.merge(stock_futures, on="finmind_futures_id", how="inner", suffixes=("_daily", ""))
    daily = _exclude_spread_contracts(daily)
    if daily.empty:
        return latest_quotes

    latest_date = latest_quotes["date"].max()
    daily = daily[daily["date"] == latest_date].copy()
    if daily.empty:
        return latest_quotes

    daily["volume"] = pd.to_numeric(daily["volume"], errors="coerce").fillna(0)
    daily["_session_rank"] = daily["trading_session"].map(lambda value: 1 if str(value) == "after_market" else 0)
    daily["_contract_sort"] = daily["contract_date"].astype(str)
    daily = daily.sort_values(
        ["stock_id", "volume", "_contract_sort", "_session_rank"],
        ascending=[True, False, True, False],
    )
    front = daily.groupby("stock_id", as_index=False).first()
    columns = [
        "stock_id",
        "contract_date",
        "open",
        "max",
        "min",
        "close",
        "spread",
        "spread_per",
        "open_interest",
        "trading_session",
    ]
    enriched = latest_quotes.merge(front.loc[:, columns], on="stock_id", how="left", suffixes=("", "_daily"))
    for column in columns[1:]:
        daily_column = column + "_daily"
        if daily_column in enriched.columns:
            if enriched[column].dtype == object:
                enriched.loc[enriched[column] == "", column] = pd.NA
            enriched[column] = enriched[column].combine_first(enriched[daily_column])
            enriched = enriched.drop(columns=[daily_column])
    return enriched


def _latest_quotes_from_fugle_quotes(
    quote_rows: Optional[pd.DataFrame],
    stock_futures: pd.DataFrame,
) -> pd.DataFrame:
    quotes = _normalize_fugle_quote_rows(quote_rows, stock_futures)
    if quotes.empty:
        return pd.DataFrame()

    latest_date = quotes["date"].max()
    quotes = quotes[quotes["date"] == latest_date].copy()
    quotes["_volume_sort"] = pd.to_numeric(quotes["Trading_Volume"], errors="coerce").fillna(0)
    quotes["_symbol_sort"] = quotes["contract_date"].astype(str)
    front = quotes.sort_values(["stock_id", "_volume_sort", "_symbol_sort"], ascending=[True, False, True])
    front = front.groupby("stock_id", as_index=False).first()
    volume = (
        quotes.groupby(["date", "stock_id"], as_index=False)
        .agg(
            stock_name=("stock_name", _first_text),
            futures_id=("futures_id", _join_unique_text),
            finmind_futures_id=("finmind_futures_id", _join_unique_text),
            contract_type=("contract_type", _join_unique_text),
            contract_type_label=("contract_type_label", _join_unique_text),
            Trading_Volume=("Trading_Volume", "sum"),
        )
    )
    detail_columns = [
        "stock_id",
        "contract_date",
        "open",
        "max",
        "min",
        "close",
        "spread",
        "spread_per",
        "open_interest",
        "trading_session",
        "source",
        "has_latest_trade",
    ]
    result = volume.merge(front.loc[:, detail_columns], on="stock_id", how="left")
    return result.sort_values(["Trading_Volume", "stock_id"], ascending=[False, True]).reset_index(drop=True)


def _normalize_fugle_quote_rows(
    quote_rows: Optional[pd.DataFrame],
    stock_futures: pd.DataFrame,
) -> pd.DataFrame:
    if quote_rows is None or quote_rows.empty or stock_futures.empty:
        return pd.DataFrame()

    quotes = quote_rows.copy()
    if "symbol" not in quotes.columns and "_ticker_symbol" in quotes.columns:
        quotes["symbol"] = quotes["_ticker_symbol"]
    if "symbol" not in quotes.columns or "date" not in quotes.columns:
        return pd.DataFrame()

    product_codes = _product_codes(stock_futures)
    if "fugle_product_id" not in quotes.columns:
        quotes["fugle_product_id"] = quotes["symbol"].map(lambda value: _match_product_code(value, product_codes))
    product_map = stock_futures.drop_duplicates(subset=["fugle_product_id"]).copy()
    quotes = quotes.merge(product_map, on="fugle_product_id", how="inner", suffixes=("_quote", ""))
    if quotes.empty:
        return pd.DataFrame()

    quotes["date"] = pd.to_datetime(quotes["date"]).dt.normalize()
    quotes["Trading_Volume"] = quotes["total"].map(lambda value: _nested_number(value, "tradeVolume"))
    quotes["Trading_Volume"] = pd.to_numeric(quotes["Trading_Volume"], errors="coerce").fillna(0)
    quotes["contract_date"] = quotes["symbol"].astype(str)
    quotes["open"] = _numeric_column(quotes, "openPrice")
    quotes["max"] = _numeric_column(quotes, "highPrice")
    quotes["min"] = _numeric_column(quotes, "lowPrice")
    close_price = _numeric_column(quotes, "closePrice")
    last_price = _numeric_column(quotes, "lastPrice")
    quotes["close"] = close_price.combine_first(last_price)
    quotes["spread"] = pd.to_numeric(quotes.get("change"), errors="coerce")
    quotes["spread_per"] = pd.to_numeric(quotes.get("changePercent"), errors="coerce")
    quotes["open_interest"] = pd.NA
    quotes["trading_session"] = "regular"
    quotes["source"] = "Fugle near-month quote"
    quotes["has_latest_trade"] = True
    if "contract_size" not in quotes.columns:
        quotes["contract_size"] = pd.NA
    if "contract_type" not in quotes.columns or "contract_type_label" not in quotes.columns:
        type_values = quotes["contract_size"].map(_contract_type_from_size)
        quotes["contract_type"] = type_values.map(lambda value: value[0])
        quotes["contract_type_label"] = type_values.map(lambda value: value[1])

    columns = [
        "date",
        "stock_id",
        "stock_name",
        "futures_id",
        "finmind_futures_id",
        "fugle_product_id",
        "contract_type",
        "contract_type_label",
        "contract_size",
        "contract_date",
        "open",
        "max",
        "min",
        "close",
        "spread",
        "spread_per",
        "open_interest",
        "trading_session",
        "source",
        "has_latest_trade",
        "Trading_Volume",
    ]
    return quotes.loc[:, columns].reset_index(drop=True)


def _series_from_first_column(df: pd.DataFrame, names: Sequence[str]) -> pd.Series:
    for name in names:
        if name in df.columns:
            return df[name]
    return pd.Series(pd.NA, index=df.index)


def _parse_taipei_timestamp(value: object):
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return pd.NaT
    if pd.isna(timestamp):
        return pd.NaT
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(TAIPEI_TZ)
    return timestamp.tz_convert(TAIPEI_TZ)


def _normalize_fugle_candle_rows(
    candle_rows: Optional[pd.DataFrame],
    stock_futures: pd.DataFrame,
) -> pd.DataFrame:
    if candle_rows is None or candle_rows.empty or stock_futures.empty:
        return pd.DataFrame()

    candles = candle_rows.copy()
    if "symbol" not in candles.columns and "_ticker_symbol" in candles.columns:
        candles["symbol"] = candles["_ticker_symbol"]
    if "symbol" not in candles.columns or "date" not in candles.columns:
        return pd.DataFrame()

    product_codes = _product_codes(stock_futures)
    if "fugle_product_id" not in candles.columns:
        candles["fugle_product_id"] = candles["symbol"].map(lambda value: _match_product_code(value, product_codes))
    product_map = stock_futures.drop_duplicates(subset=["fugle_product_id"]).copy()
    candles = candles.merge(product_map, on="fugle_product_id", how="inner", suffixes=("_candle", ""))
    if candles.empty:
        return pd.DataFrame()

    candles["_timestamp"] = candles["date"].map(_parse_taipei_timestamp)
    candles = candles[candles["_timestamp"].notna()].copy()
    if candles.empty:
        return pd.DataFrame()
    candles["as_of_date"] = candles["_timestamp"].map(lambda value: value.date())
    candles["minute"] = candles["_timestamp"].map(lambda value: value.hour * 60 + value.minute)
    candles["contract_date"] = candles["symbol"].astype(str)
    candles["close"] = pd.to_numeric(_series_from_first_column(candles, ("close", "closePrice", "lastPrice")), errors="coerce")
    candles["Trading_Volume"] = pd.to_numeric(_series_from_first_column(candles, ("volume", "tradeVolume")), errors="coerce").fillna(0)
    if "contract_type" not in candles.columns or "contract_type_label" not in candles.columns:
        type_values = candles.get("contract_size", pd.Series(pd.NA, index=candles.index)).map(_contract_type_from_size)
        candles["contract_type"] = type_values.map(lambda value: value[0])
        candles["contract_type_label"] = type_values.map(lambda value: value[1])

    columns = [
        "as_of_date",
        "minute",
        "_timestamp",
        "stock_id",
        "stock_name",
        "futures_id",
        "finmind_futures_id",
        "fugle_product_id",
        "contract_type",
        "contract_type_label",
        "contract_date",
        "close",
        "Trading_Volume",
        "symbol",
    ]
    return candles.loc[:, columns].reset_index(drop=True)


def _previous_close_from_watchlist_row(row: Dict[str, object]) -> Optional[float]:
    close = _trajectory_number(row.get("close"))
    spread = _trajectory_number(row.get("spread"))
    if close is not None and spread is not None:
        previous_close = close - spread
        if previous_close > 0:
            return previous_close
    derived_spread = _derive_spread_from_percent(row.get("close"), row.get("spread_per"))
    if close is not None and derived_spread is not None:
        previous_close = close - derived_spread
        if previous_close > 0:
            return previous_close
    return None


def _intraday_cutoff_labels() -> List[str]:
    return [
        _minute_label(minutes)
        for minutes in range(INTRADAY_OPEN_MINUTES, INTRADAY_CLOSE_MINUTES + 1, INTRADAY_BUCKET_MINUTES)
    ]


def build_intraday_trajectory_history_from_candles(
    as_of_date: object,
    watchlist_rows: Sequence[Dict[str, object]],
    candle_rows: pd.DataFrame,
    candle_minutes: int = 5,
    now: Optional[datetime] = None,
    source: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    normalized_as_of = _normalize_date(as_of_date) or taipei_now().date()
    current = now or taipei_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI_TZ)
    current = current.astimezone(TAIPEI_TZ)

    stock_meta: Dict[str, Dict[str, object]] = {}
    for row in watchlist_rows or []:
        if not isinstance(row, dict):
            continue
        stock_id = str(row.get("stock_id") or "").strip()
        if not stock_id:
            continue
        stock_meta[stock_id] = {
            "stock_name": str(row.get("stock_name") or stock_id),
            "futures_id": str(row.get("finmind_futures_id") or row.get("futures_id") or ""),
            "contract_type_label": str(row.get("contract_type_label") or ""),
            "previous_close": _previous_close_from_watchlist_row(row),
        }

    if candle_rows is None or candle_rows.empty:
        normalized_candles = pd.DataFrame()
    else:
        normalized_candles = candle_rows.copy()
    if "as_of_date" not in normalized_candles.columns and "date" in normalized_candles.columns:
        normalized_candles["_timestamp"] = normalized_candles["date"].map(_parse_taipei_timestamp)
        normalized_candles["as_of_date"] = normalized_candles["_timestamp"].map(
            lambda value: value.date() if pd.notna(value) else pd.NaT
        )
    if "minute" not in normalized_candles.columns and "_timestamp" in normalized_candles.columns:
        normalized_candles["minute"] = normalized_candles["_timestamp"].map(
            lambda value: value.hour * 60 + value.minute if pd.notna(value) else pd.NA
        )
    if "Trading_Volume" not in normalized_candles.columns:
        normalized_candles["Trading_Volume"] = pd.to_numeric(
            _series_from_first_column(normalized_candles, ("volume", "tradeVolume")),
            errors="coerce",
        ).fillna(0)
    if "close" not in normalized_candles.columns:
        normalized_candles["close"] = pd.to_numeric(
            _series_from_first_column(normalized_candles, ("closePrice", "lastPrice")),
            errors="coerce",
        )

    required_columns = {"as_of_date", "minute", "stock_id", "close", "Trading_Volume"}
    if normalized_candles.empty or not required_columns.issubset(normalized_candles.columns):
        filtered = pd.DataFrame()
    else:
        filtered = normalized_candles.copy()
        filtered["as_of_date"] = filtered["as_of_date"].map(_normalize_date)
        filtered["minute"] = pd.to_numeric(filtered["minute"], errors="coerce")
        filtered["Trading_Volume"] = pd.to_numeric(filtered["Trading_Volume"], errors="coerce").fillna(0)
        filtered["close"] = pd.to_numeric(filtered["close"], errors="coerce")
        filtered["stock_id"] = filtered["stock_id"].astype(str).str.strip()
        filtered = filtered[
            (filtered["as_of_date"] == normalized_as_of)
            & (filtered["minute"] >= INTRADAY_OPEN_MINUTES)
            & (filtered["minute"] <= INTRADAY_CLOSE_MINUTES)
            & filtered["stock_id"].ne("")
            & filtered["close"].notna()
        ].copy()
        if stock_meta:
            filtered = filtered[filtered["stock_id"].isin(stock_meta.keys())].copy()

    snapshots = []
    if not filtered.empty:
        max_complete_cutoff = min(INTRADAY_CLOSE_MINUTES, int(filtered["minute"].max()) + int(candle_minutes or 5))
        for cutoff_label in _intraday_cutoff_labels():
            cutoff_minutes = _label_to_minutes(cutoff_label)
            if cutoff_minutes > max_complete_cutoff:
                continue
            if cutoff_minutes == INTRADAY_OPEN_MINUTES:
                frame = filtered[filtered["minute"] <= cutoff_minutes].copy()
            else:
                frame = filtered[filtered["minute"] < cutoff_minutes].copy()
            if frame.empty:
                continue
            total_volume = (
                frame.groupby("stock_id", as_index=False)
                .agg(volume=("Trading_Volume", "sum"))
            )
            if "symbol" in frame.columns:
                symbol_keys = ["stock_id", "symbol"]
            else:
                symbol_keys = ["stock_id", "contract_date"] if "contract_date" in frame.columns else ["stock_id"]
            frame["_symbol_volume"] = frame.groupby(symbol_keys)["Trading_Volume"].transform("sum")
            sort_columns = ["stock_id", "_symbol_volume", "minute"]
            ascending = [True, False, False]
            if "symbol" in frame.columns:
                sort_columns.append("symbol")
                ascending.append(True)
            front = frame.sort_values(sort_columns, ascending=ascending).groupby("stock_id", as_index=False).first()
            ranked = total_volume.merge(front, on="stock_id", how="inner", suffixes=("", "_front"))

            rows = []
            for _, row in ranked.iterrows():
                stock_id = str(row.get("stock_id") or "").strip()
                meta = stock_meta.get(stock_id, {})
                previous_close = meta.get("previous_close")
                if previous_close is None:
                    previous_close = _previous_close_from_watchlist_row(row.to_dict())
                close = _trajectory_number(row.get("close"))
                volume = _trajectory_number(row.get("volume"))
                if close is None or volume is None or previous_close is None or float(previous_close) <= 0:
                    continue
                spread_per = (close - float(previous_close)) / float(previous_close) * 100
                rows.append(
                    {
                        "stock_id": stock_id,
                        "stock_name": str(meta.get("stock_name") or row.get("stock_name") or stock_id),
                        "futures_id": str(meta.get("futures_id") or row.get("finmind_futures_id") or row.get("futures_id") or ""),
                        "contract_type_label": str(meta.get("contract_type_label") or row.get("contract_type_label") or ""),
                        "close": round(float(close), 2),
                        "spread_per": round(float(spread_per), 2),
                        "volume": round(float(volume), 0),
                    }
                )

            rows = sorted(rows, key=lambda item: (-float(item["volume"]), str(item["stock_id"])))
            ranked_rows = []
            for index, row in enumerate(rows[:INTRADAY_TRAJECTORY_TOP_N], start=1):
                next_row = dict(row)
                next_row["rank"] = index
                ranked_rows.append(next_row)
            if ranked_rows:
                snapshots.append(
                    {
                        "cutoff": cutoff_label,
                        "captured_at": format_taipei_datetime(current),
                        "status": "rebuilt_5m",
                        "rows": ranked_rows,
                    }
                )

    history_source = {
        "type": "Fugle intraday candles",
        "candle_minutes": int(candle_minutes or 5),
        "watchlist_rows": len(watchlist_rows or []),
        "candle_rows": int(len(candle_rows) if candle_rows is not None else 0),
        "normalized_candle_rows": int(len(filtered)) if "filtered" in locals() else 0,
    }
    if source:
        history_source.update(source)
    return {
        "version": 1,
        "as_of_date": normalized_as_of.isoformat(),
        "updated_at": format_taipei_datetime(current),
        "snapshots": snapshots,
        "cache_hit": True,
        "source": history_source,
    }


def _records_from_fugle_products(products: pd.DataFrame) -> List[Dict[str, object]]:
    records = []
    if products.empty or "symbol" not in products.columns or "underlyingSymbol" not in products.columns:
        return records

    rows = products.copy()
    if "contractType" in rows.columns:
        rows = rows[rows["contractType"].astype(str) == "S"].copy()
    if "type" in rows.columns:
        rows = rows[rows["type"].astype(str) == "FUTURE"].copy()
    if "statusCode" in rows.columns:
        rows = rows[rows["statusCode"].astype(str).isin(["N", ""])].copy()

    for _, row in rows.iterrows():
        product_id = str(row.get("symbol", "")).strip()
        stock_id = str(row.get("underlyingSymbol", "")).strip()
        if not product_id or not stock_id.isdigit() or len(stock_id) != 4:
            continue
        contract_size = _numeric_value(row.get("contractSize"))
        contract_type, contract_type_label = _contract_type_from_size(contract_size)
        records.append(
            {
                "stock_id": stock_id,
                "stock_name": _clean_futures_name(row.get("name", "")),
                "futures_id": product_id[:-1] if product_id.endswith("F") else product_id,
                "finmind_futures_id": product_id,
                "fugle_product_id": product_id,
                "contract_size": contract_size,
                "contract_type": contract_type,
                "contract_type_label": contract_type_label,
                "name_source_rank": 0,
            }
        )
    return records


def _empty_stock_futures_map() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "stock_id",
            "stock_name",
            "futures_id",
            "finmind_futures_id",
            "fugle_product_id",
            "contract_size",
            "contract_type",
            "contract_type_label",
        ]
    )


def _empty_volume_history() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["date", "stock_id", "stock_name", "futures_id", "finmind_futures_id", "Trading_Volume"]
    )


def _empty_futures_product_history() -> pd.DataFrame:
    return pd.DataFrame(columns=FUTURES_PRODUCT_HISTORY_COLUMNS)


def _empty_futures_pool() -> pd.DataFrame:
    return pd.DataFrame(columns=FUTURES_POOL_COLUMNS)


def _empty_new_entry_pool() -> pd.DataFrame:
    return pd.DataFrame(columns=NEW_ENTRY_COLUMNS)


def _product_codes(stock_futures: pd.DataFrame) -> List[str]:
    if stock_futures.empty:
        return []
    column = "fugle_product_id" if "fugle_product_id" in stock_futures.columns else "finmind_futures_id"
    values = stock_futures[column].dropna().astype(str).str.strip()
    return sorted(set(value for value in values if value), key=len, reverse=True)


def _match_product_code(symbol: object, product_codes: List[str]) -> Optional[str]:
    text = str(symbol or "").strip()
    for code in product_codes:
        if text.startswith(code):
            return code
    return None


def _nested_number(value: object, key: str) -> Optional[float]:
    if isinstance(value, dict):
        return value.get(key)
    return None


def _numeric_column(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(pd.NA, index=df.index)
    return pd.to_numeric(df[column], errors="coerce")


def _numeric_value(value: object) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _contract_type_from_size(value: object) -> Tuple[str, str]:
    size = _numeric_value(value)
    if size == 100:
        return "small", "小型"
    return "regular", "大型"


def _session_rank(value: object) -> int:
    text = str(value or "").strip()
    if text == "after_market":
        return 2
    if text == "position":
        return 1
    return 0


def _clean_contract_date(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if text.endswith(".0"):
        return text[:-2]
    return text


def _first_valid_value(values) -> object:
    for value in values:
        if pd.notna(value):
            return value
    return pd.NA


def _last_valid_value(values) -> object:
    for value in reversed(list(values)):
        if pd.notna(value):
            return value
    return pd.NA


def _clean_futures_name(value: object) -> str:
    text = str(value or "").strip()
    if text.endswith("期貨"):
        text = text[:-2]
    if text.startswith("小型"):
        text = text[2:]
    return text


def _exclude_spread_contracts(df: pd.DataFrame) -> pd.DataFrame:
    if "contract_date" not in df.columns:
        return df.copy()
    return df[~df["contract_date"].astype(str).str.contains("/", regex=False)].copy()


def _rank_sequence(value: object) -> List[float]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = []
            return _rank_sequence(parsed)
        values = [part.strip() for part in text.split(",")]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        try:
            if pd.isna(value):
                return []
        except ValueError:
            pass
        return []

    ranks = []
    for item in values:
        number = _numeric_value(item)
        if number is not None:
            ranks.append(round(number, 0))
    return ranks


def _fallback_rank_sequence_from_bounds(row: Dict[str, object]) -> List[object]:
    best_rank = _numeric_value(row.get("best_volume_rank_5d"))
    worst_rank = _numeric_value(row.get("worst_volume_rank_5d"))
    if best_rank is None and worst_rank is None:
        return ["", "", "", "", ""]
    if best_rank is None:
        best_rank = worst_rank
    if worst_rank is None:
        worst_rank = best_rank
    if best_rank is None or worst_rank is None:
        return ["", "", "", "", ""]
    if best_rank == worst_rank:
        return [round(best_rank, 0)] * 5
    step = (worst_rank - best_rank) / 4.0
    return [round(worst_rank - step * index, 0) for index in range(5)]


def _rank_pill_class(rank: float) -> str:
    if rank <= 10:
        return "rank-pill hot"
    if rank <= 25:
        return "rank-pill warm"
    return "rank-pill"


def _render_rank_pill(value: object) -> str:
    number = _numeric_value(value)
    if number is None:
        return '<span class="rank-pill empty">-</span>'
    return '<span class="{klass}">{rank}</span>'.format(
        klass=_rank_pill_class(number),
        rank=_format_optional_number(number, 0),
    )


def _render_rank_strip(row: Dict[str, object]) -> str:
    ranks = _rank_sequence(row.get("volume_rank_5d"))
    values = ranks[-5:] if len(ranks) >= 5 else _fallback_rank_sequence_from_bounds(row)
    pills = "".join(_render_rank_pill(value) for value in values)
    return '<div class="rank-strip"><div class="rank-pills">{pills}</div></div>'.format(pills=pills)


def _render_intraday_change_placeholder() -> str:
    return '<span class="rank-delta is-empty">－</span>'


def _render_rank_status_placeholder() -> str:
    return '<span class="rank-status">等待</span>'


def pool_to_records(pool: pd.DataFrame) -> List[Dict[str, object]]:
    records = []
    for _, row in pool.iterrows():
        records.append(
            {
                "date": _string_value(row.get("date")),
                "stock_id": _string_value(row.get("stock_id")),
                "stock_name": _string_value(row.get("stock_name")),
                "futures_id": _string_value(row.get("futures_id")),
                "finmind_futures_id": _string_value(row.get("finmind_futures_id")),
                "contract_type": _string_value(row.get("contract_type")),
                "contract_type_label": _string_value(row.get("contract_type_label")),
                "contract_date": _string_value(row.get("contract_date")),
                "close": _round_value(row.get("close"), 2),
                "spread": _optional_round_value(row.get("spread"), 2),
                "spread_per": _optional_round_value(row.get("spread_per"), 2),
                "atr_20": _round_value(row.get("atr_20"), 2),
                "atr_20_percent": _round_value(row.get("atr_20_percent"), 2),
                "avg_volume_5d": _round_value(row.get("avg_volume_5d"), 0),
                "best_volume_rank_5d": _round_value(row.get("best_volume_rank_5d"), 0),
                "worst_volume_rank_5d": _round_value(row.get("worst_volume_rank_5d"), 0),
                "volume_rank_5d": _rank_sequence(row.get("volume_rank_5d")),
                "volume_top_days": _round_value(row.get("volume_top_days"), 0),
                "volume_window": _string_value(row.get("volume_window")),
                "atr_window": _string_value(row.get("atr_window")),
            }
        )
    return records


def new_entry_to_records(entries: pd.DataFrame) -> List[Dict[str, object]]:
    records = []
    if entries.empty:
        return records

    for _, row in entries.iterrows():
        records.append(
            {
                "date": _date_value(row.get("date")),
                "previous_date": _date_value(row.get("previous_date")),
                "stock_id": _string_value(row.get("stock_id")),
                "stock_name": _string_value(row.get("stock_name")),
                "futures_id": _string_value(row.get("futures_id")),
                "finmind_futures_id": _string_value(row.get("finmind_futures_id")),
                "contract_type": _string_value(row.get("contract_type")),
                "contract_type_label": _string_value(row.get("contract_type_label")),
                "contract_date": _string_value(row.get("contract_date")),
                "close": _optional_round_value(row.get("close"), 2),
                "current_volume": _optional_round_value(row.get("current_volume"), 0),
                "previous_volume": _optional_round_value(row.get("previous_volume"), 0),
                "current_rank": _optional_round_value(row.get("current_rank"), 0),
                "previous_rank": _optional_round_value(row.get("previous_rank"), 0),
            }
        )
    return records


def watchlist_to_records(watchlist: pd.DataFrame) -> List[Dict[str, object]]:
    records = []
    if watchlist.empty:
        return records

    for _, row in watchlist.iterrows():
        records.append(
            {
                "date": _date_value(row.get("date")),
                "stock_id": _string_value(row.get("stock_id")),
                "stock_name": _string_value(row.get("stock_name")),
                "futures_id": _string_value(row.get("futures_id")),
                "finmind_futures_id": _string_value(row.get("finmind_futures_id")),
                "contract_type": _string_value(row.get("contract_type")),
                "contract_type_label": _string_value(row.get("contract_type_label")),
                "contract_date": _string_value(row.get("contract_date")),
                "open": _optional_round_value(row.get("open"), 2),
                "high": _optional_round_value(row.get("max"), 2),
                "low": _optional_round_value(row.get("min"), 2),
                "close": _optional_round_value(row.get("close"), 2),
                "volume": _optional_round_value(row.get("Trading_Volume"), 0),
                "open_interest": _optional_round_value(row.get("open_interest"), 0),
                "spread": _optional_round_value(row.get("spread"), 2),
                "spread_per": _optional_round_value(row.get("spread_per"), 2),
                "trading_session": _string_value(row.get("trading_session")),
                "source": _string_value(row.get("source")),
                "has_latest_trade": bool(row.get("has_latest_trade", False)),
            }
        )
    return records


def criteria_to_dict(criteria: StockPoolCriteria) -> Dict[str, object]:
    return {
        "volume_days": criteria.volume_days,
        "volume_top_n": criteria.volume_top_n,
        "atr_days": criteria.atr_days,
        "min_price": criteria.min_price,
        "max_price": criteria.max_price,
        "min_atr_percent": criteria.min_atr_percent,
    }


def criteria_from_query(query: Dict[str, List[str]]) -> StockPoolCriteria:
    min_atr_percent = _parse_min_atr_percent(_first_query_value(query, "min_atr_percent") or _first_query_value(query, "atr"))
    return StockPoolCriteria(
        volume_days=DEFAULT_CRITERIA.volume_days,
        volume_top_n=DEFAULT_CRITERIA.volume_top_n,
        atr_days=DEFAULT_CRITERIA.atr_days,
        min_price=DEFAULT_CRITERIA.min_price,
        max_price=DEFAULT_CRITERIA.max_price,
        min_atr_percent=min_atr_percent,
    )


def _parse_as_of_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return _normalize_date(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _first_query_value(query: Dict[str, List[str]], key: str) -> Optional[str]:
    values = query.get(key) or []
    return values[0] if values else None


def _parse_min_atr_percent(value: Optional[str]) -> float:
    if value is None:
        return DEFAULT_CRITERIA.min_atr_percent
    try:
        requested = float(value)
    except (TypeError, ValueError):
        return DEFAULT_CRITERIA.min_atr_percent
    for option in MIN_ATR_PERCENT_OPTIONS:
        if abs(option - requested) < 0.000001:
            return option
    return DEFAULT_CRITERIA.min_atr_percent


def _format_percent(value: float) -> str:
    return "{:g}".format(value)


def _render_min_atr_percent_options(selected_value: float) -> str:
    options = []
    for value in MIN_ATR_PERCENT_OPTIONS:
        selected = " selected" if abs(value - selected_value) < 0.000001 else ""
        label = _format_percent(value)
        options.append('<option value="{value}"{selected}>{label}%</option>'.format(value=label, selected=selected, label=label))
    return "\n".join(options)


def _date_input_value(value: object) -> str:
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return taipei_now().strftime("%Y-%m-%d")


def render_dashboard_html(snapshot: DashboardSnapshot) -> str:
    rows_html = _render_table_rows(snapshot.rows)
    active_rows_html = _render_table_rows(snapshot.active_rows)
    new_entry_html = _render_new_entry_rows(snapshot.new_entry_rows)
    watchlist_html = _render_watchlist_rows(snapshot.watchlist_rows)
    today_overview_html = _render_today_overview_chart(snapshot.watchlist_rows)
    status_text = "同步完成" if snapshot.rows or snapshot.active_rows or snapshot.new_entry_rows or snapshot.watchlist_rows else "無符合標的"
    max_atr = max([float(row["atr_20_percent"]) for row in snapshot.rows + snapshot.active_rows], default=0.0)
    today_date = taipei_now().strftime("%Y-%m-%d")
    as_of_filter_value = _date_input_value(snapshot.as_of_date)

    return """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>每日股期股池</title>
  <style>
    :root {{
      --bg: #f6f8fb;
      --panel: #ffffff;
      --line: #d9e1ea;
      --text: #18211f;
      --muted: #647276;
      --accent: #0f766e;
      --accent-soft: #d8f3ee;
      --warn: #b45309;
      --ink: #243238;
      --table-head: #eef3f7;
      --button-text: #ffffff;
      --overview-bg: #ffffff;
      --overview-grid: #d9e1ea;
      --overview-axis: #aab7c4;
      --overview-text: #243238;
      --overview-muted: #647276;
      --overview-chip-bg: #f6f8fb;
      --overview-chip-line: #d9e1ea;
      --overview-shadow: rgba(15, 23, 42, 0.08);
      --overview-wick: #7a8795;
      --overview-focus: #243238;
      --scrollbar-thumb: rgba(100, 114, 118, 0.38);
      --scrollbar-thumb-hover: rgba(15, 118, 110, 0.72);
      --scroll-fade-start: rgba(255, 255, 255, 0.96);
      --scroll-fade-end: rgba(255, 255, 255, 0);
    }}
    [data-theme="dark"] {{
      --bg: #101418;
      --panel: #171d22;
      --line: #2b363d;
      --text: #e7edf0;
      --muted: #9aa8af;
      --accent: #2dd4bf;
      --accent-soft: #123d38;
      --warn: #f59e0b;
      --ink: #f3f7f8;
      --table-head: #202a31;
      --button-text: #06201d;
      --overview-bg: #171d22;
      --overview-grid: #2b363d;
      --overview-axis: #59677c;
      --overview-text: #e7edf0;
      --overview-muted: #9aa8af;
      --overview-chip-bg: #202a31;
      --overview-chip-line: #2b363d;
      --overview-shadow: rgba(0, 0, 0, 0.14);
      --overview-wick: #8b92a0;
      --overview-focus: #ffffff;
      --scrollbar-thumb: rgba(154, 168, 175, 0.32);
      --scrollbar-thumb-hover: rgba(45, 212, 191, 0.68);
      --scroll-fade-start: rgba(23, 29, 34, 0.96);
      --scroll-fade-end: rgba(23, 29, 34, 0);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      background: var(--bg);
      font-family: "Segoe UI", "Noto Sans TC", Arial, sans-serif;
      font-size: 15px;
    }}
    .shell {{
      max-width: 1760px;
      margin: 0 auto;
      padding: 24px;
    }}
    .topbar {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0;
      font-size: 28px;
      line-height: 1.2;
      font-weight: 750;
    }}
    .subtle {{
      color: var(--muted);
      margin-top: 8px;
    }}
    .actions {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 auto;
      min-height: 36px;
      padding: 0 14px;
      border-radius: 6px;
      border: 1px solid var(--accent);
      color: var(--button-text);
      background: var(--accent);
      text-decoration: none;
      font-weight: 650;
    }}
    .button:disabled {{
      cursor: wait;
      opacity: 0.72;
    }}
    .theme-toggle {{
      width: 38px;
      min-width: 38px;
      padding: 0;
      border-color: var(--line);
      color: var(--ink);
      background: var(--panel);
      cursor: pointer;
      font-size: 18px;
      line-height: 1;
    }}
    .status {{
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--accent);
      background: var(--panel);
      font-size: 13px;
      font-weight: 650;
    }}
    .status.secondary {{
      color: var(--muted);
      border-color: var(--line);
    }}
    .connection-status {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 32px;
      min-width: 32px;
      min-height: 32px;
      padding: 0;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: var(--panel);
      font-size: 13px;
      font-weight: 650;
    }}
    .status-dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--muted);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--muted) 18%, transparent);
    }}
    .connection-status.is-online {{
      color: var(--accent);
    }}
    .connection-status.is-online .status-dot {{
      background: #16a34a;
      box-shadow: 0 0 0 3px rgba(22, 163, 74, 0.18);
    }}
    .connection-status.is-warning {{
      color: var(--warn);
    }}
    .connection-status.is-warning .status-dot {{
      background: #f59e0b;
      box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.22);
    }}
    .connection-status.is-offline .status-dot {{
      background: #dc2626;
      box-shadow: 0 0 0 3px rgba(220, 38, 38, 0.18);
    }}
    .connection-status.is-offline {{
      color: #dc2626;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
      margin: 18px 0;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-height: 86px;
    }}
    .label {{
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 8px;
    }}
    .value {{
      font-size: 23px;
      font-weight: 760;
      line-height: 1.2;
      color: var(--ink);
      word-break: break-word;
    }}
    .criteria {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 18px;
    }}
    .chip {{
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 999px;
      padding: 7px 11px;
      color: var(--ink);
      font-size: 13px;
      font-weight: 650;
    }}
    .filter-control {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 34px;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 6px;
      padding: 5px 10px;
      color: var(--ink);
      font-size: 13px;
      font-weight: 650;
    }}
    .filter-control select,
    .filter-control input {{
      min-width: 74px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--bg);
      color: var(--ink);
      padding: 4px 8px;
      font: inherit;
    }}
    .table-wrap {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: auto;
      overflow-anchor: none;
      -webkit-overflow-scrolling: touch;
      margin-bottom: 18px;
    }}
    .scroll-frame {{
      position: relative;
      margin-bottom: 18px;
    }}
    .scroll-frame > .table-wrap,
    .scroll-frame > .overview-chart-shell {{
      margin-bottom: 0;
      scrollbar-width: thin;
      scrollbar-color: var(--scrollbar-thumb) transparent;
    }}
    .scroll-frame > .table-wrap::-webkit-scrollbar,
    .scroll-frame > .overview-chart-shell::-webkit-scrollbar {{
      width: 8px;
      height: 8px;
    }}
    .scroll-frame > .table-wrap::-webkit-scrollbar-track,
    .scroll-frame > .overview-chart-shell::-webkit-scrollbar-track {{
      background: transparent;
    }}
    .scroll-frame > .table-wrap::-webkit-scrollbar-thumb,
    .scroll-frame > .overview-chart-shell::-webkit-scrollbar-thumb {{
      min-width: 36px;
      min-height: 36px;
      border: 2px solid transparent;
      border-radius: 999px;
      background-color: var(--scrollbar-thumb);
      background-clip: content-box;
    }}
    .scroll-frame > .table-wrap::-webkit-scrollbar-thumb:hover,
    .scroll-frame > .overview-chart-shell::-webkit-scrollbar-thumb:hover {{
      background-color: var(--scrollbar-thumb-hover);
    }}
    .scroll-frame > .table-wrap::-webkit-scrollbar-corner,
    .scroll-frame > .overview-chart-shell::-webkit-scrollbar-corner {{
      background: transparent;
    }}
    .scroll-frame::before,
    .scroll-frame::after {{
      content: "";
      position: absolute;
      top: 1px;
      bottom: 9px;
      width: 30px;
      pointer-events: none;
      z-index: 5;
    }}
    .scroll-frame::before {{
      left: 1px;
      border-top-left-radius: 7px;
      border-bottom-left-radius: 7px;
      background: linear-gradient(to right, var(--scroll-fade-start), var(--scroll-fade-end));
    }}
    .scroll-frame::after {{
      right: 1px;
      border-top-right-radius: 7px;
      border-bottom-right-radius: 7px;
      background: linear-gradient(to left, var(--scroll-fade-start), var(--scroll-fade-end));
    }}
    .overview-frame {{
      margin-bottom: 0;
    }}
    .today-overview {{
      margin: 20px 0 18px;
    }}
    .today-overview-head {{
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin-bottom: 10px;
    }}
    .today-overview-head h2 {{
      margin: 0;
      font-size: 22px;
      line-height: 1.25;
      font-weight: 760;
    }}
    .today-overview-head span {{
      color: var(--muted);
      font-size: 13px;
    }}
    .overview-chart-shell {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--overview-bg);
      overflow-x: auto;
      overflow-y: hidden;
      -webkit-overflow-scrolling: touch;
      box-shadow: 0 12px 28px var(--overview-shadow);
    }}
    .overview-chart-toolbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 16px 2px;
    }}
    .overview-legend,
    .overview-chips {{
      display: flex;
      align-items: center;
      gap: 12px;
      white-space: nowrap;
    }}
    .overview-legend {{
      color: var(--overview-muted);
      font-size: 13px;
    }}
    .overview-legend span::before {{
      content: "";
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 2px;
      margin-right: 6px;
      vertical-align: -1px;
    }}
    .overview-legend .up::before {{ background: #ff3438; }}
    .overview-legend .down::before {{ background: #00c92f; }}
    .overview-chips {{
      color: var(--overview-text);
      font-size: 12px;
    }}
    .overview-chips span {{
      border: 1px solid var(--overview-chip-line);
      border-radius: 999px;
      background: var(--overview-chip-bg);
      padding: 6px 10px;
    }}
    .overview-chart {{
      display: block;
    }}
    .overview-bg {{
      fill: var(--overview-bg);
    }}
    .overview-grid {{
      stroke: var(--overview-grid);
      stroke-width: 1;
      opacity: 0.82;
    }}
    .overview-axis-zero {{
      stroke: var(--overview-axis);
      stroke-width: 1.2;
      opacity: 0.9;
    }}
    .overview-y-label {{
      fill: var(--overview-text);
      font-size: 14px;
      font-weight: 800;
    }}
    .overview-y-label.pos {{ fill: #ff3438; }}
    .overview-y-label.neg {{ fill: #00c92f; }}
    .overview-x-label {{
      fill: var(--overview-text);
      font-size: 14px;
      font-weight: 800;
      letter-spacing: 0;
      dominant-baseline: hanging;
    }}
    .overview-x-label tspan {{
      dominant-baseline: hanging;
    }}
    .overview-wick {{
      stroke: var(--overview-wick);
    }}
    .overview-candle:focus {{
      outline: none;
    }}
    .overview-candle:hover rect,
    .overview-candle:focus rect {{
      stroke: var(--overview-focus);
      stroke-width: 2;
    }}
    .overview-empty {{
      min-height: 220px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--overview-muted);
      font-size: 14px;
    }}
    .intraday-grid {{
      --intraday-card-height: clamp(350px, 27.5vw, 438px);
      display: grid;
      grid-template-columns: minmax(0, 1.08fr) minmax(520px, 0.92fr);
      gap: 16px;
      align-items: stretch;
      margin: 18px 0;
    }}
    .intraday-movers-panel {{
      margin: 18px 0;
    }}
    .trajectory-panel,
    .intraday-side-panel,
    .intraday-movers-panel,
    .pool-tabs-panel,
    .new-entry-panel {{
      min-width: 0;
    }}
    .trajectory-chart-shell,
    .intraday-card,
    .butterfly-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    .intraday-side-panel {{
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 0;
      height: 100%;
    }}
    .trajectory-panel {{
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      height: 100%;
    }}
    .trajectory-chart-shell {{
      height: var(--intraday-card-height);
      min-height: 0;
    }}
    .trajectory-chart {{
      display: block;
      width: 100%;
      height: 100%;
      min-height: 0;
      background: var(--panel);
    }}
    .butterfly-card {{
      height: var(--intraday-card-height);
      min-height: 0;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }}
    .butterfly-head {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 1px minmax(0, 1fr);
      min-height: 34px;
      border-bottom: 1px solid var(--line);
    }}
    .butterfly-title {{
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 7px 10px;
      font-size: 14px;
      font-weight: 800;
      line-height: 1.2;
    }}
    .butterfly-title.up {{
      color: #dc2626;
      background: rgba(220, 38, 38, 0.08);
    }}
    .butterfly-title.down {{
      color: #1d4ed8;
      background: rgba(29, 78, 216, 0.08);
    }}
    .butterfly-center {{
      background: var(--ink);
      opacity: 0.82;
    }}
    .butterfly-body {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 1px minmax(0, 1fr);
      min-height: 0;
    }}
    .butterfly-wing {{
      display: grid;
      grid-template-rows: repeat(15, minmax(0, 1fr));
      align-content: stretch;
      gap: 2px;
      min-width: 0;
      padding: 6px 8px;
      background:
        repeating-linear-gradient(90deg, transparent 0 24%, rgba(100, 114, 118, 0.1) 24.2% 24.6%, transparent 24.8% 49%),
        var(--panel);
    }}
    .butterfly-row {{
      display: grid;
      align-items: center;
      min-width: 0;
      min-height: 0;
    }}
    .butterfly-row.up {{
      grid-template-columns: 68px minmax(0, 1fr);
    }}
    .butterfly-row.down {{
      grid-template-columns: minmax(0, 1fr) 68px;
    }}
    .butterfly-label {{
      display: grid;
      gap: 0;
      min-width: 0;
      font-variant-numeric: tabular-nums;
    }}
    .butterfly-row.up .butterfly-label {{
      padding-right: 8px;
      text-align: left;
    }}
    .butterfly-row.down .butterfly-label {{
      padding-left: 8px;
      text-align: right;
    }}
    .butterfly-label strong {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 11px;
      line-height: 1.15;
      font-weight: 800;
    }}
    .butterfly-label span {{
      font-size: 10px;
      line-height: 1.1;
      font-weight: 800;
    }}
    .butterfly-row.up .butterfly-label span {{
      color: #dc2626;
    }}
    .butterfly-row.down .butterfly-label span {{
      color: #1d4ed8;
    }}
    .butterfly-track {{
      position: relative;
      height: 14px;
      min-width: 0;
    }}
    .butterfly-bar {{
      position: absolute;
      top: 0;
      height: 14px;
      min-width: 4px;
      border-radius: 5px;
      transition: width 420ms ease, transform 420ms ease;
      box-shadow: inset 0 -8px 14px rgba(0, 0, 0, 0.08);
    }}
    .butterfly-bar.up {{
      right: 0;
      background: linear-gradient(90deg, #fb923c, #dc2626);
    }}
    .butterfly-bar.down {{
      left: 0;
      background: linear-gradient(90deg, #1d4ed8, #60a5fa);
    }}
    .butterfly-volume {{
      position: absolute;
      top: 50%;
      transform: translateY(-50%);
      color: var(--ink);
      font-size: 10px;
      line-height: 1;
      font-weight: 800;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    .butterfly-bar.up .butterfly-volume {{
      left: -7px;
      transform: translate(-100%, -50%);
      color: #dc2626;
    }}
    .butterfly-bar.down .butterfly-volume {{
      right: -7px;
      transform: translate(100%, -50%);
      color: #1d4ed8;
    }}
    .butterfly-empty {{
      grid-column: 1 / -1;
      align-self: center;
      justify-self: center;
      color: var(--muted);
      font-size: 13px;
    }}
    .intraday-mover-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      padding: 10px;
    }}
    .mover-groups {{
      display: grid;
      gap: 8px;
      padding: 10px;
    }}
    .mover-group {{
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: var(--panel);
    }}
    .mover-group-title {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      min-height: 34px;
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      color: var(--ink);
      background: var(--table-head);
      font-size: 13px;
      font-weight: 750;
    }}
    .mover-group-title span:last-child {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }}
    .mover-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 54px 58px;
      gap: 8px;
      align-items: center;
      min-height: 54px;
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
    }}
    .mover-row:last-child {{
      border-bottom: 0;
    }}
    .mover-name {{
      display: grid;
      gap: 3px;
      min-width: 0;
    }}
    .mover-name strong {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 13px;
    }}
    .mover-name span {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--muted);
      font-size: 12px;
    }}
    .rank-now {{
      display: grid;
      justify-items: end;
      gap: 2px;
      font-variant-numeric: tabular-nums;
    }}
    .rank-now strong {{
      font-size: 16px;
      line-height: 1;
    }}
    .rank-now span {{
      color: var(--muted);
      font-size: 11px;
    }}
    .delta {{
      display: inline-flex;
      justify-content: flex-end;
      font-weight: 800;
      font-variant-numeric: tabular-nums;
    }}
    .rank-strip {{
      display: inline-grid;
      gap: 4px;
      min-width: 176px;
    }}
    .rank-pills {{
      display: inline-grid;
      grid-template-columns: repeat(5, 30px);
      gap: 6px;
      justify-content: start;
    }}
    .rank-pill {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 30px;
      height: 30px;
      border: 1px solid #dce3ea;
      border-radius: 6px;
      background: #eef2f6;
      color: #18212b;
      font-size: 13px;
      font-weight: 650;
      font-variant-numeric: tabular-nums;
    }}
    .rank-pill.hot {{
      border-color: #f4b4ad;
      background: #ffe6e3;
      color: #b91c1c;
      font-weight: 750;
    }}
    .rank-pill.warm {{
      border-color: #e5c777;
      background: #fff0cf;
      color: #b45309;
      font-weight: 700;
    }}
    .rank-pill.empty {{
      border-color: #dce3ea;
      background: #eef2f6;
      color: var(--muted);
    }}
    .rank-summary {{
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .rank-delta {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 54px;
      min-height: 28px;
      font-size: 14px;
      font-weight: 850;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    .rank-delta.is-empty {{
      color: var(--muted);
      font-weight: 750;
    }}
    .rank-status {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 48px;
      min-height: 28px;
      padding: 0 12px;
      border-radius: 999px;
      background: #e8f1ff;
      color: #1d4ed8;
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
    }}
    .rank-status.is-new,
    .rank-status.is-surge {{
      background: #ffe5e0;
      color: #b91c1c;
    }}
    .rank-status.is-fade {{
      background: #dcf8ea;
      color: #047857;
    }}
    .rank-status.is-rebuild-needed {{
      background: #f1f5f9;
      color: #64748b;
    }}
    .volume-bar {{
      grid-column: 1 / -1;
      height: 5px;
      border-radius: 999px;
      background: color-mix(in srgb, var(--line) 70%, transparent);
      overflow: hidden;
    }}
    .volume-bar i {{
      display: block;
      height: 100%;
      border-radius: inherit;
      background: var(--accent);
    }}
    .quad-bg.up-up {{ fill: color-mix(in srgb, #dc2626 13%, transparent); }}
    .quad-bg.up-down {{ fill: color-mix(in srgb, #f59e0b 14%, transparent); }}
    .quad-bg.down-up {{ fill: color-mix(in srgb, #2563eb 12%, transparent); }}
    .quad-bg.down-down {{ fill: color-mix(in srgb, #16a34a 12%, transparent); }}
    .axis-line {{
      stroke: var(--overview-axis);
      stroke-width: 1.2;
    }}
    .axis-label {{
      fill: var(--muted);
      font-size: 12px;
    }}
    .quad-label {{
      fill: var(--ink);
      font-size: 13px;
      font-weight: 800;
    }}
    .trail {{
      fill: none;
      stroke: var(--overview-wick);
      stroke-width: 1.7;
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-opacity: 0.38;
      stroke-dasharray: 4 5;
    }}
    .trail-dot {{
      fill: var(--panel);
      stroke: var(--overview-wick);
      stroke-width: 1.5;
      opacity: 0.72;
    }}
    .trail-dot.current {{
      fill: var(--accent);
      stroke: var(--accent);
      opacity: 1;
    }}
    .motion-arrow {{
      fill: var(--accent);
      opacity: 0.56;
    }}
    .trajectory-head-tools {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      flex-wrap: wrap;
      min-width: 0;
    }}
    .trajectory-controls {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      flex-wrap: nowrap;
    }}
    .trajectory-button {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--panel);
      color: var(--ink);
      height: 28px;
      padding: 0 9px;
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
      white-space: nowrap;
    }}
    .trajectory-button.primary {{
      background: var(--ink);
      border-color: var(--ink);
      color: var(--panel);
    }}
    .trajectory-frame-label {{
      min-width: 48px;
      height: 28px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: color-mix(in srgb, var(--line) 28%, transparent);
      font-size: 12px;
      font-weight: 850;
      font-variant-numeric: tabular-nums;
    }}
    .trajectory-slider {{
      width: 110px;
      accent-color: var(--accent);
    }}
    .bubble {{
      fill-opacity: 0.78;
      stroke: var(--panel);
      stroke-width: 2;
    }}
    .bubble.up-up {{ fill: #dc2626; }}
    .bubble.up-down {{ fill: #b45309; }}
    .bubble.down-up {{ fill: #2563eb; }}
    .bubble.down-down {{ fill: #16a34a; }}
    .bubble-rank {{
      fill: #ffffff;
      font-size: 11px;
      font-weight: 800;
      text-anchor: middle;
      dominant-baseline: central;
      pointer-events: none;
    }}
    .bubble-text {{
      fill: var(--ink);
      font-size: 11px;
      font-weight: 800;
      paint-order: stroke;
      stroke: var(--panel);
      stroke-width: 3px;
      stroke-linejoin: round;
    }}
    .trajectory-empty {{
      min-height: 220px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--muted);
      font-size: 14px;
      padding: 20px;
      text-align: center;
    }}
    .pool-tabs-panel {{
      margin: 18px 0;
    }}
    .pool-tabs-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin: 20px 0 10px;
    }}
    .pool-tabs-title {{
      display: grid;
      gap: 3px;
    }}
    .pool-tabs-title h2 {{
      margin: 0;
      font-size: 18px;
      line-height: 1.3;
    }}
    .pool-tabs-title span {{
      color: var(--muted);
      font-size: 13px;
    }}
    .pool-tab-list {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }}
    .pool-tab {{
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      min-height: 32px;
      padding: 0 12px;
      font: inherit;
      font-size: 13px;
      font-weight: 750;
      white-space: nowrap;
    }}
    .pool-tab:hover {{
      color: var(--ink);
      background: var(--table-head);
    }}
    .pool-tab.is-active {{
      color: var(--ink);
      background: var(--accent-soft);
    }}
    .pool-tab-panel[hidden] {{
      display: none;
    }}
    .pool-tab-panel .table-wrap {{
      margin-bottom: 0;
    }}
    .pool-tab-panel .table-wrap table {{
      min-width: 900px;
    }}
    .pool-tab-panel th,
    .pool-tab-panel td {{
      padding: 10px 8px;
      font-size: 13px;
    }}
    .pool-tab-panel .stock {{
      min-width: 86px;
    }}
    .pool-tab-panel .stock strong {{
      font-size: 14px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 980px;
    }}
    th, td {{
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      white-space: nowrap;
    }}
    th {{
      position: sticky;
      top: 0;
      background: var(--table-head);
      color: var(--ink);
      font-size: 12px;
      font-weight: 750;
    }}
    #new-entry-table-wrap th,
    #new-entry-table-wrap td {{
      padding-top: 9.6px;
      padding-bottom: 9.6px;
    }}
    .th-help {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 16px;
      height: 16px;
      margin-left: 5px;
      border: 1px solid var(--line);
      border-radius: 50%;
      color: var(--muted);
      background: var(--panel);
      font-size: 11px;
      line-height: 1;
      cursor: help;
      vertical-align: 1px;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .stock {{
      display: flex;
      flex-direction: column;
      gap: 3px;
      min-width: 128px;
    }}
    .stock strong {{ font-size: 15px; }}
    .stock span, .muted {{ color: var(--muted); font-size: 12px; }}
    .number {{ font-variant-numeric: tabular-nums; }}
    .empty {{
      padding: 42px 18px;
      text-align: center;
      color: var(--muted);
    }}
    .empty-row {{
      padding: 28px 14px;
      text-align: center;
      color: var(--muted);
    }}
    tr.row-updated td {{
      animation: row-update-flash 2.4s ease-out;
    }}
    @keyframes row-update-flash {{
      0% {{ background: color-mix(in srgb, var(--accent) 18%, transparent); }}
      100% {{ background: transparent; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      tr.row-updated td {{
        animation: none;
        background: color-mix(in srgb, var(--accent) 10%, transparent);
      }}
    }}
    .section-head {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin: 20px 0 10px;
    }}
    .section-head h2 {{
      margin: 0;
      font-size: 18px;
      line-height: 1.3;
    }}
    .section-head span {{
      color: var(--muted);
      font-size: 13px;
    }}
    .positive {{ color: #dc2626; }}
    .negative {{ color: #16a34a; }}
    .footer {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
      margin-top: 12px;
    }}
    @media (max-width: 1024px) {{
      .shell {{ padding: 20px; }}
      .topbar {{ align-items: flex-start; flex-direction: column; }}
      .actions {{ justify-content: flex-start; width: 100%; }}
      .intraday-grid {{ grid-template-columns: 1fr; }}
      .intraday-mover-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .pool-tabs-header {{
        align-items: flex-start;
        flex-direction: column;
      }}
      .pool-tab-list {{
        width: 100%;
        overflow-x: auto;
      }}
      .pool-tab-panel .table-wrap table {{ min-width: 900px; }}
    }}
    @media (max-width: 760px) {{
      body {{ font-size: 14px; }}
      .shell {{ padding: 14px; }}
      .topbar {{ gap: 12px; padding-bottom: 14px; }}
      .actions {{
        display: grid;
        grid-template-columns: minmax(68px, 0.8fr) minmax(108px, 1.2fr) 30px 34px;
        gap: 6px;
        align-items: center;
        width: 100%;
      }}
      #status-text,
      #realtime-status {{
        justify-content: center;
        width: 100%;
        min-width: 0;
        min-height: 30px;
        padding: 0 8px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        font-size: 12px;
      }}
      .connection-status {{
        width: 30px;
        min-width: 30px;
        min-height: 30px;
      }}
      .theme-toggle {{
        width: 34px;
        min-width: 34px;
        min-height: 30px;
      }}
      .criteria {{
        display: grid;
        grid-template-columns: 1fr;
        gap: 8px;
      }}
      .filter-control,
      .chip {{
        width: 100%;
        border-radius: 6px;
      }}
      .filter-control {{
        justify-content: space-between;
      }}
      .filter-control select,
      .filter-control input {{
        min-width: 92px;
      }}
      .metrics {{ grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }}
      .metric {{ min-height: 74px; padding: 11px; }}
      h1 {{ font-size: 24px; }}
      .value {{ font-size: 19px; }}
      .today-overview-head h2 {{ font-size: 20px; }}
      .section-head {{
        align-items: flex-start;
        flex-direction: column;
        gap: 4px;
      }}
      table {{ min-width: 760px; }}
      #watchlist-table-wrap table {{ min-width: 900px; }}
      .pool-tab-panel .table-wrap table {{ min-width: 880px; }}
      .intraday-mover-grid {{ grid-template-columns: 1fr; }}
      .butterfly-card {{ overflow-x: auto; }}
      .butterfly-head,
      .butterfly-body {{ min-width: 640px; }}
      th, td {{ padding: 10px 9px; }}
    }}
    @media (max-width: 520px) {{
      .metrics {{ grid-template-columns: 1fr; }}
      .subtle {{ line-height: 1.5; }}
      .actions {{ grid-template-columns: minmax(62px, 0.8fr) minmax(96px, 1.2fr) 30px 34px; }}
      .button:not(.theme-toggle) {{ min-width: 0; }}
      table {{ min-width: 700px; }}
      #watchlist-table-wrap table {{ min-width: 840px; }}
      .pool-tab-panel .table-wrap table {{ min-width: 860px; }}
      .intraday-grid {{ grid-template-columns: 1fr; }}
      .overview-chart-toolbar {{
        align-items: flex-start;
        flex-direction: column;
        gap: 8px;
      }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="topbar">
      <div>
        <h1>每日股期股池</h1>
        <div class="subtle">交易日 <span id="as-of-date">{as_of_date}</span> · 更新 <span id="generated-at">{generated_at}</span> · 盤後更新時間 13:30 13:45 14:00</div>
      </div>
      <div class="actions">
        <span class="status" id="status-text">{status_text}</span>
        <span class="status secondary" id="realtime-status">即時排序檢查中</span>
        <span class="connection-status is-offline" id="session-status" role="status" aria-label="非交易時段" title="非交易時段">
          <span class="status-dot" aria-hidden="true"></span>
        </span>
        <button class="button theme-toggle" id="theme-toggle" type="button" aria-label="切換深色或淺色主題" title="切換深色或淺色主題">☾</button>
      </div>
    </section>

    <section class="metrics" aria-label="股池摘要">
      <div class="metric"><div class="label">小型股期股池</div><div class="value" id="row-count">{row_count}</div></div>
      <div class="metric"><div class="label">大型股期股池</div><div class="value" id="active-row-count">{active_row_count}</div></div>
      <div class="metric"><div class="label">股期檔數</div><div class="value" id="watchlist-count">{watchlist_count}</div></div>
    </section>

    <section class="criteria" aria-label="篩選條件">
      <label class="filter-control" for="atr-filter">
        <span>ATR門檻</span>
        <select id="atr-filter" name="min_atr_percent">
          {atr_options}
        </select>
      </label>
      <label class="filter-control" for="as-of-filter">
        <span>回溯日期</span>
        <input id="as-of-filter" name="as_of" type="date" value="{as_of_filter_value}" max="{today_date}">
      </label>
      <span class="chip">近 {volume_days} 日所有股票期貨口數 Top {volume_top_n}</span>
      <span class="chip">小型股期：500~5000</span>
      <span class="chip">大型股期：200 以下</span>
      <span class="chip" id="atr-criteria-chip">股期日 K ATR {atr_days} 日 >= <span id="atr-criteria-value">{min_atr_percent:g}%</span></span>
    </section>

    <section class="today-overview" aria-label="今日速覽">
      <div class="today-overview-head">
        <h2>今日速覽</h2>
        <span>排序 : 成交口數Top50</span>
      </div>
      <div class="scroll-frame overview-frame">
        <section class="overview-chart-shell" id="today-overview-chart" aria-label="成交口數Top50今日速覽">
          {today_overview_content}
        </section>
      </div>
    </section>

    <section class="intraday-movers-panel" aria-label="盤中 Top Movers">
      <section class="section-head">
        <h2>盤中 Top Movers</h2>
        <span id="intraday-mover-time">等待盤中截點</span>
      </section>
      <div class="intraday-card">
        <div class="intraday-mover-grid" id="intraday-movers"></div>
      </div>
    </section>

    <section class="intraday-grid" aria-label="排名軌跡與強弱勢排序">
      <div class="trajectory-panel">
        <section class="section-head">
          <h2>排名軌跡</h2>
          <div class="trajectory-head-tools">
            <span id="intraday-trajectory-time">08:45 起，每 15 分鐘截點</span>
            <div class="trajectory-controls" aria-label="排名軌跡播放控制">
              <button class="trajectory-button primary" id="intraday-trajectory-play" type="button">播放</button>
              <button class="trajectory-button" id="intraday-trajectory-replay" type="button">重播</button>
              <span class="trajectory-frame-label" id="intraday-trajectory-frame-label">--:--</span>
              <input class="trajectory-slider" id="intraday-trajectory-slider" type="range" min="0" max="0" value="0" step="1" aria-label="排名軌跡截點">
            </div>
          </div>
        </section>
        <div class="trajectory-chart-shell">
          <svg class="trajectory-chart" id="intraday-trajectory-chart" viewBox="0 0 820 450" role="img" aria-label="排名軌跡"></svg>
        </div>
      </div>

      <aside class="intraday-side-panel" aria-label="強弱勢排序">
        <section class="section-head">
          <h2>強弱勢排序</h2>
        </section>
        <section class="butterfly-card" aria-label="強弱勢排序">
          <div class="butterfly-head">
            <div class="butterfly-title up">強勢股</div>
            <div class="butterfly-center"></div>
            <div class="butterfly-title down">弱勢股</div>
          </div>
          <div class="butterfly-body">
            <div class="butterfly-wing up" id="butterfly-up-wing"></div>
            <div class="butterfly-center"></div>
            <div class="butterfly-wing down" id="butterfly-down-wing"></div>
          </div>
        </section>
      </aside>
    </section>

    <section class="pool-tabs-panel" aria-label="股期股池與新進榜">
      <div class="pool-tabs-header">
        <div class="pool-tabs-title">
          <h2>精選股池</h2>
          <span id="pool-tabs-subtitle">小型股票期貨，價格 500~5000，流動與波動同時達標</span>
        </div>
        <div class="pool-tab-list" role="tablist" aria-label="股期股池頁簽">
          <button class="pool-tab is-active" id="pool-tab-small" type="button" role="tab" aria-selected="true" aria-controls="pool-panel-small" data-pool-tab="small">小型股期</button>
          <button class="pool-tab" id="pool-tab-large" type="button" role="tab" aria-selected="false" aria-controls="pool-panel-large" data-pool-tab="large">大型股期</button>
          <button class="pool-tab" id="pool-tab-new" type="button" role="tab" aria-selected="false" aria-controls="pool-panel-new" data-pool-tab="new">新進榜</button>
        </div>
      </div>
      <div class="pool-tab-panel" id="pool-panel-small" role="tabpanel" aria-labelledby="pool-tab-small" data-pool-panel="small">
        <div class="scroll-frame">
          <section class="table-wrap" id="pool-table-wrap" aria-label="小型股期股池列表" data-max-atr="{max_atr:.2f}">
            {table_content}
          </section>
        </div>
      </div>
      <div class="pool-tab-panel" id="pool-panel-large" role="tabpanel" aria-labelledby="pool-tab-large" data-pool-panel="large" hidden>
        <div class="scroll-frame">
          <section class="table-wrap" id="active-pool-table-wrap" aria-label="大型股期股池列表" data-max-atr="{max_atr:.2f}">
            {active_table_content}
          </section>
        </div>
      </div>
      <div class="pool-tab-panel" id="pool-panel-new" role="tabpanel" aria-labelledby="pool-tab-new" data-pool-panel="new" hidden>
        <div class="scroll-frame">
          <section class="table-wrap" id="new-entry-table-wrap" aria-label="新進榜列表">
            {new_entry_content}
          </section>
        </div>
      </div>
    </section>

    <section class="pool-tabs-panel watchlist-tabs-panel" aria-label="股票期貨 Watchlist">
      <div class="pool-tabs-header">
        <div class="pool-tabs-title">
          <h2>股票期貨 Watchlist</h2>
          <span id="watchlist-tabs-subtitle">全部股票期貨產品，即時報價一律取近月契約</span>
        </div>
        <div class="pool-tab-list" role="tablist" aria-label="股票期貨 Watchlist 分類">
          <button class="pool-tab is-active" id="watchlist-tab-all" type="button" role="tab" aria-selected="true" aria-controls="watchlist-panel" data-watchlist-tab="all">全部</button>
          <button class="pool-tab" id="watchlist-tab-large" type="button" role="tab" aria-selected="false" aria-controls="watchlist-panel" data-watchlist-tab="regular">大型股期</button>
          <button class="pool-tab" id="watchlist-tab-small" type="button" role="tab" aria-selected="false" aria-controls="watchlist-panel" data-watchlist-tab="small">小型股期</button>
        </div>
      </div>
      <div class="pool-tab-panel" id="watchlist-panel" role="tabpanel" aria-labelledby="watchlist-tab-all" data-watchlist-panel="all">
        <div class="scroll-frame">
          <section class="table-wrap" id="watchlist-table-wrap" aria-label="股票期貨 watchlist">
            {watchlist_content}
          </section>
        </div>
      </div>
    </section>

    <section class="footer">
      <span>合約資料 <span id="contract-rows">{contract_rows}</span> 筆</span>
    </section>
  </main>
</body>
</html>""".format(
        as_of_date=escape(snapshot.as_of_date),
        generated_at=escape(snapshot.generated_at),
        status_text=escape(status_text),
        row_count=snapshot.row_count,
        active_row_count=snapshot.active_row_count,
        watchlist_count=snapshot.watchlist_count,
        volume_window=escape(snapshot.volume_window or "-"),
        atr_window=escape(snapshot.atr_window or "-"),
        price_rows=snapshot.source.get("price_rows", 0),
        contract_rows=snapshot.source.get("contract_rows", 0),
        volume_days=snapshot.criteria["volume_days"],
        volume_top_n=snapshot.criteria["volume_top_n"],
        atr_days=snapshot.criteria["atr_days"],
        min_price=snapshot.criteria["min_price"],
        max_price=snapshot.criteria["max_price"],
        min_atr_percent=snapshot.criteria["min_atr_percent"],
        atr_options=_render_min_atr_percent_options(float(snapshot.criteria["min_atr_percent"])),
        as_of_filter_value=escape(as_of_filter_value),
        today_date=escape(today_date),
        today_overview_content=today_overview_html,
        table_content=rows_html,
        active_table_content=active_rows_html,
        new_entry_content=new_entry_html,
        watchlist_content=watchlist_html,
        max_atr=max_atr,
    )


def render_dashboard_shell() -> str:
    snapshot = DashboardSnapshot(
        generated_at="載入中",
        as_of_date="-",
        row_count=0,
        active_row_count=0,
        new_entry_count=0,
        watchlist_count=0,
        volume_window="-",
        atr_window="-",
        criteria=criteria_to_dict(StockPoolCriteria()),
        rows=[],
        active_rows=[],
        new_entry_rows=[],
        watchlist_rows=[],
        source={"price_rows": 0, "contract_rows": 0},
    )
    html = render_dashboard_html(snapshot)
    html = html.replace("無符合標的", "資料載入中")
    html = html.replace("今日沒有符合條件的標的", "資料載入中")
    return html.replace("</body>", _dashboard_script() + "\n</body>")


def render_error_html(message: str) -> str:
    return """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>每日股期股池</title>
  <style>
    body {{
      margin: 0;
      background: #f6f8fb;
      color: #18211f;
      font-family: "Segoe UI", "Noto Sans TC", Arial, sans-serif;
    }}
    main {{
      max-width: 760px;
      margin: 80px auto;
      padding: 24px;
      background: #ffffff;
      border: 1px solid #d9e1ea;
      border-radius: 8px;
    }}
    h1 {{ margin: 0 0 12px; font-size: 24px; }}
    p {{ color: #647276; line-height: 1.6; }}
    a {{
      display: inline-flex;
      align-items: center;
      min-height: 36px;
      padding: 0 14px;
      border-radius: 6px;
      background: #0f766e;
      color: #ffffff;
      text-decoration: none;
      font-weight: 650;
    }}
  </style>
</head>
<body>
  <main>
    <h1>每日股期股池</h1>
    <p>{message}</p>
    <a href="/">回首頁</a>
  </main>
</body>
</html>""".format(message=escape(message))


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = ThreadingHTTPServer((host, port), DashboardRequestHandler)
    print("Dashboard running at http://{}:{}".format(host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        print("\nDashboard stopped.")
    finally:
        server.server_close()


class DashboardCache:
    def __init__(self, cache_dir: Optional[str] = None) -> None:
        self.snapshots: Dict[Tuple[object, ...], DashboardSnapshot] = {}
        self.loaded_at: Dict[Tuple[object, ...], datetime] = {}
        self.refresh_locks: Dict[Tuple[object, ...], Lock] = {}
        self.lock_guard = Lock()
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.cache_dir = cache_dir or os.path.join(project_root, "data", "cache")

    def get_snapshot(
        self,
        force_refresh: bool = False,
        criteria: StockPoolCriteria = StockPoolCriteria(),
        as_of_date: Optional[object] = None,
    ) -> DashboardSnapshot:
        now = taipei_now()
        normalized_as_of = resolve_dashboard_as_of_date(as_of_date, now=now, cache_dir=self.cache_dir)
        if normalized_as_of > now.date():
            normalized_as_of = now.date()
        historical = normalized_as_of < now.date()
        trading_session = normalized_as_of == now.date() and is_taipei_trading_session(now)
        ttl_env = "DASHBOARD_HISTORICAL_CACHE_SECONDS" if historical else "DASHBOARD_CACHE_SECONDS"
        default_ttl = str(30 * 24 * 60 * 60) if historical else str(DEFAULT_LIVE_CACHE_SECONDS if trading_session else 900)
        ttl = int(os.getenv(ttl_env, default_ttl))
        closing_refresh_at = latest_closing_refresh_at(now, normalized_as_of)

        if historical:
            return self._get_or_build_snapshot(
                criteria,
                normalized_as_of,
                CACHE_KIND_FINAL,
                now,
                ttl,
                historical,
                trading_session,
                closing_refresh_at,
                force_refresh,
            )

        if trading_session:
            return self._get_or_build_snapshot(
                criteria,
                normalized_as_of,
                CACHE_KIND_INTRADAY,
                now,
                ttl,
                historical,
                trading_session,
                closing_refresh_at,
                force_refresh,
            )

        cached_final = None if force_refresh else self._get_cached_snapshot(
            criteria,
            normalized_as_of,
            CACHE_KIND_FINAL,
            now,
            ttl,
            historical,
            trading_session,
            closing_refresh_at,
        )
        if cached_final is not None and snapshot_final_ready(cached_final):
            return cached_final

        final_key = self._criteria_key(criteria, normalized_as_of, CACHE_KIND_FINAL)
        final_candidate = None
        final_error = None
        with self._refresh_lock(final_key):
            now = taipei_now()
            trading_session = normalized_as_of == now.date() and is_taipei_trading_session(now)
            closing_refresh_at = latest_closing_refresh_at(now, normalized_as_of)
            if not force_refresh:
                cached_final = self._get_cached_snapshot(
                    criteria,
                    normalized_as_of,
                    CACHE_KIND_FINAL,
                    now,
                    ttl,
                    historical,
                    trading_session,
                    closing_refresh_at,
                )
                if cached_final is not None and snapshot_final_ready(cached_final):
                    return cached_final
            try:
                final_candidate = build_daily_pool_snapshot(end_date=normalized_as_of, criteria=criteria)
                final_candidate = snapshot_with_source(final_candidate, {"cache_kind": CACHE_KIND_FINAL})
                if snapshot_final_ready(final_candidate):
                    self._store_snapshot(criteria, normalized_as_of, CACHE_KIND_FINAL, final_candidate, now)
                    return final_candidate
            except Exception as error:
                final_error = error

            if final_candidate is not None:
                return final_candidate
            raise final_error or RuntimeError("盤後日資料尚未完成")

    def _get_or_build_snapshot(
        self,
        criteria: StockPoolCriteria,
        as_of_date: object,
        cache_kind: str,
        now: datetime,
        ttl: int,
        historical: bool,
        trading_session: bool,
        closing_refresh_at: Optional[datetime],
        force_refresh: bool,
    ) -> DashboardSnapshot:
        if not force_refresh:
            cached = self._get_cached_snapshot(
                criteria,
                as_of_date,
                cache_kind,
                now,
                ttl,
                historical,
                trading_session,
                closing_refresh_at,
            )
            if cached is not None:
                return cached

        key = self._criteria_key(criteria, as_of_date, cache_kind)
        with self._refresh_lock(key):
            now = taipei_now()
            normalized_as_of = _normalize_date(as_of_date)
            trading_session = normalized_as_of == now.date() and is_taipei_trading_session(now)
            closing_refresh_at = latest_closing_refresh_at(now, as_of_date)
            if not force_refresh:
                cached = self._get_cached_snapshot(
                    criteria,
                    as_of_date,
                    cache_kind,
                    now,
                    ttl,
                    historical,
                    trading_session,
                    closing_refresh_at,
                )
                if cached is not None:
                    return cached

            try:
                snapshot = build_daily_pool_snapshot(end_date=as_of_date, criteria=criteria)
                snapshot = snapshot_with_source(snapshot, {"cache_kind": cache_kind})
                self._store_snapshot(criteria, as_of_date, cache_kind, snapshot, now)
                return snapshot
            except Exception as error:
                stale = self._get_cached_snapshot(
                    criteria,
                    as_of_date,
                    cache_kind,
                    now,
                    ttl,
                    historical,
                    trading_session,
                    closing_refresh_at,
                    allow_stale=True,
                )
                if stale is not None:
                    return snapshot_with_source(
                        stale,
                        {
                            "cache_kind": cache_kind,
                            "stale_cache_fallback": True,
                            "stale_cache_reason": str(error),
                        },
                    )
                raise

    def _get_cached_snapshot(
        self,
        criteria: StockPoolCriteria,
        as_of_date: object,
        cache_kind: str,
        now: datetime,
        ttl: int,
        historical: bool,
        trading_session: bool,
        closing_refresh_at: Optional[datetime],
        allow_stale: bool = False,
    ) -> Optional[DashboardSnapshot]:
        key = self._criteria_key(criteria, as_of_date, cache_kind)
        if key in self.snapshots and key in self.loaded_at:
            snapshot = migrate_cached_snapshot(self.snapshots[key])
            if allow_stale or is_cached_snapshot_usable(
                snapshot,
                self.loaded_at[key],
                now,
                ttl,
                historical,
                trading_session,
                closing_refresh_at,
            ):
                snapshot = snapshot_with_source(snapshot, {"cache_kind": cache_kind})
                self.snapshots[key] = snapshot
                return snapshot

        disk_snapshot, disk_loaded_at = self._read_disk_snapshot(criteria, as_of_date, cache_kind)
        if disk_snapshot is not None and disk_loaded_at is not None:
            disk_snapshot = migrate_cached_snapshot(disk_snapshot)
            if allow_stale or is_cached_snapshot_usable(
                disk_snapshot,
                disk_loaded_at,
                now,
                ttl,
                historical,
                trading_session,
                closing_refresh_at,
            ):
                disk_snapshot = snapshot_with_source(disk_snapshot, {"cache_kind": cache_kind})
                self.snapshots[key] = disk_snapshot
                self.loaded_at[key] = disk_loaded_at
                return disk_snapshot
        return None

    def _store_snapshot(
        self,
        criteria: StockPoolCriteria,
        as_of_date: object,
        cache_kind: str,
        snapshot: DashboardSnapshot,
        loaded_at: datetime,
    ) -> None:
        key = self._criteria_key(criteria, as_of_date, cache_kind)
        self.snapshots[key] = snapshot
        self.loaded_at[key] = loaded_at
        self._write_disk_snapshot(criteria, as_of_date, cache_kind, snapshot)

    def _refresh_lock(self, key: Tuple[object, ...]) -> Lock:
        with self.lock_guard:
            if key not in self.refresh_locks:
                self.refresh_locks[key] = Lock()
            return self.refresh_locks[key]

    def _criteria_key(
        self,
        criteria: StockPoolCriteria,
        as_of_date: Optional[object] = None,
        cache_kind: str = CACHE_KIND_FINAL,
    ) -> Tuple[object, ...]:
        normalized_as_of = _normalize_date(as_of_date)
        return (
            normalized_as_of.isoformat() if normalized_as_of is not None else "latest",
            criteria.volume_days,
            criteria.volume_top_n,
            criteria.atr_days,
            criteria.min_price,
            criteria.max_price,
            criteria.min_atr_percent,
            cache_kind,
        )

    def _cache_path(
        self,
        criteria: StockPoolCriteria,
        as_of_date: Optional[object],
        cache_kind: str = CACHE_KIND_FINAL,
    ) -> str:
        normalized_as_of = _normalize_date(as_of_date)
        as_of_key = normalized_as_of.isoformat() if normalized_as_of is not None else "latest"
        filename = "dashboard_asof{as_of}_{cache_kind}_vol{volume_days}_top{volume_top_n}_atr{atr_days}_price{min_price:g}-{max_price:g}_minatr{min_atr}.json".format(
            as_of=as_of_key,
            cache_kind=cache_kind,
            volume_days=criteria.volume_days,
            volume_top_n=criteria.volume_top_n,
            atr_days=criteria.atr_days,
            min_price=criteria.min_price,
            max_price=criteria.max_price,
            min_atr=_format_percent(criteria.min_atr_percent).replace(".", "_"),
        )
        return os.path.join(self.cache_dir, filename)

    def _legacy_cache_path(self, criteria: StockPoolCriteria, as_of_date: Optional[object]) -> str:
        normalized_as_of = _normalize_date(as_of_date)
        as_of_key = normalized_as_of.isoformat() if normalized_as_of is not None else "latest"
        filename = "dashboard_asof{as_of}_vol{volume_days}_top{volume_top_n}_atr{atr_days}_price{min_price:g}-{max_price:g}_minatr{min_atr}.json".format(
            as_of=as_of_key,
            volume_days=criteria.volume_days,
            volume_top_n=criteria.volume_top_n,
            atr_days=criteria.atr_days,
            min_price=criteria.min_price,
            max_price=criteria.max_price,
            min_atr=_format_percent(criteria.min_atr_percent).replace(".", "_"),
        )
        return os.path.join(self.cache_dir, filename)

    def _read_disk_snapshot(
        self,
        criteria: StockPoolCriteria,
        as_of_date: Optional[object],
        cache_kind: str = CACHE_KIND_FINAL,
    ) -> Tuple[Optional[DashboardSnapshot], Optional[datetime]]:
        paths = [self._cache_path(criteria, as_of_date, cache_kind), self._legacy_cache_path(criteria, as_of_date)]
        for path in paths:
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as cache_file:
                    payload = json.load(cache_file)
                loaded_at = datetime.fromtimestamp(os.path.getmtime(path), tz=TAIPEI_TZ)
                return DashboardSnapshot(**payload), loaded_at
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return None, None

    def _write_disk_snapshot(
        self,
        criteria: StockPoolCriteria,
        as_of_date: Optional[object],
        cache_kind: str,
        snapshot: DashboardSnapshot,
    ) -> None:
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            path = self._cache_path(criteria, as_of_date, cache_kind)
            temp_path = "{}.{}.tmp".format(path, os.getpid())
            with open(temp_path, "w", encoding="utf-8") as cache_file:
                json.dump(asdict(snapshot), cache_file, ensure_ascii=False)
            os.replace(temp_path, path)
        except OSError:
            return


def _cache_records(df: Optional[pd.DataFrame]) -> List[Dict[str, object]]:
    if df is None or df.empty:
        return []
    records = []
    for raw_row in df.to_dict(orient="records"):
        row = {}
        for key, value in raw_row.items():
            if value is None:
                row[key] = None
                continue
            try:
                if pd.isna(value):
                    row[key] = None
                    continue
            except (TypeError, ValueError):
                pass
            if isinstance(value, pd.Timestamp):
                row[key] = value.isoformat()
            elif isinstance(value, (datetime, date)):
                row[key] = value.isoformat()
            elif hasattr(value, "item"):
                try:
                    row[key] = value.item()
                except (TypeError, ValueError):
                    row[key] = value
            else:
                row[key] = value
        records.append(row)
    return records


class ContractMetadataCache:
    def __init__(self, cache_dir: Optional[str] = None) -> None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.cache_dir = cache_dir or os.path.join(project_root, "data", "cache", "contract_metadata")
        self.lock = Lock()

    def read(self, allow_stale: bool = False, now: Optional[datetime] = None) -> Optional[Dict[str, object]]:
        path = self._cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as cache_file:
                payload = json.load(cache_file)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if int(payload.get("version") or 0) != CONTRACT_METADATA_CACHE_SCHEMA_VERSION:
            return None

        loaded_at = self._parse_cached_at(payload.get("cached_at"))
        if loaded_at is None:
            return None
        current = now or taipei_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=TAIPEI_TZ)
        current = current.astimezone(TAIPEI_TZ)
        age_seconds = max(0, int((current - loaded_at).total_seconds()))
        ttl = _env_int("CONTRACT_METADATA_CACHE_SECONDS", DEFAULT_CONTRACT_METADATA_CACHE_SECONDS)
        is_fresh = age_seconds < ttl
        if not allow_stale and not is_fresh:
            return None

        stock_futures = pd.DataFrame(payload.get("stock_futures") or [])
        if stock_futures.empty:
            return None
        contracts = pd.DataFrame(payload.get("contracts") or [])
        fugle_products = pd.DataFrame(payload.get("fugle_products") or [])
        return {
            "source": str(payload.get("source") or "contract metadata cache"),
            "cached_at": payload.get("cached_at"),
            "age_seconds": age_seconds,
            "is_fresh": is_fresh,
            "stock_futures": stock_futures,
            "contracts": contracts,
            "fugle_products": fugle_products,
        }

    def store(
        self,
        source: str,
        stock_futures: pd.DataFrame,
        contracts: pd.DataFrame,
        fugle_products: Optional[pd.DataFrame] = None,
        now: Optional[datetime] = None,
    ) -> None:
        if stock_futures is None or stock_futures.empty:
            return
        current = now or taipei_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=TAIPEI_TZ)
        current = current.astimezone(TAIPEI_TZ)
        payload = {
            "version": CONTRACT_METADATA_CACHE_SCHEMA_VERSION,
            "source": source,
            "cached_at": current.isoformat(),
            "stock_futures": _cache_records(stock_futures),
            "contracts": _cache_records(contracts),
            "fugle_products": _cache_records(fugle_products),
        }
        with self.lock:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                path = self._cache_path()
                temp_path = "{}.{}.tmp".format(path, os.getpid())
                with open(temp_path, "w", encoding="utf-8") as cache_file:
                    json.dump(payload, cache_file, ensure_ascii=False)
                os.replace(temp_path, path)
            except OSError:
                return

    def _cache_path(self) -> str:
        return os.path.join(self.cache_dir, "contract_metadata_latest.json")

    def _parse_cached_at(self, value: object) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = pd.Timestamp(value).to_pydatetime()
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TAIPEI_TZ)
        return parsed.astimezone(TAIPEI_TZ)


dashboard_cache = DashboardCache()
contract_metadata_cache = ContractMetadataCache()


def _minute_label(minutes: int) -> str:
    hour = minutes // 60
    minute = minutes % 60
    return "{:02d}:{:02d}".format(hour, minute)


def _label_to_minutes(label: object) -> int:
    try:
        hour_text, minute_text = str(label or "").split(":", 1)
        return int(hour_text) * 60 + int(minute_text)
    except (TypeError, ValueError):
        return INTRADAY_OPEN_MINUTES


def _intraday_cutoff_label(now: Optional[datetime] = None) -> Optional[str]:
    current = now or taipei_now()
    if current.weekday() >= 5:
        return None
    minutes = current.hour * 60 + current.minute
    if minutes < INTRADAY_OPEN_MINUTES:
        return None
    clamped = min(minutes, INTRADAY_CLOSE_MINUTES)
    bucket = (clamped - INTRADAY_OPEN_MINUTES) // INTRADAY_BUCKET_MINUTES
    return _minute_label(INTRADAY_OPEN_MINUTES + bucket * INTRADAY_BUCKET_MINUTES)


def _trajectory_number(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        text = str(value).replace(",", "").replace("%", "").strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


class IntradayTrajectoryCache:
    def __init__(
        self,
        cache_dir: Optional[str] = None,
        retention_days: int = INTRADAY_TRAJECTORY_RETENTION_DAYS,
        seed_dir: Optional[str] = None,
    ) -> None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.cache_dir = cache_dir or os.path.join(project_root, "data", "cache", "intraday_trajectory")
        self.seed_dir = seed_dir or os.path.join(project_root, "data", "bootstrap", "intraday_trajectory")
        self.retention_days = retention_days
        self.lock = Lock()

    def read(self, as_of_date: Optional[object] = None) -> Dict[str, object]:
        normalized_as_of = resolve_dashboard_as_of_date(as_of_date, cache_dir=self.cache_dir)
        history = self._empty_history(normalized_as_of)
        cached = self._read_history_file(self._cache_path(normalized_as_of), normalized_as_of)
        if cached is not None:
            return cached
        seeded = self._read_history_file(self._seed_path(normalized_as_of), normalized_as_of)
        if seeded is not None:
            self._write_history(normalized_as_of, seeded)
            return seeded
        return history

    def _read_history_file(self, path: str, as_of_date: object) -> Optional[Dict[str, object]]:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as cache_file:
                payload = json.load(cache_file)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return self._normalize_history(payload, as_of_date, cache_hit=True)

    def append_snapshot(self, snapshot: DashboardSnapshot, now: Optional[datetime] = None) -> Dict[str, object]:
        current = now or taipei_now()
        source = snapshot.source or {}
        effective_as_of = _normalize_date(source.get("effective_as_of_date") or snapshot.as_of_date) or current.date()
        if source.get("stale_cache_fallback"):
            return self.read(effective_as_of)
        if source.get("historical_mode") or not source.get("realtime_quote_enabled"):
            return self.read(effective_as_of)
        if effective_as_of != current.date():
            return self.read(effective_as_of)

        cutoff = _intraday_cutoff_label(current)
        rows = self._snapshot_rows(snapshot)
        if not cutoff or not rows:
            return self.read(effective_as_of)

        with self.lock:
            history = self.read(effective_as_of)
            snapshots = [
                item for item in history.get("snapshots", [])
                if isinstance(item, dict) and item.get("cutoff") != cutoff
            ]
            snapshots.append(
                {
                    "cutoff": cutoff,
                    "captured_at": format_taipei_datetime(current),
                    "status": "fresh",
                    "rows": rows,
                }
            )
            snapshots = sorted(snapshots, key=lambda item: _label_to_minutes(item.get("cutoff")))
            history.update(
                {
                    "version": 1,
                    "as_of_date": effective_as_of.isoformat(),
                    "updated_at": format_taipei_datetime(current),
                    "snapshots": snapshots,
                    "cache_hit": True,
                }
            )
            self._write_history(effective_as_of, history)
            self._prune()
            return history

    def replace_history(self, as_of_date: object, history: Dict[str, object]) -> Dict[str, object]:
        normalized_as_of = _normalize_date(as_of_date) or taipei_now().date()
        normalized = self._normalize_history(history, normalized_as_of, cache_hit=True)
        normalized["updated_at"] = normalized.get("updated_at") or format_taipei_datetime()
        normalized["cache_hit"] = True
        with self.lock:
            self._write_history(normalized_as_of, normalized)
            self._prune()
        return self.read(normalized_as_of)

    def _snapshot_rows(self, snapshot: DashboardSnapshot) -> List[Dict[str, object]]:
        rows = []
        for row in snapshot.watchlist_rows or []:
            if not isinstance(row, dict):
                continue
            stock_id = str(row.get("stock_id") or "").strip()
            volume = _trajectory_number(row.get("volume"))
            spread_per = _trajectory_number(row.get("spread_per"))
            if not stock_id or volume is None or spread_per is None:
                continue
            rows.append(
                {
                    "stock_id": stock_id,
                    "stock_name": str(row.get("stock_name") or stock_id),
                    "futures_id": _display_futures_id(row),
                    "contract_type_label": str(row.get("contract_type_label") or ""),
                    "close": _trajectory_number(row.get("close")),
                    "spread_per": spread_per,
                    "volume": volume,
                }
            )

        rows = sorted(rows, key=lambda item: (-float(item["volume"]), str(item["stock_id"])))
        result = []
        for index, row in enumerate(rows[:INTRADAY_TRAJECTORY_TOP_N], start=1):
            next_row = dict(row)
            next_row["rank"] = index
            result.append(next_row)
        return result

    def _cache_path(self, as_of_date: object) -> str:
        normalized_as_of = _normalize_date(as_of_date) or taipei_now().date()
        return os.path.join(self.cache_dir, "trajectory_asof{}.json".format(normalized_as_of.isoformat()))

    def _seed_path(self, as_of_date: object) -> str:
        normalized_as_of = _normalize_date(as_of_date) or taipei_now().date()
        return os.path.join(self.seed_dir, "trajectory_asof{}.json".format(normalized_as_of.isoformat()))

    def _empty_history(self, as_of_date: object) -> Dict[str, object]:
        normalized_as_of = _normalize_date(as_of_date) or taipei_now().date()
        return {
            "version": 1,
            "as_of_date": normalized_as_of.isoformat(),
            "updated_at": "",
            "snapshots": [],
            "cache_hit": False,
        }

    def _normalize_history(self, payload: object, as_of_date: object, cache_hit: bool = False) -> Dict[str, object]:
        normalized_as_of = _normalize_date(as_of_date) or taipei_now().date()
        result = self._empty_history(normalized_as_of)
        if not isinstance(payload, dict):
            return result
        snapshots = []
        for snapshot in payload.get("snapshots", []):
            if not isinstance(snapshot, dict):
                continue
            cutoff = str(snapshot.get("cutoff") or "").strip()
            rows = snapshot.get("rows")
            if not cutoff or not isinstance(rows, list):
                continue
            snapshots.append(
                {
                    "cutoff": cutoff,
                    "captured_at": str(snapshot.get("captured_at") or ""),
                    "status": str(snapshot.get("status") or "cached"),
                    "rows": [row for row in rows if isinstance(row, dict)],
                }
            )
        result.update(
            {
                "version": int(payload.get("version") or 1),
                "as_of_date": str(payload.get("as_of_date") or normalized_as_of.isoformat()),
                "updated_at": str(payload.get("updated_at") or ""),
                "snapshots": sorted(snapshots, key=lambda item: _label_to_minutes(item.get("cutoff"))),
                "cache_hit": cache_hit,
            }
        )
        if isinstance(payload.get("source"), dict):
            result["source"] = payload["source"]
        return result

    def _write_history(self, as_of_date: object, history: Dict[str, object]) -> None:
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            path = self._cache_path(as_of_date)
            temp_path = "{}.{}.tmp".format(path, os.getpid())
            with open(temp_path, "w", encoding="utf-8") as cache_file:
                json.dump(history, cache_file, ensure_ascii=False)
            os.replace(temp_path, path)
        except OSError:
            return

    def _prune(self) -> None:
        try:
            filenames = os.listdir(self.cache_dir)
        except OSError:
            return
        dated_paths = []
        for filename in filenames:
            if not filename.startswith("trajectory_asof") or not filename.endswith(".json"):
                continue
            date_text = filename[len("trajectory_asof"):-len(".json")]
            try:
                as_of = _normalize_date(date_text)
            except (TypeError, ValueError, OverflowError):
                continue
            dated_paths.append((as_of, os.path.join(self.cache_dir, filename)))
        dated_paths.sort(key=lambda item: item[0], reverse=True)
        for _, path in dated_paths[self.retention_days:]:
            try:
                os.remove(path)
            except OSError:
                continue


intraday_trajectory_cache = IntradayTrajectoryCache()


def _json_response(payload: Dict[str, object], status: int = 200) -> Tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _query_flag(query: Dict[str, List[str]], key: str) -> bool:
    value = (_first_query_value(query, key) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _header_value(headers: Optional[Dict[str, str]], name: str) -> str:
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return str(value or "")
    return ""


def _admin_auth_error(headers: Optional[Dict[str, str]]) -> Optional[Tuple[int, Dict[str, object]]]:
    load_environment()
    expected = get_admin_refresh_token()
    if not expected:
        return 503, {
            "error": "{} is not configured on the server".format(ADMIN_REFRESH_TOKEN_ENV),
            "status": "misconfigured",
        }
    authorization = _header_value(headers, "Authorization")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return 401, {"error": "missing bearer token", "status": "unauthorized"}
    token = authorization[len(prefix):].strip()
    if not hmac.compare_digest(token, expected):
        return 401, {"error": "invalid bearer token", "status": "unauthorized"}
    return None


def _resolve_admin_refresh_mode(requested_mode: Optional[str], now: Optional[datetime] = None) -> str:
    mode = str(requested_mode or "intraday_snapshot").strip().lower()
    aliases = {
        "intraday": "intraday_snapshot",
        "pool": "intraday_snapshot",
        "snapshot": "intraday_snapshot",
        "final": "final_snapshot",
        "rebuild": "rebuild_trajectory",
        "trajectory_rebuild": "rebuild_trajectory",
    }
    mode = aliases.get(mode, mode)
    if mode != "auto":
        return mode
    current = now or taipei_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI_TZ)
    current = current.astimezone(TAIPEI_TZ)
    minutes = current.hour * 60 + current.minute
    if minutes >= 14 * 60 + 45:
        return "rebuild_trajectory"
    if minutes >= POST_CLOSE_QUOTE_START_MINUTE:
        return "final_snapshot"
    return "intraday_snapshot"


def _trajectory_history_summary(history: Dict[str, object]) -> Dict[str, object]:
    snapshots = history.get("snapshots") if isinstance(history, dict) else []
    snapshots = snapshots if isinstance(snapshots, list) else []
    last_snapshot = snapshots[-1] if snapshots and isinstance(snapshots[-1], dict) else {}
    rows = last_snapshot.get("rows") if isinstance(last_snapshot, dict) else []
    return {
        "trajectory_snapshot_count": len(snapshots),
        "trajectory_last_cutoff": str(last_snapshot.get("cutoff") or ""),
        "trajectory_last_row_count": len(rows) if isinstance(rows, list) else 0,
        "trajectory_cache_hit": bool(history.get("cache_hit")) if isinstance(history, dict) else False,
    }


def rebuild_intraday_trajectory_from_fugle(
    as_of_date: Optional[object] = None,
    criteria: StockPoolCriteria = StockPoolCriteria(),
    force_snapshot: bool = False,
    timeout: int = 60,
) -> Dict[str, object]:
    load_environment()
    token = get_fugle_token()
    if not token:
        raise RuntimeError("FUGLE_API_KEY or FUGLE_API_TOKEN is required for trajectory rebuild")

    normalized_as_of = resolve_dashboard_as_of_date(as_of_date, cache_dir=dashboard_cache.cache_dir)
    snapshot = dashboard_cache.get_snapshot(
        force_refresh=force_snapshot,
        criteria=criteria,
        as_of_date=normalized_as_of,
    )
    watchlist_rows = snapshot.watchlist_rows or []
    if not watchlist_rows:
        raise RuntimeError("dashboard watchlist is empty; cannot rebuild trajectory")

    fugle_products = fetch_fugle_stock_futures_products(token, timeout=timeout)
    fugle_tickers = fetch_fugle_stock_futures_tickers(token, timeout=timeout)
    stock_futures = build_stock_futures_contract_map_from_fugle(fugle_products)
    if stock_futures.empty:
        cached_metadata = contract_metadata_cache.read(allow_stale=True)
        if cached_metadata is not None:
            stock_futures = cached_metadata["stock_futures"]
    if stock_futures.empty:
        raise RuntimeError("Fugle product metadata is unavailable; cannot map candle symbols")

    fugle_tickers, near_month_tickers = add_fugle_contract_months(fugle_tickers, stock_futures)
    if near_month_tickers.empty:
        raise RuntimeError("Fugle near-month tickers are unavailable; cannot fetch candles")

    watchlist_ids = {str(row.get("stock_id") or "").strip() for row in watchlist_rows if isinstance(row, dict)}
    product_ids = set(
        stock_futures.loc[
            stock_futures["stock_id"].astype(str).isin(watchlist_ids),
            "fugle_product_id",
        ].astype(str)
    )
    if product_ids:
        near_month_tickers = near_month_tickers[near_month_tickers["fugle_product_id"].astype(str).isin(product_ids)].copy()
    if near_month_tickers.empty:
        raise RuntimeError("No near-month tickers match the dashboard watchlist")

    candle_minutes = _env_int("FUGLE_TRAJECTORY_CANDLE_MINUTES", 5)
    raw_candles = fetch_fugle_near_month_candles(
        token=token,
        near_month_tickers=near_month_tickers,
        stock_futures=stock_futures,
        timeframe=str(candle_minutes),
        timeout=timeout,
    )
    normalized_candles = _normalize_fugle_candle_rows(raw_candles, stock_futures)
    history = build_intraday_trajectory_history_from_candles(
        as_of_date=normalized_as_of,
        watchlist_rows=watchlist_rows,
        candle_rows=normalized_candles,
        candle_minutes=candle_minutes,
        source={
            "snapshot_as_of_date": snapshot.as_of_date,
            "snapshot_cache_kind": str(snapshot.source.get("cache_kind") or ""),
            "snapshot_stage": str(snapshot.source.get("snapshot_stage") or ""),
            "fugle_product_rows": int(len(fugle_products)),
            "fugle_ticker_rows": int(len(fugle_tickers)),
            "fugle_near_month_rows": int(len(near_month_tickers)),
            "fugle_raw_candle_rows": int(len(raw_candles)),
            "fugle_normalized_candle_rows": int(len(normalized_candles)),
        },
    )
    if not history.get("snapshots"):
        raise RuntimeError("Fugle candles did not produce any intraday trajectory snapshots")
    return intraday_trajectory_cache.replace_history(normalized_as_of, history)


def _handle_admin_refresh(query: Dict[str, List[str]], as_of_date: Optional[date], criteria: StockPoolCriteria) -> Tuple[int, str, bytes]:
    mode = _resolve_admin_refresh_mode(_first_query_value(query, "mode"))
    if mode not in {"intraday_snapshot", "final_snapshot", "rebuild_trajectory"}:
        return _json_response({"error": "unsupported refresh mode: {}".format(mode), "status": "bad_request"}, 400)

    try:
        if mode in {"intraday_snapshot", "final_snapshot"}:
            snapshot = dashboard_cache.get_snapshot(force_refresh=True, criteria=criteria, as_of_date=as_of_date)
            trajectory = intraday_trajectory_cache.append_snapshot(snapshot)
            payload = {
                "status": "ok",
                "mode": mode,
                "as_of_date": snapshot.as_of_date,
                "generated_at": snapshot.generated_at,
                "cache_kind": str(snapshot.source.get("cache_kind") or ""),
                "snapshot_stage": str(snapshot.source.get("snapshot_stage") or ""),
                "final_ready": bool(snapshot.source.get("final_ready")),
                "watchlist_count": snapshot.watchlist_count,
                **_trajectory_history_summary(trajectory),
            }
        else:
            history = rebuild_intraday_trajectory_from_fugle(
                as_of_date=as_of_date,
                criteria=criteria,
                force_snapshot=_query_flag(query, "refresh_snapshot"),
            )
            payload = {
                "status": "ok",
                "mode": mode,
                "as_of_date": str(history.get("as_of_date") or ""),
                "updated_at": str(history.get("updated_at") or ""),
                **_trajectory_history_summary(history),
                "source": history.get("source", {}),
            }
        return _json_response(payload, 200)
    except Exception as exc:
        return _json_response({"error": str(exc), "status": "failed", "mode": mode}, 500)


def build_dashboard_response(
    path: str,
    query_string: str = "",
    headers: Optional[Dict[str, str]] = None,
    method: str = "GET",
) -> Tuple[int, str, bytes]:
    query = parse_qs(query_string)
    force_refresh = query.get("refresh") == ["1"]
    criteria = criteria_from_query(query)
    as_of_date = _parse_as_of_date(_first_query_value(query, "as_of"))

    if path in ("", "/", "/index.html"):
        return 200, "text/html; charset=utf-8", render_dashboard_shell().encode("utf-8")

    if path == "/api/pool":
        try:
            snapshot = dashboard_cache.get_snapshot(force_refresh=force_refresh, criteria=criteria, as_of_date=as_of_date)
            trajectory = intraday_trajectory_cache.append_snapshot(snapshot)
            payload = asdict(snapshot)
            payload["intraday_trajectory"] = trajectory
            status = 200
        except Exception as exc:
            payload = {"error": str(exc)}
            status = 500
        return status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8")

    if path == "/api/intraday-trajectory":
        payload = intraday_trajectory_cache.read(as_of_date)
        return _json_response(payload, 200)

    if path == "/api/admin/refresh":
        if method.upper() not in {"GET", "POST"}:
            return _json_response({"error": "method not allowed", "status": "method_not_allowed"}, 405)
        auth_error = _admin_auth_error(headers)
        if auth_error is not None:
            status, payload = auth_error
            return _json_response(payload, status)
        return _handle_admin_refresh(query, as_of_date, criteria)

    if path == "/health":
        payload = {"status": "ok"}
        return _json_response(payload, 200)

    return 404, "text/plain; charset=utf-8", b"Not found"


def application(environ, start_response):
    """WSGI entrypoint for PythonAnywhere and other WSGI hosts."""
    path = environ.get("PATH_INFO", "/")
    query_string = environ.get("QUERY_STRING", "")
    headers = {}
    if environ.get("HTTP_AUTHORIZATION"):
        headers["Authorization"] = environ.get("HTTP_AUTHORIZATION", "")
    method = environ.get("REQUEST_METHOD", "GET")
    status, content_type, body = build_dashboard_response(path, query_string, headers=headers, method=method)
    reason = {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }.get(status, "OK")
    status_line = "{} {}".format(status, reason)
    headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]
    start_response(status_line, headers)
    return [body]


class DashboardRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        status, content_type, body = build_dashboard_response(
            parsed.path,
            parsed.query,
            headers=dict(self.headers),
            method="GET",
        )
        self._send_body(body, content_type, status)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        status, content_type, body = build_dashboard_response(
            parsed.path,
            parsed.query,
            headers=dict(self.headers),
            method="POST",
        )
        self._send_body(body, content_type, status)

    def log_message(self, format: str, *args: object) -> None:  # pragma: no cover
        print("{} - {}".format(self.address_string(), format % args))

    def _send_body(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _render_table_rows(rows: List[Dict[str, object]]) -> str:
    body_rows = []
    for index, row in enumerate(rows, start=1):
        atr_percent = float(row["atr_20_percent"])
        spread_per = row.get("spread_per")
        spread_per_class = _change_class(spread_per)
        body_rows.append(
            """<tr>
  <td class="number">{rank}</td>
  <td><div class="stock"><strong>{stock_name}</strong><span>{stock_meta}</span></div></td>
  <td class="number">{close:,.2f}</td>
  <td class="number {spread_per_class}">{spread_per}</td>
  <td class="number">{atr_percent:,.2f}%</td>
  <td>{rank_strip}</td>
  <td class="number">{intraday_change}</td>
  <td class="number">{intraday_open_change}</td>
  <td>{rank_status}</td>
</tr>""".format(
                rank=index,
                stock_name=escape(str(row["stock_name"] or "-")),
                stock_meta=escape(_display_stock_meta(row)),
                close=float(row["close"]),
                spread_per=_format_optional_percent(spread_per, 2),
                spread_per_class=spread_per_class,
                atr_percent=atr_percent,
                rank_strip=_render_rank_strip(row),
                intraday_change=_render_intraday_change_placeholder(),
                intraday_open_change=_render_intraday_change_placeholder(),
                rank_status=_render_rank_status_placeholder(),
            )
        )

    if not body_rows:
        body_rows.append('<tr><td class="empty-row" colspan="9">今日沒有符合條件的標的</td></tr>')

    return """<table>
  <thead>
    <tr>
      <th>#</th>
      <th>股票</th>
      <th>收盤價</th>
      <th>漲跌幅%</th>
      <th>ATR20%</th>
      <th>近 5 日排名 <span class="th-help" title="依每日股期成交口數排序，紅色為前 10、黃色為前 25" aria-label="近 5 日排名說明">!</span></th>
      <th>盤中變化 <span class="th-help" title="上一截點排名減目前排名；正值代表排名往前" aria-label="盤中變化說明">!</span></th>
      <th>開盤累積 <span class="th-help" title="08:45 第一個截點排名減目前排名；正值代表從開盤累積轉強" aria-label="開盤累積說明">!</span></th>
      <th>狀態 <span class="th-help" title="依開盤第一個截點到最新截點的累積排名變化分類" aria-label="狀態說明">!</span></th>
    </tr>
  </thead>
  <tbody>
    {rows}
  </tbody>
</table>""".format(rows="\n".join(body_rows))


def _render_new_entry_rows(rows: List[Dict[str, object]]) -> str:
    body_rows = []
    for index, row in enumerate(rows, start=1):
        previous_rank = row.get("previous_rank")
        previous_volume = row.get("previous_volume")
        body_rows.append(
            """<tr>
  <td class="number">{rank}</td>
  <td><div class="stock"><strong>{stock_name}</strong><span>{stock_meta}</span></div></td>
  <td>{contract_type_label}</td>
  <td class="number">{current_rank}</td>
  <td class="number">{previous_rank}</td>
  <td class="number">{current_volume}</td>
  <td class="number">{previous_volume}</td>
  <td class="number">{close}</td>
  <td>{contract_date}</td>
</tr>""".format(
                rank=index,
                stock_name=escape(str(row.get("stock_name") or "-")),
                stock_meta=escape(_display_stock_meta(row)),
                contract_type_label=escape(str(row.get("contract_type_label") or "-")),
                current_rank=_format_optional_number(row.get("current_rank"), 0),
                previous_rank="新" if previous_rank is None else _format_optional_number(previous_rank, 0),
                current_volume=_format_optional_number(row.get("current_volume"), 0),
                previous_volume="-" if previous_volume is None else _format_optional_number(previous_volume, 0),
                close=_format_optional_number(row.get("close"), 2),
                contract_date=escape(str(row.get("contract_date") or "-")),
            )
        )

    if not body_rows:
        body_rows.append('<tr><td class="empty-row" colspan="9">今日沒有新進榜標的</td></tr>')

    return """<table>
  <thead>
    <tr>
      <th>#</th>
      <th>股票</th>
      <th>類型</th>
      <th>本日排名</th>
      <th>前日排名</th>
      <th>本日口數</th>
      <th>前日口數</th>
      <th>收盤價</th>
      <th>合約月份</th>
    </tr>
  </thead>
  <tbody>
    {rows}
  </tbody>
</table>""".format(rows="\n".join(body_rows))


def _render_watchlist_rows(rows: List[Dict[str, object]]) -> str:
    body_rows = []
    for index, row in enumerate(rows, start=1):
        spread = row.get("spread")
        spread_per = row.get("spread_per")
        spread_class = _change_class(spread)
        spread_per_class = _change_class(spread_per)
        body_rows.append(
            """<tr>
  <td class="number">{rank}</td>
  <td><div class="stock"><strong>{stock_name}</strong><span>{stock_meta}</span></div></td>
  <td class="number {spread_class}">{spread}</td>
  <td class="number {spread_per_class}">{spread_per}</td>
  <td class="number">{volume}</td>
  <td class="number">{open_price}</td>
  <td class="number">{high}</td>
  <td class="number">{low}</td>
  <td class="number">{close}</td>
  <td>{contract_type_label}</td>
  <td>{contract_date}</td>
  <td>{date}</td>
</tr>""".format(
                rank=index,
                stock_name=escape(str(row["stock_name"] or "-")),
                stock_meta=escape(_display_stock_meta(row)),
                contract_type_label=escape(str(row.get("contract_type_label") or "-")),
                contract_date=escape(str(row["contract_date"] or "-")),
                date=escape(str(row["date"] or "-")),
                open_price=_format_optional_number(row.get("open"), 2),
                high=_format_optional_number(row.get("high"), 2),
                low=_format_optional_number(row.get("low"), 2),
                close=_format_optional_number(row.get("close"), 2),
                volume=_format_optional_number(row.get("volume"), 0),
                spread=_format_optional_number(spread, 2),
                spread_per=_format_optional_percent(spread_per, 2),
                spread_class=spread_class,
                spread_per_class=spread_per_class,
            )
        )

    if not body_rows:
        body_rows.append('<tr><td class="empty-row" colspan="12">尚無股票期貨 watchlist 資料</td></tr>')

    return """<table>
  <thead>
    <tr>
      <th>#</th>
      <th>股票</th>
      <th>漲跌</th>
      <th>漲跌%</th>
      <th>成交口數</th>
      <th>開盤</th>
      <th>最高</th>
      <th>最低</th>
      <th>收盤</th>
      <th>類型</th>
      <th>合約月份</th>
      <th>日期</th>
    </tr>
  </thead>
  <tbody>
    {rows}
  </tbody>
</table>""".format(rows="\n".join(body_rows))


def _render_today_overview_chart(rows: List[Dict[str, object]]) -> str:
    chart_rows = _today_overview_rows(rows)
    if not chart_rows:
        return '<div class="overview-empty">尚無今日速覽資料</div>'

    width = max(2060, 50 + len(chart_rows) * 40 + 50)
    height = 430
    plot_top = 52
    plot_bottom = 266
    plot_left = 46
    plot_right = width - 24
    plot_height = plot_bottom - plot_top
    scale_min = -10
    scale_max = 10
    x_step = (plot_right - plot_left) / len(chart_rows)

    def y_position(percent: float) -> float:
        clamped = max(scale_min, min(scale_max, percent))
        return plot_bottom - ((clamped - scale_min) / (scale_max - scale_min)) * plot_height

    tick_lines = []
    for tick in (-10, -5, 0, 5, 10):
        tick_y = y_position(float(tick))
        label_class = "pos" if tick > 0 else "neg" if tick < 0 else ""
        tick_lines.append(
            '<line x1="{plot_left}" x2="{plot_right}" y1="{tick_y:.1f}" y2="{tick_y:.1f}" class="overview-grid"></line>'
            '<text x="40" y="{label_y:.1f}" class="overview-y-label {label_class}" text-anchor="end">{tick}%</text>'.format(
                plot_left=plot_left,
                plot_right=plot_right,
                tick_y=tick_y,
                label_y=tick_y + 5,
                label_class=label_class,
                tick=tick,
            )
        )

    candles = []
    for index, row in enumerate(chart_rows, start=1):
        center_x = plot_left + x_step * (index - 1) + x_step / 2
        candle_width = max(10, min(26, x_step * 0.56))
        open_y = y_position(row["open_pct"])
        close_y = y_position(row["close_pct"])
        high_y = y_position(row["high_pct"])
        low_y = y_position(row["low_pct"])
        body_y = min(open_y, close_y)
        body_height = max(3, abs(open_y - close_y))
        color = "#ff3438" if row["close_pct"] >= 0 else "#00c92f"
        label_tspans = "".join(
            '<tspan x="{x:.1f}" dy="{dy}">{char}</tspan>'.format(
                x=center_x,
                dy="0" if char_index == 0 else "1.05em",
                char=escape(char),
            )
            for char_index, char in enumerate(str(row["stock_name"]))
        )
        title = "{rank}. {stock_name} {change}｜成交口數 {volume}{symbol}".format(
            rank=index,
            stock_name=row["stock_name"],
            change=_format_overview_percent(row["close_pct"]),
            volume=_format_optional_number(row["volume"], 0),
            symbol="｜{}".format(_display_futures_id(row)) if _display_futures_id(row) != "-" else "",
        )
        candles.append(
            """<g class="overview-candle" tabindex="0" data-name="{stock_name}" data-pct="{change}" data-volume="{volume}">
  <title>{title}</title>
  <line class="overview-wick" x1="{x:.1f}" x2="{x:.1f}" y1="{high_y:.1f}" y2="{low_y:.1f}" stroke-width="2" stroke-linecap="round"></line>
  <rect x="{rect_x:.1f}" y="{body_y:.1f}" width="{width:.1f}" height="{height:.1f}" rx="1" fill="{color}"></rect>
  <text x="{x:.1f}" y="286" class="overview-x-label" text-anchor="middle">{label}</text>
</g>""".format(
                stock_name=escape(str(row["stock_name"])),
                change=escape(_format_overview_percent(row["close_pct"])),
                volume=escape(_format_optional_number(row["volume"], 0)),
                title=escape(title),
                x=center_x,
                high_y=high_y,
                low_y=low_y,
                rect_x=center_x - candle_width / 2,
                body_y=body_y,
                width=candle_width,
                height=body_height,
                color=color,
                label=label_tspans,
            )
        )

    chips = "".join(
        "<span>{stock} {change} / {volume}口</span>".format(
            stock=escape(str(row["stock_name"])),
            change=escape(_format_overview_percent(row["close_pct"])),
            volume=escape(_format_optional_number(row["volume"], 0)),
        )
        for row in chart_rows[:3]
    )

    return """<div class="overview-chart-toolbar" style="min-width: {width}px;">
  <div class="overview-legend"><span class="up">上漲</span><span class="down">下跌</span><span>Y 軸：相對昨收漲跌幅</span></div>
  <div class="overview-chips">{chips}</div>
</div>
<svg class="overview-chart" role="img" aria-label="成交口數Top50股票期貨今日速覽" viewBox="0 0 {width} {height}" width="{width}" height="{height}" style="min-width: {width}px;">
  <rect class="overview-bg" x="0" y="0" width="{width}" height="{height}"></rect>
  {ticks}
  <line x1="{plot_left}" x2="{plot_right}" y1="{zero_y:.1f}" y2="{zero_y:.1f}" class="overview-axis-zero"></line>
  {candles}
</svg>""".format(
        width=width,
        height=height,
        chips=chips,
        ticks="\n  ".join(tick_lines),
        plot_left=plot_left,
        plot_right=plot_right,
        zero_y=y_position(0),
        candles="\n  ".join(candles),
    )


def _today_overview_rows(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    chart_rows = []
    for row in rows:
        open_price = _float_or_none(row.get("open"))
        high = _float_or_none(row.get("high"))
        low = _float_or_none(row.get("low"))
        close = _float_or_none(row.get("close"))
        spread = _float_or_none(row.get("spread"))
        spread_per = _float_or_none(row.get("spread_per"))
        volume = _float_or_none(row.get("volume"))
        stock_name = str(row.get("stock_name") or row.get("stock_id") or "").strip()
        if not stock_name or None in (open_price, high, low, close, spread, spread_per, volume):
            continue
        previous_close = close - spread
        if previous_close <= 0:
            continue
        chart_row = dict(row)
        chart_row.update(
            {
                "stock_name": stock_name,
                "volume": volume,
                "open_pct": (open_price - previous_close) / previous_close * 100,
                "high_pct": (high - previous_close) / previous_close * 100,
                "low_pct": (low - previous_close) / previous_close * 100,
                "close_pct": spread_per,
            }
        )
        chart_rows.append(chart_row)
    chart_rows.sort(key=lambda row: row["volume"], reverse=True)
    return chart_rows[:TODAY_OVERVIEW_TOP_N]


def _float_or_none(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number


def _format_overview_percent(value: object) -> str:
    number = _float_or_none(value)
    if number is None:
        return "--"
    return "{sign}{value:.2f}%".format(sign="+" if number > 0 else "", value=number)


def _dashboard_script() -> str:
    return """<script>
(function () {
  const REALTIME_REFRESH_MS = 30000;
  const TAIPEI_TIME_ZONE = "Asia/Taipei";
  const CLOSING_REFRESH_MINUTES = [13 * 60 + 30, 13 * 60 + 45, 14 * 60];
  const TODAY_OVERVIEW_TOP_N = 50;
  const INTRADAY_OPEN_MINUTES = 8 * 60 + 45;
  const INTRADAY_CLOSE_MINUTES = 13 * 60 + 45;
  const INTRADAY_BUCKET_MINUTES = 15;
  const INTRADAY_TOP_N = 50;
  const INTRADAY_CHART_TOP_N = 24;
  const OUT_OF_TOP_RANK = INTRADAY_TOP_N + 1;
  const BUTTERFLY_SIDE_LIMIT = 15;
  const INTRADAY_STORAGE_PREFIX = "stock-futures-intraday-history-v1";
  const completedClosingRefreshes = new Set();
  let asOfPinned = new URLSearchParams(window.location.search).has("as_of");
  const intradayChartState = {
    initialized: false,
    idsKey: "",
    domainKey: "",
    domain: null,
    scaleKey: "",
    frameIndex: null,
    playing: false,
    playbackTimer: null,
    lastStep: null,
    lastPoints: new Map(),
    animations: new Map()
  };
  const poolTableHead = `
    <thead>
      <tr>
        <th>#</th>
        <th>股票</th>
        <th>收盤價</th>
        <th>漲跌幅%</th>
        <th>ATR20%</th>
        <th>近 5 日排名 <span class="th-help" title="依每日股期成交口數排序，紅色為前 10、黃色為前 25" aria-label="近 5 日排名說明">!</span></th>
        <th>盤中變化 <span class="th-help" title="上一截點排名減目前排名；正值代表排名往前" aria-label="盤中變化說明">!</span></th>
        <th>開盤累積 <span class="th-help" title="08:45 第一個截點排名減目前排名；正值代表從開盤累積轉強" aria-label="開盤累積說明">!</span></th>
        <th>狀態 <span class="th-help" title="依開盤第一個截點到最新截點的累積排名變化分類" aria-label="狀態說明">!</span></th>
      </tr>
    </thead>`;
  const newEntryTableHead = `
    <thead>
      <tr>
        <th>#</th>
        <th>股票</th>
        <th>類型</th>
        <th>本日排名</th>
        <th>前日排名</th>
        <th>本日口數</th>
        <th>前日口數</th>
        <th>收盤價</th>
        <th>合約月份</th>
      </tr>
    </thead>`;
  const watchlistTableHead = `
    <thead>
      <tr>
        <th>#</th>
        <th>股票</th>
        <th>漲跌</th>
        <th>漲跌%</th>
        <th>成交口數</th>
        <th>開盤</th>
        <th>最高</th>
        <th>最低</th>
        <th>收盤</th>
        <th>類型</th>
        <th>合約月份</th>
        <th>日期</th>
      </tr>
    </thead>`;
  const tableSignatures = {
    pool: new Map(),
    activePool: new Map(),
    newEntry: new Map(),
    watchlist: new Map()
  };
  const poolTabSubtitles = {
    small: "小型股票期貨，價格 500~5000，流動與波動同時達標",
    large: "大型股票期貨，價格 200 以下，口數與 ATR 條件達標",
    new: "前一交易日 50 名外，最新交易日進入口數 Top 50"
  };
  const watchlistTabSubtitles = {
    all: "全部股票期貨產品，即時報價一律取近月契約",
    regular: "只顯示大型股票期貨標的",
    small: "只顯示小型股票期貨標的"
  };
  let currentWatchlistRows = [];
  let currentWatchlistTab = "all";

  function applyTheme(theme) {
    const nextTheme = theme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = nextTheme;
    const toggle = document.getElementById("theme-toggle");
    if (toggle) {
      const switchesTo = nextTheme === "dark" ? "淺色" : "深色";
      toggle.textContent = nextTheme === "dark" ? "☼" : "☾";
      toggle.setAttribute("aria-label", `切換為${switchesTo}主題`);
      toggle.setAttribute("title", `切換為${switchesTo}主題`);
    }
    localStorage.setItem("stock-futures-theme", nextTheme);
  }

  function initThemeToggle() {
    const savedTheme = localStorage.getItem("stock-futures-theme");
    const preferredTheme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    applyTheme(savedTheme || preferredTheme);
    const toggle = document.getElementById("theme-toggle");
    if (toggle) {
      toggle.addEventListener("click", () => {
        applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
      });
    }
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value == null || value === "" ? "-" : String(value);
  }

  function taipeiParts(now) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: TAIPEI_TIME_ZONE,
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    }).formatToParts(now);
    return Object.fromEntries(parts.map((part) => [part.type, part.value]));
  }

  function taipeiDateString(now = new Date()) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: TAIPEI_TIME_ZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    }).formatToParts(now);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  }

  function isTradingSession(now = new Date()) {
    const parts = taipeiParts(now);
    if (parts.weekday === "Sat" || parts.weekday === "Sun") return false;
    const hour = Number(parts.hour);
    const minute = Number(parts.minute);
    const minutes = hour * 60 + minute;
    return minutes >= 8 * 60 + 45 && minutes <= 13 * 60 + 45;
  }

  function updateRealtimeStatus(isLive, payload) {
    const source = payload && payload.source ? payload.source : {};
    if (source.stale_cache_fallback) {
      setText("realtime-status", "外部資料暫停 · 暫用快取");
      return;
    }
    if (source.historical_mode) {
      setText("realtime-status", "歷史回溯模式｜不合併即時報價");
      return;
    }
    if (source.final_ready === false && source.snapshot_stage === "intraday_fallback") {
      setText("realtime-status", "盤後日資料尚未完成 · 暫用盤中快照");
      return;
    }
    if (source.final_ready === false && source.snapshot_stage === "final_pending") {
      setText("realtime-status", "盤後日資料尚未完成 · 等待完整日資料");
      return;
    }
    if (source.final_ready === true && source.snapshot_stage === "final") {
      setText("realtime-status", "盤後正式日資料");
      return;
    }
    const quoteRows = source.fugle_quote_rows == null ? "-" : source.fugle_quote_rows;
    const text = isLive
      ? `即時排序中 · ${quoteRows} 筆 · 30 秒更新`
      : "非交易時段 · 共用快照";
    setText("realtime-status", text);
  }

  function setSessionStatus(status, text) {
    const el = document.getElementById("session-status");
    const normalized = status === "online" ? "online" : "offline";
    const accessibleText = text || (normalized === "online" ? "交易時段" : "非交易時段");
    if (el) {
      el.className = `connection-status is-${normalized}`;
      el.setAttribute("aria-label", accessibleText);
      el.setAttribute("title", accessibleText);
    }
  }

  function updateSessionStatus(isLive) {
    setSessionStatus(isLive ? "online" : "offline", isLive ? "交易時段" : "非交易時段");
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatNumber(value, digits) {
    if (value === null || value === undefined || value === "") return "-";
    const number = Number(value || 0);
    return number.toLocaleString("zh-TW", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    });
  }

  function displayFuturesId(row) {
    return row.finmind_futures_id || row.futures_id || "-";
  }

  function displayInlineFuturesId(row) {
    const code = displayFuturesId(row);
    if (!code || code === "-") return "";
    return String(code)
      .replace(/，/g, ",")
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean)
      .join("/");
  }

  function stockMeta(row) {
    const stockId = row.stock_id || "-";
    const futuresId = displayInlineFuturesId(row);
    return futuresId ? `${stockId} / ${futuresId}` : stockId;
  }

  function formatPercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value || "-");
    return number.toLocaleString("zh-TW", {
      minimumFractionDigits: number % 1 === 0 ? 0 : 1,
      maximumFractionDigits: 1
    });
  }

  function formatPercentValue(value) {
    if (value === null || value === undefined || value === "") return "-";
    return `${formatNumber(value, 2)}%`;
  }

  function formatOverviewPercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
  }

  function formatSignedPercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "-";
    return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
  }

  function changeClass(value) {
    if (value === null || value === undefined || value === "") return "";
    const number = Number(value);
    if (number > 0) return "positive";
    if (number < 0) return "negative";
    return "";
  }

  function signatureValue(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "number" && Number.isFinite(value)) return value.toFixed(4);
    return String(value);
  }

  function rowSignature(values) {
    return values.map(signatureValue).join("|");
  }

  function rowKey(row) {
    return [
      row.stock_id || "",
      displayFuturesId(row),
      row.contract_type || row.contract_type_label || "",
      row.contract_date || ""
    ].map(signatureValue).join("|");
  }

  function rowAttributes(tableName, key, signature) {
    const previousSignature = tableSignatures[tableName].get(key);
    const updatedClass = previousSignature !== undefined && previousSignature !== signature ? ' class="row-updated"' : "";
    return `${updatedClass} data-row-key="${escapeHtml(key)}" data-row-signature="${escapeHtml(signature)}"`;
  }

  function numberOrNull(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(String(value).replace(/,/g, "").replace("%", ""));
    return Number.isFinite(number) ? number : null;
  }

  function minuteLabel(minutes) {
    const hour = Math.floor(minutes / 60);
    const minute = minutes % 60;
    return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
  }

  function labelToMinutes(label) {
    const parts = String(label || "").split(":").map((value) => Number(value));
    if (parts.length !== 2 || parts.some((value) => !Number.isFinite(value))) return INTRADAY_OPEN_MINUTES;
    return parts[0] * 60 + parts[1];
  }

  function taipeiMinuteOfDay(now = new Date()) {
    const parts = taipeiParts(now);
    return Number(parts.hour) * 60 + Number(parts.minute);
  }

  function currentIntradayCutoff(now = new Date()) {
    const parts = taipeiParts(now);
    if (parts.weekday === "Sat" || parts.weekday === "Sun") return null;
    const minutes = taipeiMinuteOfDay(now);
    if (minutes < INTRADAY_OPEN_MINUTES) return null;
    const clamped = Math.min(minutes, INTRADAY_CLOSE_MINUTES);
    const bucket = Math.floor((clamped - INTRADAY_OPEN_MINUTES) / INTRADAY_BUCKET_MINUTES);
    return minuteLabel(INTRADAY_OPEN_MINUTES + bucket * INTRADAY_BUCKET_MINUTES);
  }

  function intradayHistoryKey(asOfDate) {
    return `${INTRADAY_STORAGE_PREFIX}:${asOfDate || taipeiDateString()}`;
  }

  function normalizeIntradayHistory(history, fallbackAsOfDate) {
    const asOfDate = (history && history.as_of_date) || fallbackAsOfDate || taipeiDateString();
    const snapshots = history && Array.isArray(history.snapshots) ? history.snapshots : [];
    return {
      ...(history || {}),
      as_of_date: asOfDate,
      snapshots: snapshots
        .filter((snapshot) => snapshot && snapshot.cutoff && Array.isArray(snapshot.rows))
        .sort((a, b) => labelToMinutes(a.cutoff) - labelToMinutes(b.cutoff))
    };
  }

  function hasIntradaySnapshots(history) {
    return !!(history && Array.isArray(history.snapshots) && history.snapshots.length);
  }

  function loadIntradayHistory(asOfDate) {
    try {
      const raw = localStorage.getItem(intradayHistoryKey(asOfDate));
      if (!raw) return { as_of_date: asOfDate, snapshots: [] };
      const parsed = JSON.parse(raw);
      return normalizeIntradayHistory(parsed, asOfDate);
    } catch (error) {
      return { as_of_date: asOfDate, snapshots: [] };
    }
  }

  function saveIntradayHistory(history) {
    try {
      localStorage.setItem(intradayHistoryKey(history.as_of_date), JSON.stringify(history));
    } catch (error) {
      // Best-effort browser-side history only.
    }
  }

  function useIntradayHistory(history, fallbackAsOfDate) {
    const normalized = normalizeIntradayHistory(history, fallbackAsOfDate);
    if (hasIntradaySnapshots(normalized)) {
      saveIntradayHistory(normalized);
      renderIntradayPanels(normalized);
    }
    return normalized;
  }

  function buildIntradayTrajectoryUrl() {
    const params = new URLSearchParams();
    if (asOfPinned) params.set("as_of", currentAsOfDate());
    const query = params.toString();
    return query ? `/api/intraday-trajectory?${query}` : "/api/intraday-trajectory";
  }

  async function loadCachedIntradayTrajectory() {
    const asOf = asOfPinned ? currentAsOfDate() : "";
    try {
      const response = await fetch(buildIntradayTrajectoryUrl());
      const payload = await response.json();
      if (!response.ok || payload.error) throw new Error(payload.error || "排名軌跡快取讀取失敗");
      const history = useIntradayHistory(payload, payload.as_of_date || asOf || taipeiDateString());
      if (hasIntradaySnapshots(history)) return history;
    } catch (error) {
      // Server-side trajectory cache is best-effort; keep any browser-side history.
    }
    const localHistory = loadIntradayHistory(asOf || taipeiDateString());
    if (hasIntradaySnapshots(localHistory)) renderIntradayPanels(localHistory);
    return localHistory;
  }

  function watchlistSnapshotRows(rows) {
    return (rows || [])
      .map((row) => {
        const volume = numberOrNull(row.volume);
        const spreadPer = numberOrNull(row.spread_per);
        const close = numberOrNull(row.close);
        const stockId = String(row.stock_id || "").trim();
        if (!stockId || volume === null || spreadPer === null) return null;
        return {
          stock_id: stockId,
          stock_name: String(row.stock_name || stockId),
          futures_id: displayFuturesId(row),
          contract_type_label: row.contract_type_label || "",
          close,
          spread_per: spreadPer,
          volume
        };
      })
      .filter(Boolean)
      .sort((a, b) => b.volume - a.volume || a.stock_id.localeCompare(b.stock_id))
      .slice(0, Math.max(INTRADAY_TOP_N, INTRADAY_CHART_TOP_N))
      .map((row, index) => ({ ...row, rank: index + 1 }));
  }

  function butterflySourceRows(rows) {
    return (rows || [])
      .map((row) => {
        const volume = numberOrNull(row.volume);
        const spreadPer = numberOrNull(row.spread_per);
        const stockId = String(row.stock_id || "").trim();
        if (!stockId || volume === null || spreadPer === null) return null;
        return {
          ...row,
          stock_id: stockId,
          stock_name: String(row.stock_name || stockId),
          volume,
          spread_per: spreadPer
        };
      })
      .filter(Boolean)
      .sort((a, b) => b.volume - a.volume || a.stock_id.localeCompare(b.stock_id))
      .slice(0, INTRADAY_TOP_N);
  }

  function butterflyWingRows(rows, direction) {
    const source = butterflySourceRows(rows);
    if (direction === "up") {
      return source
        .filter((row) => row.spread_per > 0)
        .sort((a, b) => b.spread_per - a.spread_per || b.volume - a.volume)
        .slice(0, BUTTERFLY_SIDE_LIMIT);
    }
    return source
      .filter((row) => row.spread_per < 0)
      .sort((a, b) => a.spread_per - b.spread_per || b.volume - a.volume)
      .slice(0, BUTTERFLY_SIDE_LIMIT);
  }

  function renderButterflyWing(rows, direction, maxVolume) {
    if (!rows.length) {
      return '<div class="butterfly-empty">尚無符合標的</div>';
    }
    return rows.map((row) => {
      const width = Math.max(3, row.volume / maxVolume * 100);
      const label = `<div class="butterfly-label"><strong>${escapeHtml(row.stock_name)}</strong><span>${formatSignedPercent(row.spread_per)}</span></div>`;
      const bar = `<div class="butterfly-track"><div class="butterfly-bar ${direction}" style="width:${width.toFixed(1)}%" title="${escapeHtml(row.stock_name)}｜${formatSignedPercent(row.spread_per)}｜成交口數 ${formatNumber(row.volume, 0)}"><span class="butterfly-volume">${formatNumber(row.volume, 0)}</span></div></div>`;
      return `<div class="butterfly-row ${direction}" data-butterfly-key="${escapeHtml(row.stock_id)}">${direction === "up" ? label + bar : bar + label}</div>`;
    }).join("");
  }

  function renderButterflyChart(rows) {
    const upWing = document.getElementById("butterfly-up-wing");
    const downWing = document.getElementById("butterfly-down-wing");
    if (!upWing || !downWing) return;
    const source = butterflySourceRows(rows);
    const upRows = butterflyWingRows(rows, "up");
    const downRows = butterflyWingRows(rows, "down");
    const maxVolume = Math.max(...source.map((row) => row.volume), 1);
    upWing.innerHTML = renderButterflyWing(upRows, "up", maxVolume);
    downWing.innerHTML = renderButterflyWing(downRows, "down", maxVolume);
  }

  function recordIntradaySnapshot(payload) {
    const source = payload && payload.source ? payload.source : {};
    const effectiveAsOf = source.effective_as_of_date || (payload && payload.as_of_date) || taipeiDateString();
    if (source.historical_mode || effectiveAsOf !== taipeiDateString()) {
      return loadIntradayHistory(effectiveAsOf);
    }
    const cutoff = currentIntradayCutoff();
    const rows = watchlistSnapshotRows(payload && payload.watchlist_rows);
    if (!cutoff || !rows.length) return loadIntradayHistory(effectiveAsOf);

    const history = loadIntradayHistory(effectiveAsOf);
    history.as_of_date = effectiveAsOf;
    history.snapshots = history.snapshots.filter((snapshot) => snapshot.cutoff !== cutoff);
    history.snapshots.push({
      cutoff,
      captured_at: new Date().toISOString(),
      rows
    });
    history.snapshots = history.snapshots
      .sort((a, b) => labelToMinutes(a.cutoff) - labelToMinutes(b.cutoff))
      .slice(-32);
    saveIntradayHistory(history);
    return history;
  }

  function snapshotMap(snapshot) {
    return new Map((snapshot && snapshot.rows ? snapshot.rows : []).map((row) => [row.stock_id, row]));
  }

  function deltaText(delta) {
    if (delta > 0) return `▲ ${delta}`;
    if (delta < 0) return `▼ ${Math.abs(delta)}`;
    return "－";
  }

  function rankClass(delta) {
    if (delta > 0) return "positive";
    if (delta < 0) return "negative";
    return "";
  }

  function rankPillClass(rank) {
    if (rank <= 10) return "rank-pill hot";
    if (rank <= 25) return "rank-pill warm";
    return "rank-pill";
  }

  function fallbackRankValues(row) {
    let bestRank = numberOrNull(row && row.best_volume_rank_5d);
    let worstRank = numberOrNull(row && row.worst_volume_rank_5d);
    if (bestRank === null && worstRank === null) return ["", "", "", "", ""];
    if (bestRank === null) bestRank = worstRank;
    if (worstRank === null) worstRank = bestRank;
    if (bestRank === null || worstRank === null) return ["", "", "", "", ""];
    if (bestRank === worstRank) return Array(5).fill(bestRank);
    const step = (worstRank - bestRank) / 4;
    return Array.from({ length: 5 }, (_, index) => Math.round(worstRank - step * index));
  }

  function rankPill(value) {
    const rank = numberOrNull(value);
    if (rank === null) return '<span class="rank-pill empty">-</span>';
    return `<span class="${rankPillClass(rank)}">${formatNumber(rank, 0)}</span>`;
  }

  function rankValues(row) {
    const raw = row && row.volume_rank_5d;
    let values = [];
    if (Array.isArray(raw)) {
      values = raw;
    } else if (typeof raw === "string" && raw.trim()) {
      const text = raw.trim();
      if (text.startsWith("[") && text.endsWith("]")) {
        try {
          const parsed = JSON.parse(text);
          values = Array.isArray(parsed) ? parsed : [];
        } catch (error) {
          values = [];
        }
      } else {
        values = text.split(",");
      }
    }
    return values
      .map((value) => numberOrNull(value))
      .filter((value) => value !== null)
      .slice(-5);
  }

  function renderRankStrip(row) {
    const ranks = rankValues(row);
    const values = ranks.length >= 5 ? ranks.slice(-5) : fallbackRankValues(row);
    return `<div class="rank-strip"><div class="rank-pills">${values.map(rankPill).join("")}</div></div>`;
  }

  function poolMovement(row) {
    const stockId = String(row && row.stock_id || "").trim();
    if (!stockId || !hasIntradaySnapshots(currentIntradayHistory)) {
      return null;
    }
    const latestIndex = currentIntradayHistory.snapshots.length - 1;
    return movementAt(currentIntradayHistory, stockId, latestIndex);
  }

  function renderRankDelta(movement, mode) {
    if (!movement) return '<span class="rank-delta is-empty">－</span>';
    const delta = mode === "cumulative" ? movement.deltaOpen : movement.delta;
    const fromRank = mode === "cumulative" ? movement.openRankLabel : movement.prevRankLabel;
    const label = mode === "cumulative" ? "08:45" : "前次";
    const title = `${movement.cutoff} 截點：${label} ${fromRank}，目前 ${movement.currentRankLabel}`;
    return `<span class="rank-delta ${rankClass(delta)}" title="${escapeHtml(title)}">${escapeHtml(deltaText(delta))}</span>`;
  }

  function rankDisplay(value) {
    const rank = numberOrNull(value);
    if (rank === null || rank > INTRADAY_TOP_N || rank >= 999) return "未進榜";
    return formatNumber(rank, 0);
  }

  function renderRankStatus(movement) {
    if (!movement) return '<span class="rank-status">等待</span>';
    if (!movement.openInTop && movement.currentInTop) {
      return '<span class="rank-status is-new">新進</span>';
    }
    if (movement.deltaOpen >= 8) {
      return '<span class="rank-status is-surge">急升</span>';
    }
    if (movement.deltaOpen <= -8) {
      return '<span class="rank-status is-fade">轉弱</span>';
    }
    return '<span class="rank-status">穩定</span>';
  }

  function renderIntradayChange(row) {
    const movement = poolMovement(row);
    if (!movement && hasIntradaySnapshots(currentIntradayHistory)) {
      return '<span class="rank-delta is-empty">未進榜</span>';
    }
    return renderRankDelta(movement, "interval");
  }

  function renderIntradayOpenChange(row) {
    const movement = poolMovement(row);
    if (!movement && hasIntradaySnapshots(currentIntradayHistory)) {
      return '<span class="rank-delta is-empty">未進榜</span>';
    }
    return renderRankDelta(movement, "cumulative");
  }

  function renderIntradayStatus(row) {
    const movement = poolMovement(row);
    if (!movement && hasIntradaySnapshots(currentIntradayHistory)) {
      return '<span class="rank-status">未進榜</span>';
    }
    if (!movement && currentDashboardSource && currentDashboardSource.historical_mode) {
      return '<span class="rank-status is-rebuild-needed">尚未重建</span>';
    }
    return renderRankStatus(movement);
  }

  function movementBetween(history, stockId, fromIndex, toIndex, options = {}) {
    if (!history || !history.snapshots || !history.snapshots[toIndex]) return null;
    const snapshots = history.snapshots;
    const currentSnapshot = snapshots[toIndex];
    const current = snapshotMap(currentSnapshot).get(stockId);
    if (!current && !options.allowMissingCurrent) return null;
    const firstSnapshot = snapshots[0];
    const previousSnapshot = snapshots[Math.max(0, fromIndex)];
    const secondSnapshot = snapshots[Math.min(1, snapshots.length - 1)];
    const first = snapshotMap(firstSnapshot).get(stockId);
    const previous = snapshotMap(previousSnapshot).get(stockId);
    const second = snapshotMap(secondSnapshot).get(stockId);
    const base = current || previous || first;
    if (!base) return null;
    const openInTop = !!first;
    const prevInTop = !!previous;
    const currentInTop = !!current;
    const openRank = openInTop ? first.rank : OUT_OF_TOP_RANK;
    const prevRank = prevInTop ? previous.rank : OUT_OF_TOP_RANK;
    const currentRank = currentInTop ? current.rank : OUT_OF_TOP_RANK;
    const firstVolume = first ? first.volume : 0;
    const secondVolume = second ? second.volume : firstVolume;
    const currentVolume = current ? current.volume : previous ? previous.volume : firstVolume;
    const openingSegmentVolume = Math.max(1, secondVolume - firstVolume || currentVolume || 1);
    const elapsed = Math.max(1, Math.round((labelToMinutes(currentSnapshot.cutoff) - labelToMinutes(firstSnapshot.cutoff)) / INTRADAY_BUCKET_MINUTES));
    const expectedVolume = openingSegmentVolume * elapsed;
    const volumeRate = expectedVolume > 0 ? (currentVolume - expectedVolume) / expectedVolume * 100 : 0;
    return {
      ...base,
      cutoff: currentSnapshot.cutoff,
      fromCutoff: previousSnapshot.cutoff,
      openRank,
      prevRank,
      currentRank,
      openInTop,
      prevInTop,
      currentInTop,
      openRankLabel: openInTop ? rankDisplay(openRank) : "未進榜",
      prevRankLabel: prevInTop ? rankDisplay(prevRank) : "未進榜",
      currentRankLabel: currentInTop ? rankDisplay(currentRank) : "未進榜",
      delta: prevRank - currentRank,
      deltaOpen: openRank - currentRank,
      volume: currentVolume,
      volumeRate
    };
  }

  function movementAt(history, stockId, index) {
    return movementBetween(history, stockId, Math.max(0, index - 1), index);
  }

  function latestMovementRows(history) {
    if (!history || !history.snapshots || !history.snapshots.length) return [];
    const latestIndex = history.snapshots.length - 1;
    return history.snapshots[latestIndex].rows
      .map((row) => movementAt(history, row.stock_id, latestIndex))
      .filter(Boolean)
      .sort((a, b) => a.currentRank - b.currentRank);
  }

  function intervalMovementRows(history) {
    if (!history || !history.snapshots || history.snapshots.length < 2) return [];
    const rows = [];
    for (let index = 1; index < history.snapshots.length; index += 1) {
      const previousRows = history.snapshots[index - 1].rows || [];
      const currentRows = history.snapshots[index].rows || [];
      const stockIds = new Set([
        ...previousRows.map((row) => row.stock_id),
        ...currentRows.map((row) => row.stock_id)
      ]);
      stockIds.forEach((stockId) => {
        const movement = movementBetween(history, stockId, index - 1, index, { allowMissingCurrent: true });
        if (movement) rows.push(movement);
      });
    }
    return rows;
  }

  function strongestByStock(rows, scoreFn, betterFn) {
    const best = new Map();
    rows.forEach((row) => {
      const key = row.stock_id;
      const score = scoreFn(row);
      const current = best.get(key);
      if (!current || betterFn(score, current.score)) {
        best.set(key, { row, score });
      }
    });
    return Array.from(best.values()).map((entry) => entry.row);
  }

  function renderIntradayEmpty(message) {
    const chart = document.getElementById("intraday-trajectory-chart");
    const movers = document.getElementById("intraday-movers");
    clearTrajectoryPlayback();
    setText("intraday-mover-time", "等待盤中截點");
    setText("intraday-trajectory-time", "08:45 起，每 15 分鐘截點");
    if (chart) {
      chart.innerHTML = `<foreignObject x="0" y="0" width="820" height="450"><div xmlns="http://www.w3.org/1999/xhtml" class="trajectory-empty">${escapeHtml(message)}</div></foreignObject>`;
      intradayChartState.initialized = false;
      intradayChartState.idsKey = "";
      intradayChartState.scaleKey = "";
      intradayChartState.frameIndex = null;
      intradayChartState.lastPoints.clear();
    }
    if (movers) movers.innerHTML = "";
  }

  function renderMoverGroup(title, rows, mode) {
    if (!rows.length) {
      return `<div class="mover-group">
        <div class="mover-group-title"><span>${escapeHtml(title)}</span><span>0</span></div>
        <div class="mover-row"><div class="mover-name"><strong>-</strong><span>尚無符合標的</span></div><div></div><div></div></div>
      </div>`;
    }
    const maxVolume = Math.max(...rows.map((row) => row.volume), 1);
    return `<div class="mover-group">
      <div class="mover-group-title"><span>${escapeHtml(title)}</span><span>${rows.length}</span></div>
      ${rows.map((row) => {
        const delta = mode === "cumulative" ? row.deltaOpen : row.delta;
        const fromRank = mode === "cumulative" ? row.openRankLabel : row.prevRankLabel;
        const period = row.fromCutoff && row.cutoff ? `｜${row.fromCutoff}→${row.cutoff}` : "";
        const bar = Math.max(7, row.volume / maxVolume * 100);
        return `<div class="mover-row">
          <div class="mover-name"><strong>${escapeHtml(row.stock_name)}</strong><span>${fromRank} → ${row.currentRankLabel}${period} / ${formatNumber(row.volume, 0)} 口</span></div>
          <div class="rank-now"><strong>${row.currentRankLabel}</strong><span>目前</span></div>
          <div class="delta ${rankClass(delta)}">${deltaText(delta)}</div>
          <div class="volume-bar"><i style="width:${bar.toFixed(1)}%"></i></div>
        </div>`;
      }).join("")}
    </div>`;
  }

  function renderIntradayMovers(history) {
    const movers = document.getElementById("intraday-movers");
    const rows = latestMovementRows(history);
    if (!movers || rows.length < 1) return;
    const snapshots = history.snapshots || [];
    const first = snapshots[0];
    const latest = snapshots[snapshots.length - 1];
    const intervalRows = intervalMovementRows(history);
    const surgeRows = strongestByStock(
      intervalRows.filter((row) => row.prevInTop && row.currentInTop && row.delta >= 8),
      (row) => row.delta,
      (next, current) => next > current
    ).sort((a, b) => b.delta - a.delta).slice(0, 4);
    const fadeRows = strongestByStock(
      intervalRows.filter((row) => row.prevInTop && row.delta <= -8),
      (row) => row.delta,
      (next, current) => next < current
    ).sort((a, b) => a.delta - b.delta).slice(0, 4);
    const isNewTopN = (row) => !row.prevInTop && row.currentInTop;
    const moverGroups = [
      renderMoverGroup("急升", surgeRows, "interval"),
      renderMoverGroup("轉弱", fadeRows, "interval"),
      renderMoverGroup("新進 TopN", rows.filter(isNewTopN).sort((a, b) => a.currentRank - b.currentRank).slice(0, 4), "interval"),
      renderMoverGroup(
        "08:45起累積變化",
        rows.filter((row) => row.openInTop && row.currentInTop).sort((a, b) => Math.abs(b.deltaOpen) - Math.abs(a.deltaOpen)).slice(0, 5),
        "cumulative"
      )
    ];
    movers.innerHTML = moverGroups.join("");
    if (first && latest) setText("intraday-mover-time", `${first.cutoff} → ${latest.cutoff}`);
    if (latest && snapshots[0]) setText("intraday-trajectory-time", `${snapshots[0].cutoff} → ${latest.cutoff}`);
  }

  function quadrantClass(row) {
    const priceUp = Number(row.spread_per) >= 0;
    const volumeUp = Number(row.volumeRate) >= 0;
    if (priceUp && volumeUp) return "up-up";
    if (priceUp && !volumeUp) return "up-down";
    if (!priceUp && volumeUp) return "down-up";
    return "down-down";
  }

  function quadrantText(row) {
    const label = quadrantClass(row);
    if (label === "up-up") return "價漲量漲";
    if (label === "up-down") return "價漲量縮";
    if (label === "down-up") return "價跌量漲";
    return "價跌量縮";
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function trajectoryRows(history) {
    return latestMovementRows(history)
      .filter((row) => row.currentRank <= INTRADAY_CHART_TOP_N)
      .slice(0, INTRADAY_CHART_TOP_N);
  }

  function clearTrajectoryPlayback() {
    if (intradayChartState.playbackTimer) {
      clearInterval(intradayChartState.playbackTimer);
      intradayChartState.playbackTimer = null;
    }
    intradayChartState.playing = false;
    intradayChartState.animations.forEach((frameId) => cancelAnimationFrame(frameId));
    intradayChartState.animations.clear();
    updateTrajectoryControls(currentIntradayHistory);
  }

  function trajectoryFrameMax(history) {
    return Math.max(0, (history && history.snapshots ? history.snapshots.length : 1) - 1);
  }

  function trajectoryFrameLabel(history, frameIndex) {
    const snapshots = history && history.snapshots ? history.snapshots : [];
    const snapshot = snapshots[clamp(frameIndex, 0, Math.max(0, snapshots.length - 1))];
    return snapshot && snapshot.cutoff ? snapshot.cutoff : "--:--";
  }

  function trajectoryDomainFor(history, ids) {
    const domainKey = history && history.as_of_date ? String(history.as_of_date) : "";
    if (!intradayChartState.domain || intradayChartState.domainKey !== domainKey) {
      intradayChartState.domain = { xMax: 10, yMax: 120 };
      intradayChartState.domainKey = domainKey;
    }
    const idSet = new Set(ids || []);
    let priceMax = intradayChartState.domain.xMax;
    let volumeMax = intradayChartState.domain.yMax;
    (history.snapshots || []).forEach((snapshot, index) => {
      (snapshot.rows || []).forEach((row) => {
        if (!idSet.has(row.stock_id)) return;
        const movement = movementAt(history, row.stock_id, index);
        if (!movement) return;
        const volumeRate = Math.abs(Number(movement.volumeRate) || 0);
        const spreadPer = Math.abs(Number(movement.spread_per) || 0);
        if (Number.isFinite(spreadPer)) priceMax = Math.max(priceMax, Math.min(15, spreadPer * 1.15));
        if (Number.isFinite(volumeRate)) volumeMax = Math.max(volumeMax, Math.min(260, volumeRate * 1.15));
      });
    });
    intradayChartState.domain = {
      xMax: Math.max(4, Math.ceil(priceMax)),
      yMax: Math.max(20, Math.ceil(volumeMax / 10) * 10)
    };
    return intradayChartState.domain;
  }

  function trajectoryScale(history, ids) {
    const width = 820;
    const height = 450;
    const left = 58;
    const right = 18;
    const top = 20;
    const bottom = 44;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const domain = trajectoryDomainFor(history, ids);
    const xMax = domain.xMax;
    const yMax = domain.yMax;
    const x = (value) => left + ((clamp(value, -xMax, xMax) + xMax) / (xMax * 2)) * plotWidth;
    const y = (value) => top + ((yMax - clamp(value, -yMax, yMax)) / (yMax * 2)) * plotHeight;
    return {
      width,
      height,
      left,
      top,
      xMax,
      yMax,
      x,
      y,
      xMin: left,
      xRight: left + plotWidth,
      yTop: top,
      yBottom: top + plotHeight,
      zeroX: x(0),
      zeroY: y(0),
      key: `${xMax}:${yMax}`
    };
  }

  function trajectoryPoint(row, scale) {
    const cx = clamp(scale.x(row.spread_per), scale.xMin + 10, scale.xRight - 10);
    const cy = clamp(scale.y(row.volumeRate), scale.yTop + 12, scale.yBottom - 12);
    const radius = 8 + Math.min(18, Math.abs(row.deltaOpen) * 0.45);
    return {
      ...row,
      cx,
      cy,
      radius,
      labelX: clamp(cx + radius + 5, scale.xMin + 24, scale.xRight - 40),
      labelY: clamp(cy - 6, scale.yTop + 12, scale.yBottom - 8),
      cls: quadrantClass(row)
    };
  }

  function trailArrow(points) {
    if (points.length < 2) return "";
    const from = points[points.length - 2];
    const to = points[points.length - 1];
    const angle = Math.atan2(to.cy - from.cy, to.cx - from.cx);
    const size = 7;
    const wing = 0.75;
    const p1 = `${to.cx.toFixed(1)},${to.cy.toFixed(1)}`;
    const p2 = `${(to.cx - Math.cos(angle - wing) * size).toFixed(1)},${(to.cy - Math.sin(angle - wing) * size).toFixed(1)}`;
    const p3 = `${(to.cx - Math.cos(angle + wing) * size).toFixed(1)},${(to.cy - Math.sin(angle + wing) * size).toFixed(1)}`;
    return `<polygon class="motion-arrow" points="${p1} ${p2} ${p3}"></polygon>`;
  }

  function renderTrailLayer(history, ids, scale, frameIndex) {
    const idSet = new Set(ids);
    return ids.map((stockId) => {
      const points = [];
      history.snapshots.forEach((snapshot, index) => {
        if (index > frameIndex) return;
        if (!idSet.has(stockId)) return;
        const row = movementAt(history, stockId, index);
        if (row) points.push(trajectoryPoint(row, scale));
      });
      if (!points.length) return "";
      const path = points.map((point, index) => `${index ? "L" : "M"}${point.cx.toFixed(1)} ${point.cy.toFixed(1)}`).join(" ");
      const dots = points.map((point, index) => {
        const cls = index === points.length - 1 ? "trail-dot current" : "trail-dot";
        return `<circle class="${cls}" cx="${point.cx.toFixed(1)}" cy="${point.cy.toFixed(1)}" r="${index === points.length - 1 ? 3.3 : 2.5}"></circle>`;
      }).join("");
      return `<g><path class="trail" d="${path}"></path>${dots}${trailArrow(points)}</g>`;
    }).join("");
  }

  function renderTrajectoryBase(scale) {
    const chart = document.getElementById("intraday-trajectory-chart");
    if (!chart) return;
    chart.innerHTML = `
      <rect class="quad-bg down-up" x="${scale.xMin}" y="${scale.yTop}" width="${scale.zeroX - scale.xMin}" height="${scale.zeroY - scale.yTop}"></rect>
      <rect class="quad-bg up-up" x="${scale.zeroX}" y="${scale.yTop}" width="${scale.xRight - scale.zeroX}" height="${scale.zeroY - scale.yTop}"></rect>
      <rect class="quad-bg down-down" x="${scale.xMin}" y="${scale.zeroY}" width="${scale.zeroX - scale.xMin}" height="${scale.yBottom - scale.zeroY}"></rect>
      <rect class="quad-bg up-down" x="${scale.zeroX}" y="${scale.zeroY}" width="${scale.xRight - scale.zeroX}" height="${scale.yBottom - scale.zeroY}"></rect>
      <line class="axis-line" x1="${scale.xMin}" y1="${scale.zeroY}" x2="${scale.xRight}" y2="${scale.zeroY}"></line>
      <line class="axis-line" x1="${scale.zeroX}" y1="${scale.yTop}" x2="${scale.zeroX}" y2="${scale.yBottom}"></line>
      <text class="quad-label" x="${scale.xMin + 12}" y="${scale.yTop + 22}">價跌量漲</text>
      <text class="quad-label" x="${scale.zeroX + 12}" y="${scale.yTop + 22}">價漲量漲</text>
      <text class="quad-label" x="${scale.xMin + 12}" y="${scale.yBottom - 14}">價跌量縮</text>
      <text class="quad-label" x="${scale.zeroX + 12}" y="${scale.yBottom - 14}">價漲量縮</text>
      <text class="axis-label" x="${scale.xMin}" y="${scale.height - 22}">-${scale.xMax}%</text>
      <text class="axis-label" x="${scale.zeroX - 10}" y="${scale.height - 22}">0</text>
      <text class="axis-label" x="${scale.xRight - 32}" y="${scale.height - 22}">+${scale.xMax}%</text>
      <text class="axis-label" x="${scale.width / 2 - 34}" y="${scale.height - 10}">價格基準</text>
      <text class="axis-label" x="12" y="${scale.yTop + 10}">+${scale.yMax}%</text>
      <text class="axis-label" x="12" y="${scale.zeroY + 4}">量能偏離 %</text>
      <text class="axis-label" x="12" y="${scale.yBottom}">-${scale.yMax}%</text>
      <g id="intraday-trail-layer"></g>
      <g id="intraday-bubble-layer"></g>`;
    intradayChartState.initialized = true;
    intradayChartState.scaleKey = scale.key;
  }

  function renderTrajectoryFrame(history, ids, scale, frameIndex) {
    const chart = document.getElementById("intraday-trajectory-chart");
    const trailLayer = chart && chart.querySelector("#intraday-trail-layer");
    const bubbleLayer = chart && chart.querySelector("#intraday-bubble-layer");
    if (!trailLayer || !bubbleLayer) return;
    trailLayer.innerHTML = renderTrailLayer(history, ids, scale, frameIndex);
    const bubbleGroups = ids.map((stockId) => movementAt(history, stockId, frameIndex)).filter(Boolean).map((row) => {
      const point = trajectoryPoint(row, scale);
      const title = `${row.stock_name}｜${quadrantText(row)}｜排名 ${row.currentRank}｜漲跌幅 ${formatPercentValue(row.spread_per)}｜量能偏離 ${formatPercentValue(row.volumeRate)}｜累積口數 ${formatNumber(row.volume, 0)}`;
      return `<g id="intraday-bubble-${escapeHtml(row.stock_id)}">
        <title>${escapeHtml(title)}</title>
        <circle class="bubble ${point.cls}" cx="${point.cx.toFixed(1)}" cy="${point.cy.toFixed(1)}" r="${point.radius.toFixed(1)}"></circle>
        <text class="bubble-rank" x="${point.cx.toFixed(1)}" y="${point.cy.toFixed(1)}">${point.currentRank}</text>
        <text class="bubble-text" x="${point.labelX.toFixed(1)}" y="${point.labelY.toFixed(1)}">${escapeHtml(row.stock_name)}</text>
      </g>`;
    }).join("");
    bubbleLayer.innerHTML = bubbleGroups;
  }

  function updateTrajectoryControls(history) {
    const playButton = document.getElementById("intraday-trajectory-play");
    const replayButton = document.getElementById("intraday-trajectory-replay");
    const slider = document.getElementById("intraday-trajectory-slider");
    const frameLabel = document.getElementById("intraday-trajectory-frame-label");
    if (!slider || !frameLabel || !playButton || !replayButton) return;
    const maxFrame = trajectoryFrameMax(history);
    const frameIndex = clamp(intradayChartState.frameIndex ?? maxFrame, 0, maxFrame);
    slider.max = String(maxFrame);
    slider.value = String(frameIndex);
    slider.disabled = maxFrame < 1;
    playButton.disabled = maxFrame < 1;
    replayButton.disabled = maxFrame < 1;
    playButton.textContent = intradayChartState.playing ? "暫停" : "播放";
    frameLabel.textContent = trajectoryFrameLabel(history, frameIndex);
    playButton.onclick = toggleTrajectoryPlayback;
    replayButton.onclick = replayTrajectory;
    slider.oninput = () => {
      stopTrajectoryPlayback(false);
      intradayChartState.frameIndex = Number(slider.value) || 0;
      drawTrajectoryFromState(currentIntradayHistory);
    };
  }

  function stopTrajectoryPlayback(updateControls = true) {
    if (intradayChartState.playbackTimer) {
      clearInterval(intradayChartState.playbackTimer);
      intradayChartState.playbackTimer = null;
    }
    intradayChartState.playing = false;
    if (updateControls) updateTrajectoryControls(currentIntradayHistory);
  }

  function startTrajectoryPlayback() {
    if (intradayChartState.playbackTimer) return;
    intradayChartState.playing = true;
    intradayChartState.playbackTimer = setInterval(() => {
      const history = currentIntradayHistory;
      if (!hasIntradaySnapshots(history)) {
        stopTrajectoryPlayback();
        return;
      }
      const maxFrame = trajectoryFrameMax(history);
      const currentFrame = intradayChartState.frameIndex ?? maxFrame;
      intradayChartState.frameIndex = currentFrame >= maxFrame ? 0 : currentFrame + 1;
      drawTrajectoryFromState(history);
    }, 720);
    updateTrajectoryControls(currentIntradayHistory);
  }

  function toggleTrajectoryPlayback() {
    if (intradayChartState.playing) {
      stopTrajectoryPlayback();
    } else {
      startTrajectoryPlayback();
    }
  }

  function replayTrajectory() {
    stopTrajectoryPlayback(false);
    intradayChartState.frameIndex = 0;
    drawTrajectoryFromState(currentIntradayHistory);
    startTrajectoryPlayback();
  }

  function drawTrajectoryFromState(history) {
    const rows = trajectoryRows(history);
    if (!rows.length) {
      renderIntradayEmpty("盤中截點累積中；交易時段開著頁面後會逐格留下軌跡。");
      return;
    }
    const ids = rows.map((row) => row.stock_id);
    const scale = trajectoryScale(history, ids);
    const idsKey = ids.join("|");
    const maxFrame = trajectoryFrameMax(history);
    if (intradayChartState.frameIndex === null || intradayChartState.frameIndex > maxFrame) {
      intradayChartState.frameIndex = maxFrame;
    }
    if (!intradayChartState.playing && intradayChartState.frameIndex === null) {
      intradayChartState.frameIndex = maxFrame;
    }
    if (!intradayChartState.initialized || intradayChartState.idsKey !== idsKey || intradayChartState.scaleKey !== scale.key) {
      intradayChartState.lastPoints.clear();
      renderTrajectoryBase(scale);
      intradayChartState.idsKey = idsKey;
    }
    renderTrajectoryFrame(history, ids, scale, intradayChartState.frameIndex);
    updateTrajectoryControls(history);
    intradayChartState.lastStep = history.snapshots.length;
  }

  function renderTrajectoryChart(history) {
    if (!intradayChartState.playing) {
      intradayChartState.frameIndex = trajectoryFrameMax(history);
    }
    drawTrajectoryFromState(history);
  }

  function renderIntradayPanels(history) {
    if (!history || !history.snapshots || !history.snapshots.length) {
      renderIntradayEmpty("等待今日盤中資料；08:45 起每 15 分鐘會記錄一個 server 端截點。");
      return;
    }
    renderIntradayMovers(history);
    renderTrajectoryChart(history);
  }

  function todayOverviewRows(rows) {
    return (rows || [])
      .map((row) => {
        const open = numberOrNull(row.open);
        const high = numberOrNull(row.high);
        const low = numberOrNull(row.low);
        const close = numberOrNull(row.close);
        const spread = numberOrNull(row.spread);
        const spreadPer = numberOrNull(row.spread_per);
        const volume = numberOrNull(row.volume);
        const stockName = String(row.stock_name || row.stock_id || "").trim();
        if (!stockName || [open, high, low, close, spread, spreadPer, volume].some((value) => value === null)) return null;
        const previousClose = close - spread;
        if (!Number.isFinite(previousClose) || previousClose <= 0) return null;
        return {
          ...row,
          stock_name: stockName,
          volume,
          open_pct: (open - previousClose) / previousClose * 100,
          high_pct: (high - previousClose) / previousClose * 100,
          low_pct: (low - previousClose) / previousClose * 100,
          close_pct: spreadPer
        };
      })
      .filter(Boolean)
      .sort((a, b) => b.volume - a.volume)
      .slice(0, TODAY_OVERVIEW_TOP_N);
  }

  function renderTodayOverviewChart(rows) {
    const chart = document.getElementById("today-overview-chart");
    if (!chart) return;
    const scrollLeft = chart.scrollLeft;
    const chartRows = todayOverviewRows(rows);
    if (!chartRows.length) {
      chart.innerHTML = '<div class="overview-empty">尚無今日速覽資料</div>';
      chart.scrollLeft = scrollLeft;
      return;
    }

    const width = Math.max(2060, 50 + chartRows.length * 40 + 50);
    const height = 430;
    const plotTop = 52;
    const plotBottom = 266;
    const plotLeft = 46;
    const plotRight = width - 24;
    const plotHeight = plotBottom - plotTop;
    const scaleMin = -10;
    const scaleMax = 10;
    const xStep = (plotRight - plotLeft) / chartRows.length;
    const y = (percent) => {
      const clamped = Math.max(scaleMin, Math.min(scaleMax, percent));
      return plotBottom - ((clamped - scaleMin) / (scaleMax - scaleMin)) * plotHeight;
    };
    const ticks = [-10, -5, 0, 5, 10].map((tick) => {
      const tickY = y(tick);
      const labelClass = tick > 0 ? "pos" : tick < 0 ? "neg" : "";
      return `<line x1="${plotLeft}" x2="${plotRight}" y1="${tickY.toFixed(1)}" y2="${tickY.toFixed(1)}" class="overview-grid"></line>
        <text x="40" y="${(tickY + 5).toFixed(1)}" class="overview-y-label ${labelClass}" text-anchor="end">${tick}%</text>`;
    }).join("");
    const candles = chartRows.map((row, index) => {
      const centerX = plotLeft + xStep * index + xStep / 2;
      const candleWidth = Math.max(10, Math.min(26, xStep * 0.56));
      const openY = y(row.open_pct);
      const closeY = y(row.close_pct);
      const highY = y(row.high_pct);
      const lowY = y(row.low_pct);
      const bodyY = Math.min(openY, closeY);
      const bodyHeight = Math.max(3, Math.abs(openY - closeY));
      const color = row.close_pct >= 0 ? "#ff3438" : "#00c92f";
      const label = Array.from(String(row.stock_name)).map((char, charIndex) => (
        `<tspan x="${centerX.toFixed(1)}" dy="${charIndex === 0 ? "0" : "1.05em"}">${escapeHtml(char)}</tspan>`
      )).join("");
      const title = `${index + 1}. ${row.stock_name} ${formatOverviewPercent(row.close_pct)}｜成交口數 ${formatNumber(row.volume, 0)}${displayFuturesId(row) !== "-" ? "｜" + displayFuturesId(row) : ""}`;
      return `<g class="overview-candle" tabindex="0" data-name="${escapeHtml(row.stock_name)}" data-pct="${escapeHtml(formatOverviewPercent(row.close_pct))}" data-volume="${escapeHtml(formatNumber(row.volume, 0))}">
        <title>${escapeHtml(title)}</title>
        <line class="overview-wick" x1="${centerX.toFixed(1)}" x2="${centerX.toFixed(1)}" y1="${highY.toFixed(1)}" y2="${lowY.toFixed(1)}" stroke-width="2" stroke-linecap="round"></line>
        <rect x="${(centerX - candleWidth / 2).toFixed(1)}" y="${bodyY.toFixed(1)}" width="${candleWidth.toFixed(1)}" height="${bodyHeight.toFixed(1)}" rx="1" fill="${color}"></rect>
        <text x="${centerX.toFixed(1)}" y="286" class="overview-x-label" text-anchor="middle">${label}</text>
      </g>`;
    }).join("");
    const chips = chartRows.slice(0, 3).map((row) => (
      `<span>${escapeHtml(row.stock_name)} ${escapeHtml(formatOverviewPercent(row.close_pct))} / ${escapeHtml(formatNumber(row.volume, 0))}口</span>`
    )).join("");

    chart.innerHTML = `<div class="overview-chart-toolbar" style="min-width: ${width}px;">
        <div class="overview-legend"><span class="up">上漲</span><span class="down">下跌</span><span>Y 軸：相對昨收漲跌幅</span></div>
        <div class="overview-chips">${chips}</div>
      </div>
      <svg class="overview-chart" role="img" aria-label="成交口數Top50股票期貨今日速覽" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" style="min-width: ${width}px;">
        <rect class="overview-bg" x="0" y="0" width="${width}" height="${height}"></rect>
        ${ticks}
        <line x1="${plotLeft}" x2="${plotRight}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}" class="overview-axis-zero"></line>
        ${candles}
      </svg>`;
    chart.scrollLeft = scrollLeft;
    if (window.requestAnimationFrame) {
      window.requestAnimationFrame(() => {
        chart.scrollLeft = scrollLeft;
      });
    }
  }

  function captureViewportScroll() {
    return {
      x: window.scrollX || window.pageXOffset || 0,
      y: window.scrollY || window.pageYOffset || 0
    };
  }

  function restoreViewportScroll(position) {
    if (!position) return;
    window.scrollTo(position.x, position.y);
    if (window.requestAnimationFrame) {
      window.requestAnimationFrame(() => window.scrollTo(position.x, position.y));
    }
  }

  function renderTable(tableName, tableWrapId, tableHead, bodyHtml, nextSignatures) {
    const tableWrap = document.getElementById(tableWrapId);
    if (!tableWrap) return;
    const scrollLeft = tableWrap.scrollLeft;
    const scrollTop = tableWrap.scrollTop;
    const table = tableWrap.querySelector("table");
    const tbody = table ? table.querySelector("tbody") : null;
    if (tbody) {
      tbody.innerHTML = bodyHtml;
    } else {
      tableWrap.innerHTML = `<table>${tableHead}<tbody>${bodyHtml}</tbody></table>`;
    }
    tableSignatures[tableName] = nextSignatures;
    tableWrap.scrollLeft = scrollLeft;
    tableWrap.scrollTop = scrollTop;
    if (window.requestAnimationFrame) {
      window.requestAnimationFrame(() => {
        tableWrap.scrollLeft = scrollLeft;
        tableWrap.scrollTop = scrollTop;
      });
    }
  }

  function emptyRender(html) {
    return { html, signatures: new Map() };
  }

  function currentAtrPercent() {
    const select = document.getElementById("atr-filter");
    if (select && select.value) return select.value;
    return new URLSearchParams(window.location.search).get("min_atr_percent") || "3";
  }

  function currentAsOfDate() {
    if (!asOfPinned) return taipeiDateString();
    const input = document.getElementById("as-of-filter");
    if (input && input.value) return input.value;
    return new URLSearchParams(window.location.search).get("as_of") || taipeiDateString();
  }

  function isSelectedToday() {
    return !asOfPinned || currentAsOfDate() === taipeiDateString();
  }

  function shouldForceLiveRefresh() {
    return isSelectedToday() && isTradingSession();
  }

  function shouldRunClosingRefresh(now = new Date()) {
    if (!isSelectedToday()) return false;
    const parts = taipeiParts(now);
    if (parts.weekday === "Sat" || parts.weekday === "Sun") return false;
    const currentMinutes = Number(parts.hour) * 60 + Number(parts.minute);
    const dueMinute = CLOSING_REFRESH_MINUTES.filter((minute) => currentMinutes >= minute).pop();
    if (dueMinute == null) return false;
    const key = `${taipeiDateString(now)}-${dueMinute}`;
    if (completedClosingRefreshes.has(key)) return false;
    completedClosingRefreshes.add(key);
    return true;
  }

  function setAtrFilterValue(value) {
    const select = document.getElementById("atr-filter");
    if (!select) return;
    const match = Array.from(select.options).find((option) => Number(option.value) === Number(value));
    if (match) select.value = match.value;
  }

  function setAsOfFilterValue(value) {
    const input = document.getElementById("as-of-filter");
    if (!input || !value) return;
    input.value = value;
  }

  function replaceUrlParams(params) {
    const query = params.toString();
    const nextUrl = `${window.location.pathname}${query ? "?" + query : ""}`;
    if (nextUrl === `${window.location.pathname}${window.location.search}`) return;
    window.history.replaceState(null, "", nextUrl);
  }

  function syncAsOfParam(params, value) {
    if (!asOfPinned) {
      params.delete("as_of");
      return;
    }
    const normalized = value || taipeiDateString();
    params.set("as_of", normalized);
  }

  function buildPoolUrl() {
    const params = new URLSearchParams(window.location.search);
    params.set("min_atr_percent", currentAtrPercent());
    syncAsOfParam(params, currentAsOfDate());
    params.delete("refresh");
    const query = params.toString();
    return query ? `/api/pool?${query}` : "/api/pool";
  }

  function updateCriteria(payload) {
    const criteria = payload && payload.criteria ? payload.criteria : {};
    const source = payload && payload.source ? payload.source : {};
    if (criteria.min_atr_percent != null) {
      setAtrFilterValue(criteria.min_atr_percent);
      setText("atr-criteria-value", `${formatPercent(criteria.min_atr_percent)}%`);
    }
    const effectiveAsOf = source.effective_as_of_date || (payload && payload.as_of_date);
    if (effectiveAsOf) setAsOfFilterValue(effectiveAsOf);
  }

  function initAtrFilter() {
    const select = document.getElementById("atr-filter");
    if (!select) return;
    const requestedAtr = new URLSearchParams(window.location.search).get("min_atr_percent");
    if (requestedAtr) setAtrFilterValue(requestedAtr);
    select.addEventListener("change", () => {
      const params = new URLSearchParams(window.location.search);
      params.set("min_atr_percent", select.value);
      params.delete("refresh");
      replaceUrlParams(params);
      setText("atr-criteria-value", `${formatPercent(select.value)}%`);
      loadPool();
    });
  }

  function initAsOfFilter() {
    const input = document.getElementById("as-of-filter");
    if (!input) return;
    const today = taipeiDateString();
    input.max = today;
    const requestedAsOf = new URLSearchParams(window.location.search).get("as_of");
    setAsOfFilterValue(asOfPinned ? requestedAsOf || today : today);
    input.addEventListener("change", () => {
      asOfPinned = true;
      const params = new URLSearchParams(window.location.search);
      params.set("as_of", input.value || today);
      params.set("min_atr_percent", currentAtrPercent());
      params.delete("refresh");
      replaceUrlParams(params);
      loadPool();
    });
  }

  function switchPoolTab(tabName) {
    const nextTab = poolTabSubtitles[tabName] ? tabName : "small";
    document.querySelectorAll("[data-pool-tab]").forEach((button) => {
      const active = button.dataset.poolTab === nextTab;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll("[data-pool-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.poolPanel !== nextTab;
    });
    setText("pool-tabs-subtitle", poolTabSubtitles[nextTab]);
  }

  function initPoolTabs() {
    document.querySelectorAll("[data-pool-tab]").forEach((button) => {
      button.addEventListener("click", () => switchPoolTab(button.dataset.poolTab));
    });
    switchPoolTab("small");
  }

  function watchlistTypeTokens(row) {
    return String(row && (row.contract_type || row.contract_type_label) || "")
      .split(/[,\s、，\\/]+/)
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean);
  }

  function watchlistMatchesTab(row, tabName) {
    if (tabName === "all") return true;
    const tokens = watchlistTypeTokens(row);
    if (tabName === "regular") {
      return tokens.includes("regular") || tokens.includes("大型") || tokens.includes("大型股期");
    }
    if (tabName === "small") {
      return tokens.includes("small") || tokens.includes("小型") || tokens.includes("小型股期");
    }
    return true;
  }

  function filterWatchlistRows(rows, tabName) {
    const nextTab = watchlistTabSubtitles[tabName] ? tabName : "all";
    return nextTab === "all" ? rows : rows.filter((row) => watchlistMatchesTab(row, nextTab));
  }

  function updateWatchlistTabs(tabName) {
    const nextTab = watchlistTabSubtitles[tabName] ? tabName : "all";
    currentWatchlistTab = nextTab;
    document.querySelectorAll("[data-watchlist-tab]").forEach((button) => {
      const active = button.dataset.watchlistTab === nextTab;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    const panel = document.getElementById("watchlist-panel");
    if (panel) {
      panel.dataset.watchlistPanel = nextTab;
      panel.setAttribute("aria-labelledby", `watchlist-tab-${nextTab === "regular" ? "large" : nextTab}`);
    }
    setText("watchlist-tabs-subtitle", watchlistTabSubtitles[nextTab]);
  }

  function switchWatchlistTab(tabName) {
    updateWatchlistTabs(tabName);
    renderWatchlistTable(currentWatchlistRows);
  }

  function initWatchlistTabs() {
    document.querySelectorAll("[data-watchlist-tab]").forEach((button) => {
      button.addEventListener("click", () => switchWatchlistTab(button.dataset.watchlistTab));
    });
    updateWatchlistTabs("all");
  }

  function renderPoolRows(rows, tableName) {
    if (!rows.length) {
      return emptyRender('<tr><td class="empty-row" colspan="9">今日沒有符合條件的標的</td></tr>');
    }
    const signatures = new Map();
    const html = rows.map((row, index) => {
      const spreadPerClass = changeClass(row.spread_per);
      const rankStrip = renderRankStrip(row);
      const intradayChange = renderIntradayChange(row);
      const intradayOpenChange = renderIntradayOpenChange(row);
      const intradayStatus = renderIntradayStatus(row);
      const key = rowKey(row);
      const signature = rowSignature([
        index + 1,
        row.close,
        row.spread_per,
        row.atr_20_percent,
        row.best_volume_rank_5d,
        row.worst_volume_rank_5d,
        rankValues(row).join(","),
        intradayChange,
        intradayOpenChange,
        intradayStatus
      ]);
      signatures.set(key, signature);
      return `<tr${rowAttributes(tableName, key, signature)}>
        <td class="number">${index + 1}</td>
        <td><div class="stock"><strong>${escapeHtml(row.stock_name || "-")}</strong><span>${escapeHtml(stockMeta(row))}</span></div></td>
        <td class="number">${formatNumber(row.close, 2)}</td>
        <td class="number ${spreadPerClass}">${formatPercentValue(row.spread_per)}</td>
        <td class="number">${formatNumber(row.atr_20_percent, 2)}%</td>
        <td>${rankStrip}</td>
        <td class="number">${intradayChange}</td>
        <td class="number">${intradayOpenChange}</td>
        <td>${intradayStatus}</td>
      </tr>`;
    }).join("");
    return { html, signatures };
  }

  function renderNewEntryRows(rows) {
    if (!rows.length) {
      return emptyRender('<tr><td class="empty-row" colspan="9">今日沒有新進榜標的</td></tr>');
    }
    const signatures = new Map();
    const html = rows.map((row, index) => {
      const previousRank = row.previous_rank == null ? "新" : formatNumber(row.previous_rank, 0);
      const previousVolume = row.previous_volume == null ? "-" : formatNumber(row.previous_volume, 0);
      const key = rowKey(row);
      const signature = rowSignature([
        index + 1,
        row.current_rank,
        row.previous_rank,
        row.current_volume,
        row.previous_volume,
        row.close
      ]);
      signatures.set(key, signature);
      return `<tr${rowAttributes("newEntry", key, signature)}>
        <td class="number">${index + 1}</td>
        <td><div class="stock"><strong>${escapeHtml(row.stock_name || "-")}</strong><span>${escapeHtml(stockMeta(row))}</span></div></td>
        <td>${escapeHtml(row.contract_type_label || "-")}</td>
        <td class="number">${formatNumber(row.current_rank, 0)}</td>
        <td class="number">${previousRank}</td>
        <td class="number">${formatNumber(row.current_volume, 0)}</td>
        <td class="number">${previousVolume}</td>
        <td class="number">${formatNumber(row.close, 2)}</td>
        <td>${escapeHtml(row.contract_date || "-")}</td>
      </tr>`;
    }).join("");
    return { html, signatures };
  }

  function renderWatchlistRows(rows) {
    if (!rows.length) {
      return emptyRender('<tr><td class="empty-row" colspan="12">尚無股票期貨 watchlist 資料</td></tr>');
    }
    const signatures = new Map();
    const html = rows.map((row, index) => {
      const spreadClass = changeClass(row.spread);
      const spreadPerClass = changeClass(row.spread_per);
      const key = rowKey(row);
      const signature = rowSignature([
        index + 1,
        row.date,
        row.open,
        row.high,
        row.low,
        row.close,
        row.volume,
        row.spread,
        row.spread_per
      ]);
      signatures.set(key, signature);
      return `<tr${rowAttributes("watchlist", key, signature)}>
        <td class="number">${index + 1}</td>
        <td><div class="stock"><strong>${escapeHtml(row.stock_name || "-")}</strong><span>${escapeHtml(stockMeta(row))}</span></div></td>
        <td class="number ${spreadClass}">${formatNumber(row.spread, 2)}</td>
        <td class="number ${spreadPerClass}">${formatPercentValue(row.spread_per)}</td>
        <td class="number">${formatNumber(row.volume, 0)}</td>
        <td class="number">${formatNumber(row.open, 2)}</td>
        <td class="number">${formatNumber(row.high, 2)}</td>
        <td class="number">${formatNumber(row.low, 2)}</td>
        <td class="number">${formatNumber(row.close, 2)}</td>
        <td>${escapeHtml(row.contract_type_label || "-")}</td>
        <td>${escapeHtml(row.contract_date || "-")}</td>
        <td>${escapeHtml(row.date || "-")}</td>
      </tr>`;
    }).join("");
    return { html, signatures };
  }

  function renderPoolTable(rows) {
    const rendered = renderPoolRows(rows, "pool");
    renderTable("pool", "pool-table-wrap", poolTableHead, rendered.html, rendered.signatures);
  }

  function renderActivePoolTable(rows) {
    const rendered = renderPoolRows(rows, "activePool");
    renderTable("activePool", "active-pool-table-wrap", poolTableHead, rendered.html, rendered.signatures);
  }

  function renderNewEntryTable(rows) {
    const rendered = renderNewEntryRows(rows);
    renderTable("newEntry", "new-entry-table-wrap", newEntryTableHead, rendered.html, rendered.signatures);
  }

  function renderWatchlistTable(rows) {
    currentWatchlistRows = rows || [];
    const rendered = renderWatchlistRows(filterWatchlistRows(currentWatchlistRows, currentWatchlistTab));
    renderTable("watchlist", "watchlist-table-wrap", watchlistTableHead, rendered.html, rendered.signatures);
  }

  function renderErrorTables(message) {
    renderTable(
      "pool",
      "pool-table-wrap",
      poolTableHead,
      `<tr><td class="empty-row" colspan="9">${escapeHtml(message)}</td></tr>`,
      new Map()
    );
    renderTable(
      "activePool",
      "active-pool-table-wrap",
      poolTableHead,
      `<tr><td class="empty-row" colspan="9">${escapeHtml(message)}</td></tr>`,
      new Map()
    );
    renderTable(
      "newEntry",
      "new-entry-table-wrap",
      newEntryTableHead,
      `<tr><td class="empty-row" colspan="9">${escapeHtml(message)}</td></tr>`,
      new Map()
    );
    renderTable(
      "watchlist",
      "watchlist-table-wrap",
      watchlistTableHead,
      `<tr><td class="empty-row" colspan="12">${escapeHtml(message)}</td></tr>`,
      new Map()
    );
    renderTodayOverviewChart([]);
    renderButterflyChart([]);
  }

  let isLoading = false;
  let hasRenderedSnapshot = false;
  let currentIntradayHistory = null;
  let currentDashboardSource = null;

  async function loadPool() {
    if (isLoading) return;
    isLoading = true;
    setText("status-text", hasRenderedSnapshot ? "更新中" : "資料載入中");
    try {
      const url = buildPoolUrl();
      const pageParams = new URLSearchParams(window.location.search);
      if (pageParams.get("refresh") === "1") {
        pageParams.set("min_atr_percent", currentAtrPercent());
        syncAsOfParam(pageParams, currentAsOfDate());
        pageParams.delete("refresh");
        replaceUrlParams(pageParams);
      }
      const response = await fetch(url);
      const payload = await response.json();
      if (!response.ok || payload.error) throw new Error(payload.error || "資料載入失敗");

      setText("as-of-date", payload.as_of_date);
      setText("generated-at", payload.generated_at);
      updateCriteria(payload);
      const effectiveAsOf = payload.source && payload.source.effective_as_of_date ? payload.source.effective_as_of_date : payload.as_of_date;
      if (effectiveAsOf) {
        const syncedParams = new URLSearchParams(window.location.search);
        syncAsOfParam(syncedParams, effectiveAsOf);
        syncedParams.set("min_atr_percent", currentAtrPercent());
        syncedParams.delete("refresh");
        replaceUrlParams(syncedParams);
      }
      setText("row-count", payload.row_count);
      setText("active-row-count", payload.active_row_count);
      setText("watchlist-count", payload.watchlist_count);
      setText("contract-rows", payload.source && payload.source.contract_rows);
      setText("status-text", payload.source && payload.source.stale_cache_fallback ? "暫用快取" : "同步完成");
      const viewportScroll = captureViewportScroll();
      currentDashboardSource = payload.source || {};
      let intradayHistory = payload.intraday_trajectory
        ? useIntradayHistory(payload.intraday_trajectory, effectiveAsOf)
        : null;
      if (!hasIntradaySnapshots(intradayHistory)) {
        intradayHistory = recordIntradaySnapshot(payload);
      }
      currentIntradayHistory = intradayHistory;
      renderPoolTable(payload.rows || []);
      renderActivePoolTable(payload.active_rows || []);
      renderNewEntryTable(payload.new_entry_rows || []);
      renderWatchlistTable(payload.watchlist_rows || []);
      renderTodayOverviewChart(payload.watchlist_rows || []);
      renderButterflyChart(payload.watchlist_rows || []);
      if (payload.source && payload.source.historical_mode && !hasIntradaySnapshots(intradayHistory)) {
        renderIntradayEmpty("歷史回溯模式不記錄排名軌跡；請切回今日資料。");
      } else {
        renderIntradayPanels(intradayHistory);
      }
      restoreViewportScroll(viewportScroll);
      hasRenderedSnapshot = true;
      updateRealtimeStatus(shouldForceLiveRefresh(), payload);
      updateSessionStatus(shouldForceLiveRefresh());
    } catch (error) {
      setText("status-text", "資料錯誤");
      setText("realtime-status", "即時排序暫停");
      updateSessionStatus(shouldForceLiveRefresh());
      if (!hasRenderedSnapshot) {
        renderErrorTables(error.message);
      }
    } finally {
      isLoading = false;
    }
  }

  function startRealtimeRanking() {
    updateRealtimeStatus(shouldForceLiveRefresh(), null);
    updateSessionStatus(shouldForceLiveRefresh());
    window.setInterval(() => {
      const live = shouldForceLiveRefresh();
      const closingRefresh = shouldRunClosingRefresh();
      const backgroundRefresh = live || closingRefresh;
      updateRealtimeStatus(live, null);
      updateSessionStatus(live);
      if (backgroundRefresh) loadPool();
    }, REALTIME_REFRESH_MS);
  }

  initThemeToggle();
  initAtrFilter();
  initAsOfFilter();
  initPoolTabs();
  initWatchlistTabs();
  loadCachedIntradayTrajectory().finally(() => loadPool());
  startRealtimeRanking();
}());
</script>"""


def _round_value(value: object, digits: int) -> float:
    if pd.isna(value):
        return 0.0
    return round(float(value), digits)


def _optional_round_value(value: object, digits: int) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def _date_value(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _string_value(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value)


def _format_optional_number(value: object, digits: int) -> str:
    if value is None or pd.isna(value):
        return "-"
    return "{:,.{}f}".format(float(value), digits)


def _format_optional_percent(value: object, digits: int) -> str:
    if value is None or pd.isna(value):
        return "-"
    return "{:,.{}f}%".format(float(value), digits)


def _change_class(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    number = float(value)
    if number > 0:
        return "positive"
    if number < 0:
        return "negative"
    return ""


def _display_futures_id(row: Dict[str, object]) -> str:
    full_code = _string_value(row.get("finmind_futures_id")).strip()
    short_code = _string_value(row.get("futures_id")).strip()
    return full_code or short_code or "-"


def _display_inline_futures_id(row: Dict[str, object]) -> str:
    code = _display_futures_id(row)
    if not code or code == "-":
        return ""
    return "/".join(part.strip() for part in code.replace("，", ",").split(",") if part.strip())


def _display_stock_meta(row: Dict[str, object]) -> str:
    stock_id = _string_value(row.get("stock_id")).strip() or "-"
    futures_id = _display_inline_futures_id(row)
    if futures_id:
        return "{} / {}".format(stock_id, futures_id)
    return stock_id


def _date_window(df: pd.DataFrame, count: int) -> str:
    if df.empty or "date" not in df.columns:
        return ""
    dates = sorted(pd.to_datetime(df["date"]).dt.normalize().dropna().unique())
    if not dates:
        return ""
    dates = dates[-count:]
    return "{}~{}".format(
        pd.Timestamp(dates[0]).strftime("%Y-%m-%d"),
        pd.Timestamp(dates[-1]).strftime("%Y-%m-%d"),
    )


def _format_date_range(values: Sequence[pd.Timestamp]) -> str:
    if not values:
        return ""
    return "{}~{}".format(
        pd.Timestamp(values[0]).strftime("%Y-%m-%d"),
        pd.Timestamp(values[-1]).strftime("%Y-%m-%d"),
    )


def _first_text(values) -> str:
    for value in values:
        if pd.notna(value) and str(value).strip():
            return str(value).strip()
    return ""


def _join_unique_text(values) -> str:
    unique_values = sorted({str(value).strip() for value in values if pd.notna(value) and str(value).strip()})
    return ", ".join(unique_values)


def _normalize_date(value: Optional[object]) -> Optional[date]:
    if value is None:
        return None
    return pd.Timestamp(value).date()


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the daily stock futures pool dashboard.")
    parser.add_argument("--host", default=os.getenv("DASHBOARD_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", str(DEFAULT_PORT))))
    args = parser.parse_args(argv)
    run_server(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
