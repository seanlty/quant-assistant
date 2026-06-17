import os
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import requests
from requests import RequestException
from dotenv import load_dotenv

from src.data_sources import FINMIND_DATA_URL, fetch_finmind_stock_prices, fetch_taifex_stock_futures_contracts
from src.stock_pool import (
    StockPoolCriteria,
    build_stock_futures_pool,
    get_last_trading_dates,
    normalize_futures_contracts,
    normalize_stock_prices,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


@pytest.mark.integration
def test_live_finmind_stock_futures_pool_matches_daily_rules():
    """Validate the daily stock futures pool against live FinMind and TAIFEX data."""
    token = os.getenv("FINMIND_API_TOKEN") or os.getenv("FINMIND_API_KEY") or os.getenv("FINMIND_TOKEN")
    if not token:
        pytest.skip("FINMIND_API_TOKEN or FINMIND_API_KEY is not set in .env")

    criteria = StockPoolCriteria()
    end_day = _get_api_end_date()
    required_days = max(criteria.volume_days, criteria.atr_days + 1)
    trading_dates = _recent_finmind_trading_dates(
        token,
        end_day,
        required_days + int(os.getenv("STOCK_POOL_API_TRADING_DATE_BUFFER", "10")),
    )

    raw_prices = _fetch_finmind_prices_by_trading_date(token, trading_dates, required_days)
    assert not raw_prices.empty, "FinMind returned no TaiwanStockPrice rows"

    try:
        contracts = fetch_taifex_stock_futures_contracts(timeout=60)
    except RequestException as exc:
        pytest.fail("TAIFEX contract request failed: {}".format(exc.__class__.__name__), pytrace=False)
    assert not contracts.empty, "TAIFEX returned no stock futures contract rows"

    prices = normalize_stock_prices(raw_prices)
    as_of_date = prices["date"].max()
    _assert_has_enough_trading_dates(prices, as_of_date, criteria)

    pool = build_stock_futures_pool(
        raw_prices,
        contracts,
        as_of_date=as_of_date,
        criteria=criteria,
    )
    expected_ids = _expected_stock_ids(prices, contracts, as_of_date, criteria)

    assert set(pool["stock_id"]) == expected_ids
    _assert_pool_rows_match_rules(pool, prices, contracts, as_of_date, criteria)


def _get_api_end_date():
    configured_date = os.getenv("STOCK_POOL_API_END_DATE")
    if configured_date:
        return date.fromisoformat(configured_date)
    return date.today()


def _recent_finmind_trading_dates(token, end_day, count):
    params = {"dataset": "TaiwanStockTradingDate"}
    headers = {"Authorization": "Bearer {}".format(token)}
    try:
        response = requests.get(FINMIND_DATA_URL, params=params, headers=headers, timeout=60)
        response.raise_for_status()
    except RequestException as exc:
        pytest.fail("FinMind trading-date request failed: {}".format(exc.__class__.__name__), pytrace=False)

    payload = response.json()
    if payload.get("status") != 200:
        pytest.fail("FinMind trading-date API returned status {}".format(payload.get("status")), pytrace=False)

    data = pd.DataFrame(payload.get("data", []))
    if data.empty or "date" not in data.columns:
        pytest.fail("FinMind trading-date API returned no date rows", pytrace=False)

    trading_dates = pd.to_datetime(data["date"]).dt.date
    trading_dates = sorted(trading_day for trading_day in trading_dates if trading_day <= end_day)
    if len(trading_dates) < count:
        pytest.fail("FinMind returned fewer than {} trading dates".format(count), pytrace=False)
    return trading_dates[-count:]


def _fetch_finmind_prices_by_trading_date(token, trading_dates, required_days):
    frames = []
    for trading_day in reversed(trading_dates):
        try:
            day_prices = fetch_finmind_stock_prices(
                start_date=trading_day.isoformat(),
                token=token,
                timeout=60,
            )
        except RequestException as exc:
            pytest.fail(
                "FinMind price request failed for {}: {}".format(
                    trading_day.isoformat(),
                    exc.__class__.__name__,
                ),
                pytrace=False,
            )
        if day_prices.empty:
            continue
        frames.append(day_prices)
        if len(frames) == required_days:
            break

    if len(frames) < required_days:
        pytest.fail(
            "FinMind returned prices for only {} of {} required trading dates".format(
                len(frames),
                required_days,
            ),
            pytrace=False,
        )
    return pd.concat(list(reversed(frames)), ignore_index=True)


def _assert_has_enough_trading_dates(prices, as_of_date, criteria):
    required_days = max(criteria.volume_days, criteria.atr_days + 1)
    trading_dates = get_last_trading_dates(prices, as_of_date, required_days)
    assert len(trading_dates) == required_days


def _expected_stock_ids(prices, contracts, as_of_date, criteria):
    volume_ids = _persistent_top_volume_ids(prices, as_of_date, criteria)
    small_futures_ids = _small_stock_futures_ids(contracts)
    price_ids = _price_range_ids(prices, as_of_date, criteria)
    atr_ids = _atr_percent_ids(prices, as_of_date, criteria)
    return volume_ids & small_futures_ids & price_ids & atr_ids


def _persistent_top_volume_ids(prices, as_of_date, criteria):
    volume_dates = get_last_trading_dates(prices, as_of_date, criteria.volume_days)
    window = prices[prices["date"].isin(volume_dates)].copy()
    window["volume_rank"] = window.groupby("date")["Trading_Volume"].rank(
        method="min",
        ascending=False,
    )
    top = window[window["volume_rank"] <= criteria.volume_top_n]
    top_day_counts = top.groupby("stock_id")["date"].nunique()
    return set(top_day_counts[top_day_counts == criteria.volume_days].index)


def _small_stock_futures_ids(contracts):
    normalized = normalize_futures_contracts(contracts)
    is_small_contract = normalized["contract_size"] == 100
    is_stock_id = normalized["stock_id"].str.match(r"^\d{4}$", na=False)
    return set(normalized.loc[is_small_contract & is_stock_id, "stock_id"])


def _price_range_ids(prices, as_of_date, criteria):
    latest = prices[prices["date"] == pd.Timestamp(as_of_date).normalize()]
    in_range = latest["close"].between(criteria.min_price, criteria.max_price)
    return set(latest.loc[in_range, "stock_id"])


def _atr_percent_ids(prices, as_of_date, criteria):
    atr_dates = get_last_trading_dates(prices, as_of_date, criteria.atr_days + 1)
    window = prices[prices["date"].isin(atr_dates)].copy()

    complete_day_counts = window.groupby("stock_id")["date"].nunique()
    complete_ids = complete_day_counts[complete_day_counts == len(atr_dates)].index
    window = window[window["stock_id"].isin(complete_ids)].sort_values(["stock_id", "date"])

    window["prev_close"] = window.groupby("stock_id")["close"].shift(1)
    true_range_parts = pd.concat(
        [
            window["max"] - window["min"],
            (window["max"] - window["prev_close"]).abs(),
            (window["min"] - window["prev_close"]).abs(),
        ],
        axis=1,
    )
    window["true_range"] = true_range_parts.max(axis=1)

    tr_window = window[window["date"].isin(atr_dates[1:])]
    atr = tr_window.groupby("stock_id", as_index=False).agg(
        atr_20=("true_range", "mean"),
        atr_days=("date", "nunique"),
    )
    atr = atr[atr["atr_days"] == criteria.atr_days].drop(columns=["atr_days"])

    latest = window[window["date"] == atr_dates[-1]][["stock_id", "close"]]
    atr = atr.merge(latest, on="stock_id", how="inner")
    atr["atr_20_percent"] = atr["atr_20"] / atr["close"] * 100.0
    return set(atr.loc[atr["atr_20_percent"] >= criteria.min_atr_percent, "stock_id"])


def _assert_pool_rows_match_rules(pool, prices, contracts, as_of_date, criteria):
    if pool.empty:
        return

    small_futures_ids = _small_stock_futures_ids(contracts)
    assert set(pool["stock_id"]).issubset(small_futures_ids)
    assert pool["close"].between(criteria.min_price, criteria.max_price).all()
    assert (pool["atr_20_percent"] >= criteria.min_atr_percent).all()

    volume_dates = get_last_trading_dates(prices, as_of_date, criteria.volume_days)
    window = prices[prices["date"].isin(volume_dates)].copy()
    window["volume_rank"] = window.groupby("date")["Trading_Volume"].rank(
        method="min",
        ascending=False,
    )

    for stock_id in pool["stock_id"]:
        stock_ranks = window.loc[window["stock_id"] == stock_id, ["date", "volume_rank"]]
        assert stock_ranks["date"].nunique() == criteria.volume_days
        assert (stock_ranks["volume_rank"] <= criteria.volume_top_n).all()
