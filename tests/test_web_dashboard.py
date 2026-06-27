import json
import os
import shutil
import uuid
from dataclasses import replace
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from src.stock_pool import StockPoolCriteria
from src.web_dashboard import (
    DashboardSnapshot,
    DashboardCache,
    ContractMetadataCache,
    IntradayTrajectoryCache,
    StockIndustryMapCache,
    TAIPEI_TZ,
    add_fugle_contract_months,
    application,
    build_stock_industry_map,
    build_near_month_tickers_from_watchlist_symbols,
    build_intraday_trajectory_history_from_candles,
    build_fugle_quote_volume_history,
    build_daily_pool_snapshot,
    build_dashboard_response,
    build_contracts_from_stock_futures,
    build_futures_product_history,
    build_futures_strategy_pool,
    fetch_fugle_near_month_candles,
    build_new_entry_pool,
    build_stock_futures_contract_map_from_fugle,
    build_stock_futures_contract_map,
    build_stock_futures_latest_quotes,
    build_stock_futures_volume_history,
    candidate_stock_ids_from_futures_volume,
    criteria_from_query,
    enrich_latest_quotes_with_daily_prices,
    enrich_watchlist_records_with_industry,
    final_readiness_from_daily_history,
    format_taipei_datetime,
    fugle_connection_status,
    industry_group_from_category,
    is_taipei_post_close_quote_window,
    latest_closing_refresh_at,
    merge_realtime_volume_history,
    new_entry_to_records,
    pool_to_records,
    render_dashboard_html,
    render_dashboard_shell,
    resolve_dashboard_as_of_date,
    watchlist_to_records,
    _render_today_overview_chart,
    _today_overview_rows,
)


def _call_wsgi(path="/", query_string=""):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(application({"PATH_INFO": path, "QUERY_STRING": query_string}, start_response))
    return captured, body


@pytest.fixture(autouse=True)
def _stub_stock_industry_map_loader(monkeypatch):
    monkeypatch.setattr(
        "src.web_dashboard.load_stock_industry_map",
        lambda token=None, timeout=60: {
            "source": "test stock industry map",
            "cache_hit": False,
            "is_fresh": True,
            "age_seconds": 0,
            "raw_rows": 0,
            "map": {},
        },
    )


def _minimal_snapshot(min_atr_percent=3.0):
    return DashboardSnapshot(
        generated_at="2026-06-15 12:00:00 GMT+8",
        as_of_date="2026-06-15",
        row_count=0,
        active_row_count=0,
        new_entry_count=0,
        watchlist_count=0,
        volume_window="-",
        atr_window="-",
        criteria={
            "volume_days": 5,
            "volume_top_n": 50,
            "atr_days": 20,
            "min_price": 500.0,
            "max_price": 5000.0,
            "min_atr_percent": min_atr_percent,
        },
        rows=[],
        active_rows=[],
        new_entry_rows=[],
        watchlist_rows=[],
        source={
            "price_rows": 0,
            "contract_rows": 0,
            "cache_schema_version": 5,
            "cache_kind": "final",
            "snapshot_stage": "final",
            "final_ready": True,
            "final_readiness_reason": "test snapshot",
            "historical_mode": False,
            "realtime_quote_enabled": False,
        },
    )


def _stock_futures_contract_rows():
    return pd.DataFrame(
        [
            {"futures_id": "CD", "stock_id": "2330", "stock_name": "TSMC", "contract_size": 2000},
            {"futures_id": "QF", "stock_id": "2330", "stock_name": "TSMC", "contract_size": 100},
        ]
    )


def _stock_futures_product_info():
    return pd.DataFrame(
        [
            {"code": "CDF", "type": "TaiwanFuturesDaily", "name": "TSMC futures"},
            {"code": "QFF", "type": "TaiwanFuturesDaily", "name": "Small TSMC futures"},
        ]
    )


def _futures_daily_rows(trading_dates):
    rows = []
    products = [
        ("CDF", 150, 2500),
        ("QFF", 1000, 3000),
    ]
    for offset, trading_day in enumerate(trading_dates):
        for futures_id, close_base, volume in products:
            close = close_base + offset
            rows.append(
                {
                    "date": trading_day.isoformat(),
                    "futures_id": futures_id,
                    "contract_date": "202606",
                    "open": close - 1,
                    "max": close * 1.04,
                    "min": close * 0.96,
                    "close": close,
                    "spread": 1,
                    "spread_per": 0.5,
                    "volume": volume,
                    "open_interest": 1000,
                    "trading_session": "position",
                }
            )
    return rows


def test_wsgi_application_serves_shell_and_health():
    captured, body = _call_wsgi("/")

    assert captured["status"].startswith("200")
    assert captured["headers"]["Content-Type"] == "text/html; charset=utf-8"
    assert "每日股期股池" in body.decode("utf-8")

    captured, body = _call_wsgi("/health")

    assert captured["status"].startswith("200")
    assert captured["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert body == b'{"status": "ok"}'


def test_dashboard_response_returns_404_for_unknown_path():
    status, content_type, body = build_dashboard_response("/missing")

    assert status == 404
    assert content_type == "text/plain; charset=utf-8"
    assert body == b"Not found"


def test_criteria_from_query_accepts_atr_dropdown_values():
    assert criteria_from_query({"min_atr_percent": ["2.5"]}).min_atr_percent == 2.5
    assert criteria_from_query({"min_atr_percent": ["5"]}).min_atr_percent == 5.0
    assert criteria_from_query({"min_atr_percent": ["3.2"]}).min_atr_percent == 3.0
    assert criteria_from_query({}).min_atr_percent == 3.0


def test_resolve_dashboard_as_of_respects_explicit_date_before_open():
    resolved = resolve_dashboard_as_of_date(
        date(2026, 6, 12),
        now=datetime(2026, 6, 18, 8, 30, tzinfo=TAIPEI_TZ),
    )

    assert resolved == date(2026, 6, 12)


def test_resolve_dashboard_as_of_uses_previous_cached_date_before_open():
    cache_dir = os.path.join(os.getcwd(), "data", "test-dashboard-cache-{}".format(uuid.uuid4().hex))
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(os.path.join(cache_dir, "dashboard_asof2026-06-17_final_vol5_top50_atr20_price500-5000_minatr3.json"), "w", encoding="utf-8") as cache_file:
            json.dump({"as_of_date": "2026-06-17"}, cache_file)
        with open(os.path.join(cache_dir, "dashboard_asof2026-06-18_intraday_vol5_top50_atr20_price500-5000_minatr3.json"), "w", encoding="utf-8") as cache_file:
            json.dump({"as_of_date": "2026-06-18"}, cache_file)

        resolved = resolve_dashboard_as_of_date(
            now=datetime(2026, 6, 18, 8, 30, tzinfo=TAIPEI_TZ),
            cache_dir=cache_dir,
        )

        assert resolved == date(2026, 6, 17)
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_resolve_dashboard_as_of_switches_to_today_at_open():
    resolved = resolve_dashboard_as_of_date(
        now=datetime(2026, 6, 18, 8, 45, tzinfo=TAIPEI_TZ),
    )

    assert resolved == date(2026, 6, 18)


def test_resolve_dashboard_as_of_falls_back_to_previous_weekday_on_weekend():
    resolved = resolve_dashboard_as_of_date(
        now=datetime(2026, 6, 20, 10, 0, tzinfo=TAIPEI_TZ),
    )

    assert resolved == date(2026, 6, 19)


def test_api_pool_passes_atr_and_as_of_query_to_cache(monkeypatch):
    captured = {}

    def fake_get_snapshot(force_refresh=False, criteria=StockPoolCriteria(), as_of_date=None):
        captured["force_refresh"] = force_refresh
        captured["criteria"] = criteria
        captured["as_of_date"] = as_of_date
        return _minimal_snapshot(criteria.min_atr_percent)

    monkeypatch.setattr("src.web_dashboard.dashboard_cache.get_snapshot", fake_get_snapshot)

    status, content_type, body = build_dashboard_response("/api/pool", "min_atr_percent=4.5&as_of=2026-06-12&refresh=1")
    payload = json.loads(body.decode("utf-8"))

    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    assert captured["force_refresh"] is True
    assert captured["criteria"].min_atr_percent == 4.5
    assert captured["as_of_date"] == date(2026, 6, 12)
    assert payload["criteria"]["min_atr_percent"] == 4.5


def test_api_pool_appends_intraday_trajectory_cache(monkeypatch):
    cache_dir = os.path.join(os.getcwd(), "data", "test-intraday-trajectory-{}".format(uuid.uuid4().hex))
    try:
        trajectory_cache = IntradayTrajectoryCache(cache_dir=cache_dir)
        snapshot = _minimal_snapshot()
        snapshot.source["cache_kind"] = "intraday"
        snapshot.source["snapshot_stage"] = "intraday"
        snapshot.source["realtime_quote_enabled"] = True
        snapshot.source["effective_as_of_date"] = "2026-06-15"
        snapshot.watchlist_rows.extend(
            [
                {
                    "stock_id": "2330",
                    "stock_name": "TSMC",
                    "futures_id": "CDF",
                    "contract_type_label": "大型",
                    "close": 1000,
                    "spread_per": 1.2,
                    "volume": 1200,
                },
                {
                    "stock_id": "2317",
                    "stock_name": "Hon Hai",
                    "futures_id": "DHF",
                    "contract_type_label": "大型",
                    "close": 180,
                    "spread_per": -0.5,
                    "volume": 2500,
                },
            ]
        )

        monkeypatch.setattr("src.web_dashboard.dashboard_cache.get_snapshot", lambda **kwargs: snapshot)
        monkeypatch.setattr("src.web_dashboard.intraday_trajectory_cache", trajectory_cache)
        monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 15, 9, 7, tzinfo=TAIPEI_TZ))

        status, content_type, body = build_dashboard_response("/api/pool")
        payload = json.loads(body.decode("utf-8"))

        assert status == 200
        assert content_type == "application/json; charset=utf-8"
        history = payload["intraday_trajectory"]
        assert history["as_of_date"] == "2026-06-15"
        assert history["snapshots"][0]["cutoff"] == "09:00"
        assert history["snapshots"][0]["rows"][0]["stock_id"] == "2317"
        assert history["snapshots"][0]["rows"][0]["rank"] == 1

        cached = trajectory_cache.read(date(2026, 6, 15))
        assert cached["cache_hit"] is True
        assert cached["snapshots"][0]["rows"][1]["stock_id"] == "2330"
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_intraday_trajectory_endpoint_reads_cache(monkeypatch):
    cache_dir = os.path.join(os.getcwd(), "data", "test-intraday-trajectory-{}".format(uuid.uuid4().hex))
    try:
        trajectory_cache = IntradayTrajectoryCache(cache_dir=cache_dir)
        history = {
            "version": 1,
            "as_of_date": "2026-06-16",
            "updated_at": "2026-06-16 09:00:00 GMT+8",
            "snapshots": [
                {
                    "cutoff": "08:45",
                    "captured_at": "2026-06-16 08:46:00 GMT+8",
                    "status": "fresh",
                    "rows": [{"stock_id": "2330", "stock_name": "TSMC", "volume": 100, "spread_per": 0.5, "rank": 1}],
                }
            ],
        }
        trajectory_cache._write_history(date(2026, 6, 16), history)
        monkeypatch.setattr("src.web_dashboard.intraday_trajectory_cache", trajectory_cache)

        status, content_type, body = build_dashboard_response("/api/intraday-trajectory", "as_of=2026-06-16")
        payload = json.loads(body.decode("utf-8"))

        assert status == 200
        assert content_type == "application/json; charset=utf-8"
        assert payload["cache_hit"] is True
        assert payload["as_of_date"] == "2026-06-16"
        assert payload["snapshots"][0]["cutoff"] == "08:45"
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_intraday_trajectory_read_defaults_to_previous_day_before_open(monkeypatch):
    cache_dir = os.path.join(os.getcwd(), "data", "test-intraday-trajectory-{}".format(uuid.uuid4().hex))
    try:
        trajectory_cache = IntradayTrajectoryCache(cache_dir=cache_dir)
        history = {
            "version": 1,
            "as_of_date": "2026-06-17",
            "updated_at": "2026-06-17 13:45:00 GMT+8",
            "snapshots": [
                {
                    "cutoff": "13:45",
                    "captured_at": "2026-06-17 13:45:00 GMT+8",
                    "status": "fresh",
                    "rows": [{"stock_id": "2330", "stock_name": "TSMC", "volume": 100, "spread_per": 0.5, "rank": 1}],
                }
            ],
        }
        trajectory_cache._write_history(date(2026, 6, 17), history)
        monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 18, 8, 30, tzinfo=TAIPEI_TZ))

        payload = trajectory_cache.read()

        assert payload["cache_hit"] is True
        assert payload["as_of_date"] == "2026-06-17"
        assert payload["snapshots"][0]["cutoff"] == "13:45"
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_intraday_trajectory_endpoint_reads_bootstrap_seed(monkeypatch):
    suffix = uuid.uuid4().hex
    cache_dir = os.path.join(os.getcwd(), "data", "test-intraday-cache-{}".format(suffix))
    seed_dir = os.path.join(os.getcwd(), "data", "test-intraday-seed-{}".format(suffix))
    try:
        os.makedirs(seed_dir, exist_ok=True)
        seed_path = os.path.join(seed_dir, "trajectory_asof2026-06-16.json")
        history = {
            "version": 1,
            "as_of_date": "2026-06-16",
            "updated_at": "2026-06-16 13:30:00 GMT+8",
            "snapshots": [
                {
                    "cutoff": "13:30",
                    "captured_at": "2026-06-16 13:30:00 GMT+8",
                    "status": "rebuilt_5m",
                    "rows": [{"stock_id": "2317", "stock_name": "Foxconn", "volume": 1200, "spread_per": 1.2, "rank": 1}],
                }
            ],
        }
        with open(seed_path, "w", encoding="utf-8") as seed_file:
            json.dump(history, seed_file, ensure_ascii=False)

        trajectory_cache = IntradayTrajectoryCache(cache_dir=cache_dir, seed_dir=seed_dir)
        monkeypatch.setattr("src.web_dashboard.intraday_trajectory_cache", trajectory_cache)

        status, content_type, body = build_dashboard_response("/api/intraday-trajectory", "as_of=2026-06-16")
        payload = json.loads(body.decode("utf-8"))

        assert status == 200
        assert content_type == "application/json; charset=utf-8"
        assert payload["cache_hit"] is True
        assert payload["snapshots"][0]["cutoff"] == "13:30"
        assert payload["snapshots"][0]["rows"][0]["stock_id"] == "2317"
        assert os.path.exists(os.path.join(cache_dir, "trajectory_asof2026-06-16.json"))
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)
        shutil.rmtree(seed_dir, ignore_errors=True)


def test_intraday_trajectory_cache_prunes_to_recent_five_days():
    cache_dir = os.path.join(os.getcwd(), "data", "test-intraday-trajectory-{}".format(uuid.uuid4().hex))
    try:
        trajectory_cache = IntradayTrajectoryCache(cache_dir=cache_dir, retention_days=5)
        for day in range(1, 8):
            as_of = date(2026, 6, day)
            trajectory_cache._write_history(as_of, {"version": 1, "as_of_date": as_of.isoformat(), "snapshots": []})

        trajectory_cache._prune()
        remaining = sorted(filename for filename in os.listdir(cache_dir) if filename.endswith(".json"))

        assert len(remaining) == 5
        assert remaining[0] == "trajectory_asof2026-06-03.json"
        assert remaining[-1] == "trajectory_asof2026-06-07.json"
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_intraday_trajectory_cache_replace_history_preserves_source():
    cache_dir = os.path.join(os.getcwd(), "data", "test-intraday-trajectory-{}".format(uuid.uuid4().hex))
    try:
        trajectory_cache = IntradayTrajectoryCache(cache_dir=cache_dir)
        history = {
            "version": 1,
            "as_of_date": "2026-06-17",
            "updated_at": "2026-06-17 14:50:00 GMT+8",
            "source": {"type": "Fugle intraday candles"},
            "snapshots": [
                {
                    "cutoff": "09:00",
                    "captured_at": "2026-06-17 14:50:00 GMT+8",
                    "status": "rebuilt_5m",
                    "rows": [{"stock_id": "2330", "volume": 100, "spread_per": 1.0, "rank": 1}],
                }
            ],
        }

        payload = trajectory_cache.replace_history(date(2026, 6, 17), history)

        assert payload["cache_hit"] is True
        assert payload["source"]["type"] == "Fugle intraday candles"
        assert payload["snapshots"][0]["status"] == "rebuilt_5m"
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_build_intraday_trajectory_history_from_candles_uses_cumulative_volume():
    watchlist_rows = [
        {
            "stock_id": "2330",
            "stock_name": "台積電",
            "finmind_futures_id": "CDF",
            "contract_type_label": "大型",
            "close": 105,
            "spread": 5,
        },
        {
            "stock_id": "2317",
            "stock_name": "鴻海",
            "finmind_futures_id": "DHF",
            "contract_type_label": "大型",
            "close": 98,
            "spread": -2,
        },
    ]
    candle_rows = pd.DataFrame(
        [
            {
                "as_of_date": date(2026, 6, 17),
                "minute": 8 * 60 + 45,
                "stock_id": "2330",
                "stock_name": "台積電",
                "finmind_futures_id": "CDF",
                "contract_type_label": "大型",
                "contract_date": "CDFG6",
                "symbol": "CDFG6",
                "close": 102,
                "Trading_Volume": 100,
            },
            {
                "as_of_date": date(2026, 6, 17),
                "minute": 8 * 60 + 50,
                "stock_id": "2330",
                "stock_name": "台積電",
                "finmind_futures_id": "CDF",
                "contract_type_label": "大型",
                "contract_date": "CDFG6",
                "symbol": "CDFG6",
                "close": 104,
                "Trading_Volume": 50,
            },
            {
                "as_of_date": date(2026, 6, 17),
                "minute": 8 * 60 + 55,
                "stock_id": "2330",
                "stock_name": "台積電",
                "finmind_futures_id": "CDF",
                "contract_type_label": "大型",
                "contract_date": "CDFG6",
                "symbol": "CDFG6",
                "close": 104,
                "Trading_Volume": 0,
            },
            {
                "as_of_date": date(2026, 6, 17),
                "minute": 8 * 60 + 45,
                "stock_id": "2317",
                "stock_name": "鴻海",
                "finmind_futures_id": "DHF",
                "contract_type_label": "大型",
                "contract_date": "DHFG6",
                "symbol": "DHFG6",
                "close": 99,
                "Trading_Volume": 300,
            },
        ]
    )

    history = build_intraday_trajectory_history_from_candles(
        date(2026, 6, 17),
        watchlist_rows,
        candle_rows,
        now=datetime(2026, 6, 17, 14, 50, tzinfo=TAIPEI_TZ),
    )

    assert history["as_of_date"] == "2026-06-17"
    assert history["snapshots"][0]["cutoff"] == "08:45"
    assert history["snapshots"][0]["rows"][0]["stock_id"] == "2317"
    assert history["snapshots"][0]["rows"][0]["rank"] == 1
    assert history["snapshots"][1]["cutoff"] == "09:00"
    tsmc = [row for row in history["snapshots"][1]["rows"] if row["stock_id"] == "2330"][0]
    assert tsmc["volume"] == 150
    assert tsmc["spread_per"] == 4.0


def test_build_near_month_tickers_from_watchlist_symbols_ignores_numeric_months():
    stock_futures = pd.DataFrame(
        [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "futures_id": "CD",
                "finmind_futures_id": "CDF",
                "fugle_product_id": "CDF",
                "contract_size": 2000,
            }
        ]
    )
    watchlist_rows = [
        {"stock_id": "2330", "contract_date": "202607", "futures_id": "CDF"},
        {"stock_id": "2330", "contract_date": "CDFG6", "futures_id": "CDF"},
    ]

    tickers = build_near_month_tickers_from_watchlist_symbols(watchlist_rows, stock_futures)

    assert list(tickers["symbol"]) == ["CDFG6"]
    assert tickers.iloc[0]["fugle_product_id"] == "CDF"


def test_fetch_fugle_near_month_candles_omits_regular_session(monkeypatch):
    stock_futures = pd.DataFrame(
        [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "futures_id": "CDF",
                "finmind_futures_id": "CDF",
                "fugle_product_id": "CDF",
                "contract_size": 2000,
            }
        ]
    )
    near_month_tickers = pd.DataFrame([{"symbol": "CDFG6", "fugle_product_id": "CDF", "endDate": "2026-07-15"}])
    captured = {}

    def fake_fetch_candles(symbol, api_key, timeframe="5", session=None, timeout=30):
        captured["symbol"] = symbol
        captured["session"] = session
        return {
            "data": [
                {
                    "date": "2026-06-17T08:45:00.000+08:00",
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 1,
                }
            ]
        }

    monkeypatch.setattr("src.web_dashboard.fetch_fugle_futopt_candles", fake_fetch_candles)

    candles = fetch_fugle_near_month_candles("token", near_month_tickers, stock_futures)

    assert captured["symbol"] == "CDFG6"
    assert captured["session"] is None
    assert len(candles) == 1
    assert candles.attrs["fetch_error_count"] == 0


def test_rebuild_trajectory_falls_back_to_product_tickers(monkeypatch):
    snapshot = replace(_minimal_snapshot(), as_of_date="2026-06-17")
    snapshot.watchlist_rows.append(
        {
            "stock_id": "2330",
            "stock_name": "台積電",
            "finmind_futures_id": "CDF",
            "futures_id": "CDF",
            "contract_type_label": "大型",
            "close": 105,
            "spread": 5,
            "contract_date": "202607",
        }
    )
    products = pd.DataFrame(
        [
            {
                "symbol": "CDF",
                "underlyingSymbol": "2330",
                "name": "台積電期貨",
                "type": "FUTURE",
                "contractType": "S",
                "contractSize": 2000,
            }
        ]
    )
    product_tickers = pd.DataFrame([{"symbol": "CDFG6", "endDate": "2026-07-15"}])
    candles = pd.DataFrame(
        [
            {"date": "2026-06-17T08:45:00+08:00", "symbol": "CDFG6", "close": 102, "volume": 100},
            {"date": "2026-06-17T08:50:00+08:00", "symbol": "CDFG6", "close": 103, "volume": 50},
            {"date": "2026-06-17T08:55:00+08:00", "symbol": "CDFG6", "close": 104, "volume": 25},
        ]
    )
    cache_dir = os.path.join(os.getcwd(), "data", "test-intraday-trajectory-{}".format(uuid.uuid4().hex))
    try:
        trajectory_cache = IntradayTrajectoryCache(cache_dir=cache_dir)
        monkeypatch.setenv("FUGLE_API_KEY", "test-key")
        monkeypatch.setattr("src.web_dashboard.dashboard_cache.get_snapshot", lambda **kwargs: snapshot)
        monkeypatch.setattr("src.web_dashboard.fetch_fugle_stock_futures_products", lambda token, timeout=60: products)
        monkeypatch.setattr("src.web_dashboard.fetch_fugle_stock_futures_tickers", lambda token, timeout=60: pd.DataFrame())
        monkeypatch.setattr(
            "src.web_dashboard.fetch_fugle_stock_futures_tickers_by_products",
            lambda token, stock_futures, product_ids=None, timeout=60: product_tickers,
        )
        monkeypatch.setattr(
            "src.web_dashboard.fetch_fugle_near_month_candles",
            lambda token, near_month_tickers, stock_futures, timeframe="5", session="REGULAR", timeout=60: candles,
        )
        monkeypatch.setattr("src.web_dashboard.intraday_trajectory_cache", trajectory_cache)

        from src.web_dashboard import rebuild_intraday_trajectory_from_fugle

        history = rebuild_intraday_trajectory_from_fugle(as_of_date=date(2026, 6, 17))

        assert history["cache_hit"] is True
        assert history["snapshots"][0]["cutoff"] == "08:45"
        assert history["snapshots"][0]["rows"][0]["stock_id"] == "2330"
        assert history["source"]["fugle_ticker_rows"] == 1
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_admin_refresh_requires_configured_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_REFRESH_TOKEN", "")
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "")

    status, _, body = build_dashboard_response(
        "/api/admin/refresh",
        headers={"Authorization": "Bearer test-token"},
    )
    payload = json.loads(body.decode("utf-8"))

    assert status == 503
    assert payload["status"] == "misconfigured"


def test_admin_refresh_rejects_missing_or_wrong_bearer(monkeypatch):
    monkeypatch.setenv("DASHBOARD_REFRESH_TOKEN", "secret-token")

    status, _, body = build_dashboard_response("/api/admin/refresh")
    payload = json.loads(body.decode("utf-8"))
    assert status == 401
    assert payload["status"] == "unauthorized"

    status, _, body = build_dashboard_response(
        "/api/admin/refresh",
        headers={"Authorization": "Bearer wrong-token"},
    )
    payload = json.loads(body.decode("utf-8"))
    assert status == 401
    assert payload["status"] == "unauthorized"


def test_admin_refresh_intraday_snapshot_uses_bearer_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_REFRESH_TOKEN", "secret-token")
    captured = {}
    snapshot = _minimal_snapshot(min_atr_percent=4.0)
    snapshot.source["cache_kind"] = "intraday"
    snapshot.source["snapshot_stage"] = "intraday"
    snapshot.source["final_ready"] = False

    def fake_get_snapshot(force_refresh=False, criteria=None, as_of_date=None):
        captured["force_refresh"] = force_refresh
        captured["criteria"] = criteria
        captured["as_of_date"] = as_of_date
        return snapshot

    class FakeTrajectoryCache:
        def append_snapshot(self, snapshot):
            captured["snapshot"] = snapshot
            return {
                "version": 1,
                "as_of_date": snapshot.as_of_date,
                "snapshots": [{"cutoff": "09:00", "rows": [{"stock_id": "2330"}]}],
                "cache_hit": True,
            }

    monkeypatch.setattr("src.web_dashboard.dashboard_cache.get_snapshot", fake_get_snapshot)
    monkeypatch.setattr("src.web_dashboard.intraday_trajectory_cache", FakeTrajectoryCache())

    status, _, body = build_dashboard_response(
        "/api/admin/refresh",
        "mode=intraday_snapshot&min_atr_percent=4&as_of=2026-06-17",
        headers={"Authorization": "Bearer secret-token"},
    )
    payload = json.loads(body.decode("utf-8"))

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["mode"] == "intraday_snapshot"
    assert payload["trajectory_snapshot_count"] == 1
    assert captured["force_refresh"] is True
    assert captured["criteria"].min_atr_percent == 4.0
    assert captured["as_of_date"] == date(2026, 6, 17)


def test_admin_refresh_rebuild_trajectory_uses_bearer_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_REFRESH_TOKEN", "secret-token")
    captured = {}

    def fake_rebuild(as_of_date=None, criteria=None, force_snapshot=False, timeout=60):
        captured["as_of_date"] = as_of_date
        captured["criteria"] = criteria
        captured["force_snapshot"] = force_snapshot
        return {
            "version": 1,
            "as_of_date": "2026-06-17",
            "updated_at": "2026-06-17 14:50:00 GMT+8",
            "snapshots": [{"cutoff": "13:30", "rows": [{"stock_id": "2330"}]}],
            "cache_hit": True,
            "source": {"type": "test"},
        }

    monkeypatch.setattr("src.web_dashboard.rebuild_intraday_trajectory_from_fugle", fake_rebuild)

    status, _, body = build_dashboard_response(
        "/api/admin/refresh",
        "mode=rebuild_trajectory&as_of=2026-06-17&refresh_snapshot=1&min_atr_percent=3",
        headers={"Authorization": "Bearer secret-token"},
    )
    payload = json.loads(body.decode("utf-8"))

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["mode"] == "rebuild_trajectory"
    assert payload["trajectory_snapshot_count"] == 1
    assert captured["as_of_date"] == date(2026, 6, 17)
    assert captured["force_snapshot"] is True


def test_contract_metadata_cache_round_trips_fugle_products():
    cache_dir = os.path.join(os.getcwd(), "data", "test-contract-metadata-{}".format(uuid.uuid4().hex))
    try:
        metadata_cache = ContractMetadataCache(cache_dir=cache_dir)
        products = pd.DataFrame(
            [
                {
                    "symbol": "CDF",
                    "type": "FUTURE",
                    "contractType": "S",
                    "statusCode": "N",
                    "name": "TSMC futures",
                    "underlyingSymbol": "2330",
                    "contractSize": 2000,
                },
                {
                    "symbol": "QFF",
                    "type": "FUTURE",
                    "contractType": "S",
                    "statusCode": "N",
                    "name": "Small TSMC futures",
                    "underlyingSymbol": "2330",
                    "contractSize": 100,
                },
            ]
        )
        stock_futures = build_stock_futures_contract_map_from_fugle(products)
        contracts = build_contracts_from_stock_futures(stock_futures)

        metadata_cache.store(
            "Fugle intraday products",
            stock_futures,
            contracts,
            products,
            now=datetime(2026, 6, 16, 9, 0, tzinfo=TAIPEI_TZ),
        )
        cached = metadata_cache.read(now=datetime(2026, 6, 16, 10, 0, tzinfo=TAIPEI_TZ))

        assert cached is not None
        assert cached["source"] == "Fugle intraday products"
        assert cached["is_fresh"] is True
        assert len(cached["stock_futures"]) == 2
        assert len(cached["contracts"]) == 2
        assert len(cached["fugle_products"]) == 2
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_stock_industry_map_groups_and_enriches_watchlist():
    stock_info = pd.DataFrame(
        [
            {
                "date": "None",
                "stock_id": "2330",
                "stock_name": "台積電",
                "industry_category": "半導體業",
                "type": "twse",
            },
            {
                "date": "2026-06-15",
                "stock_id": "1301",
                "stock_name": "台塑",
                "industry_category": "塑膠工業",
                "type": "twse",
            },
            {
                "date": "2026-06-15",
                "stock_id": "9999",
                "stock_name": "未分類",
                "industry_category": "",
                "type": "twse",
            },
        ]
    )

    industry_map = build_stock_industry_map(stock_info)
    enriched = enrich_watchlist_records_with_industry(
        [
            {"stock_id": "2330", "stock_name": "台積電"},
            {"stock_id": "1301", "stock_name": "台塑"},
            {"stock_id": "9999", "stock_name": "未分類"},
            {"stock_id": "0000", "stock_name": "Missing"},
        ],
        industry_map,
    )

    assert industry_map["2330"]["industry_group"] == "電子股"
    assert industry_map["2330"]["date"] == ""
    assert industry_map["1301"]["industry_group"] == "非電子"
    assert industry_group_from_category("光電業") == "電子股"
    assert enriched[0]["industry_category"] == "半導體業"
    assert enriched[1]["industry_group"] == "非電子"
    assert enriched[2]["industry_group"] == "未分類"
    assert enriched[3]["industry_group"] == "未分類"


def test_stock_industry_map_cache_round_trips():
    cache_dir = os.path.join(os.getcwd(), "data", "test-stock-industry-map-{}".format(uuid.uuid4().hex))
    try:
        industry_cache = StockIndustryMapCache(cache_dir=cache_dir)
        stock_info = pd.DataFrame(
            [
                {
                    "date": "2026-06-15",
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "industry_category": "半導體業",
                    "type": "twse",
                }
            ]
        )
        industry_map = build_stock_industry_map(stock_info)

        industry_cache.store(
            source="FinMind TaiwanStockInfo",
            stock_info=stock_info,
            industry_map=industry_map,
            now=datetime(2026, 6, 16, 9, 0, tzinfo=TAIPEI_TZ),
        )
        cached = industry_cache.read(now=datetime(2026, 6, 16, 10, 0, tzinfo=TAIPEI_TZ))

        assert cached is not None
        assert cached["source"] == "FinMind TaiwanStockInfo"
        assert cached["cache_hit"] is True
        assert cached["is_fresh"] is True
        assert cached["raw_rows"] == 1
        assert cached["map"]["2330"]["industry_category"] == "半導體業"
        assert cached["map"]["2330"]["industry_group"] == "電子股"
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_live_snapshot_uses_cached_contract_metadata_before_taifex(monkeypatch):
    cache_dir = os.path.join(os.getcwd(), "data", "test-contract-metadata-{}".format(uuid.uuid4().hex))
    try:
        metadata_cache = ContractMetadataCache(cache_dir=cache_dir)
        products = pd.DataFrame(
            [
                {
                    "symbol": "CDF",
                    "type": "FUTURE",
                    "contractType": "S",
                    "statusCode": "N",
                    "name": "TSMC futures",
                    "underlyingSymbol": "2330",
                    "contractSize": 2000,
                },
                {
                    "symbol": "QFF",
                    "type": "FUTURE",
                    "contractType": "S",
                    "statusCode": "N",
                    "name": "Small TSMC futures",
                    "underlyingSymbol": "2330",
                    "contractSize": 100,
                },
            ]
        )
        stock_futures = build_stock_futures_contract_map_from_fugle(products)
        metadata_cache.store(
            "Fugle intraday products",
            stock_futures,
            build_contracts_from_stock_futures(stock_futures),
            products,
            now=datetime(2026, 6, 10, 9, 0, tzinfo=TAIPEI_TZ),
        )
        all_dates = [value.date() for value in pd.bdate_range("2026-05-18", "2026-06-16")]

        def fail_taifex_fetch(*args, **kwargs):
            raise AssertionError("cached contract metadata should be used before TAIFEX")

        monkeypatch.setattr("src.web_dashboard.contract_metadata_cache", metadata_cache)
        monkeypatch.setattr("src.web_dashboard.get_finmind_token", lambda: "finmind-token")
        monkeypatch.setattr("src.web_dashboard.get_fugle_token", lambda: "fugle-token")
        monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 16, 10, 0, tzinfo=TAIPEI_TZ))
        monkeypatch.setattr("src.web_dashboard.fetch_fugle_stock_futures_products", lambda token, timeout=60: pd.DataFrame())
        monkeypatch.setattr("src.web_dashboard.fetch_fugle_stock_futures_tickers", lambda token, timeout=60: pd.DataFrame())
        monkeypatch.setattr("src.web_dashboard.fetch_fugle_near_month_quotes", lambda token, near_month_tickers, stock_futures, timeout=60: pd.DataFrame())
        monkeypatch.setattr("src.web_dashboard.fetch_taifex_stock_futures_contracts", fail_taifex_fetch)
        monkeypatch.setattr("src.web_dashboard.fetch_finmind_futopt_daily_info", lambda token, timeout=60: (_ for _ in ()).throw(AssertionError("FinMind product metadata should not be needed")))
        monkeypatch.setattr("src.web_dashboard.fetch_recent_finmind_trading_dates", lambda token, end_day, count, timeout=60: all_dates[-count:])

        def fake_futures_history(token, trading_dates, required_days, timeout=60):
            used_dates = trading_dates[-required_days:]
            return pd.DataFrame(_futures_daily_rows(used_dates)), used_dates

        monkeypatch.setattr("src.web_dashboard.fetch_recent_finmind_futures_daily_history", fake_futures_history)

        snapshot = build_daily_pool_snapshot(
            end_date=date(2026, 6, 16),
            criteria=StockPoolCriteria(min_atr_percent=2.0),
            timeout=1,
            trading_date_buffer=5,
        )

        assert snapshot.source["contract_source"] == "Cached Fugle intraday products"
        assert snapshot.source["contract_metadata_cache_hit"] is True
        assert snapshot.source["realtime_quote_enabled"] is True
        assert snapshot.watchlist_rows
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_dashboard_cache_returns_stale_intraday_snapshot_when_refresh_fails(monkeypatch):
    cache_dir = os.path.join(os.getcwd(), "data", "test-dashboard-cache-{}".format(uuid.uuid4().hex))
    try:
        criteria = StockPoolCriteria(min_atr_percent=4.0)
        cache = DashboardCache(cache_dir=cache_dir)
        snapshot = _minimal_snapshot(criteria.min_atr_percent)
        snapshot.source["cache_kind"] = "intraday"
        snapshot.source["snapshot_stage"] = "intraday"
        snapshot.source["realtime_quote_enabled"] = True
        key = cache._criteria_key(criteria, date(2026, 6, 16), "intraday")
        cache.snapshots[key] = snapshot
        cache.loaded_at[key] = datetime(2026, 6, 16, 9, 0, tzinfo=TAIPEI_TZ)

        monkeypatch.setenv("DASHBOARD_CACHE_SECONDS", "1")
        monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 16, 10, 0, tzinfo=TAIPEI_TZ))
        monkeypatch.setattr(cache, "_read_disk_snapshot", lambda *args, **kwargs: (None, None))
        monkeypatch.setattr("src.web_dashboard.build_daily_pool_snapshot", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("temporary upstream failure")))

        result = cache.get_snapshot(criteria=criteria, as_of_date=date(2026, 6, 16))

        assert result.source["stale_cache_fallback"] is True
        assert "temporary upstream failure" in result.source["stale_cache_reason"]
        assert result.source["cache_kind"] == "intraday"
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_dashboard_cache_serves_expired_snapshot_outside_trading(monkeypatch):
    criteria = StockPoolCriteria(min_atr_percent=4.0)
    cache = DashboardCache(cache_dir="unused-cache-dir")
    snapshot = _minimal_snapshot(criteria.min_atr_percent)
    key = cache._criteria_key(criteria, date(2026, 6, 15))
    cache.snapshots[key] = snapshot
    cache.loaded_at[key] = datetime(2026, 6, 15, 14, 0, tzinfo=TAIPEI_TZ)

    def fail_build_snapshot(**kwargs):
        raise AssertionError("non-trading requests should reuse cached snapshots")

    monkeypatch.setenv("DASHBOARD_CACHE_SECONDS", "1")
    monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 15, 20, 0, tzinfo=TAIPEI_TZ))
    monkeypatch.setattr("src.web_dashboard.build_daily_pool_snapshot", fail_build_snapshot)

    assert cache.get_snapshot(criteria=criteria, as_of_date=date(2026, 6, 15)) is snapshot


def test_dashboard_cache_uses_shared_30_second_live_snapshot(monkeypatch):
    criteria = StockPoolCriteria(min_atr_percent=4.0)
    cache = DashboardCache(cache_dir="unused-cache-dir")
    key = cache._criteria_key(criteria, date(2026, 6, 16), "intraday")
    cached_snapshot = _minimal_snapshot(criteria.min_atr_percent)
    fresh_snapshot = _minimal_snapshot(criteria.min_atr_percent)
    for snapshot in (cached_snapshot, fresh_snapshot):
        snapshot.source["cache_kind"] = "intraday"
        snapshot.source["snapshot_stage"] = "intraday"
        snapshot.source["final_ready"] = False
    cache.snapshots[key] = cached_snapshot
    cache.loaded_at[key] = datetime(2026, 6, 16, 9, 59, 31, tzinfo=TAIPEI_TZ)

    monkeypatch.delenv("DASHBOARD_CACHE_SECONDS", raising=False)
    monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 16, 10, 0, 0, tzinfo=TAIPEI_TZ))
    monkeypatch.setattr(cache, "_write_disk_snapshot", lambda *args, **kwargs: None)

    assert cache.get_snapshot(criteria=criteria, as_of_date=date(2026, 6, 16)) is cached_snapshot

    cache.loaded_at[key] = datetime(2026, 6, 16, 9, 59, 29, tzinfo=TAIPEI_TZ)
    monkeypatch.setattr("src.web_dashboard.build_daily_pool_snapshot", lambda **kwargs: fresh_snapshot)

    assert cache.get_snapshot(criteria=criteria, as_of_date=date(2026, 6, 16)) is fresh_snapshot


def test_dashboard_cache_migrates_pool_snapshot_missing_spread(monkeypatch):
    criteria = StockPoolCriteria(min_atr_percent=4.0)
    cache = DashboardCache(cache_dir="unused-cache-dir")
    key = cache._criteria_key(criteria, date(2026, 6, 16), "intraday")
    old_snapshot = DashboardSnapshot(
        generated_at="2026-06-16 09:59:45 GMT+8",
        as_of_date="2026-06-16",
        row_count=1,
        active_row_count=1,
        new_entry_count=0,
        watchlist_count=0,
        volume_window="-",
        atr_window="-",
        criteria={"min_atr_percent": 4.0},
        rows=[
            {
                "stock_id": "2330",
                "finmind_futures_id": "QFF",
                "close": 1000.0,
                "spread_per": 1.0,
                "volume_rank_5d": [9, 8, 7, 6, 5],
            }
        ],
        active_rows=[
            {
                "stock_id": "2303",
                "finmind_futures_id": "CCF",
                "close": 101.0,
                "spread_per": 1.0,
                "volume_rank_5d": [30, 25, 22, 18, 12],
            }
        ],
        new_entry_rows=[],
        watchlist_rows=[
            {
                "stock_id": "2330",
                "finmind_futures_id": "QFF",
                "futures_id": "QF",
                "spread": 12.5,
                "industry_category": "半導體業",
                "industry_group": "電子股",
            }
        ],
        source={},
    )
    cache.snapshots[key] = old_snapshot
    cache.loaded_at[key] = datetime(2026, 6, 16, 9, 59, 45, tzinfo=TAIPEI_TZ)

    def fail_build_snapshot(**kwargs):
        raise AssertionError("compatible migrated snapshots should not rebuild")

    monkeypatch.delenv("DASHBOARD_CACHE_SECONDS", raising=False)
    monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 16, 10, 0, 0, tzinfo=TAIPEI_TZ))
    monkeypatch.setattr(cache, "_read_disk_snapshot", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(cache, "_write_disk_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.web_dashboard.build_daily_pool_snapshot", fail_build_snapshot)

    snapshot = cache.get_snapshot(criteria=criteria, as_of_date=date(2026, 6, 16))

    assert snapshot.rows[0]["spread"] == 12.5
    assert snapshot.active_rows[0]["spread"] == 1.0
    assert snapshot.rows[0]["volume_rank_5d"] == [9, 8, 7, 6, 5]
    assert snapshot.active_rows[0]["volume_rank_5d"] == [30, 25, 22, 18, 12]
    assert snapshot.source["cache_schema_version"] == 5
    assert snapshot.source["cache_kind"] == "intraday"


def test_dashboard_cache_rebuilds_snapshot_missing_rank_sequence(monkeypatch):
    criteria = StockPoolCriteria(min_atr_percent=4.0)
    cache = DashboardCache(cache_dir="unused-cache-dir")
    key = cache._criteria_key(criteria, date(2026, 6, 16), "intraday")
    old_snapshot = _minimal_snapshot(criteria.min_atr_percent)
    old_snapshot.rows.append(
        {
            "stock_id": "2330",
            "finmind_futures_id": "QFF",
            "close": 1000.0,
            "spread": 12.5,
            "spread_per": 1.0,
            "best_volume_rank_5d": 2,
            "worst_volume_rank_5d": 18,
        }
    )
    cache.snapshots[key] = old_snapshot
    cache.loaded_at[key] = datetime(2026, 6, 16, 10, 0, 0, tzinfo=TAIPEI_TZ)
    fresh_snapshot = _minimal_snapshot(criteria.min_atr_percent)
    fresh_snapshot.rows.append(
        {
            "stock_id": "2330",
            "finmind_futures_id": "QFF",
            "close": 1000.0,
            "spread": 12.5,
            "spread_per": 1.0,
            "volume_rank_5d": [18, 12, 8, 5, 2],
        }
    )

    monkeypatch.delenv("DASHBOARD_CACHE_SECONDS", raising=False)
    monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 16, 10, 0, 30, tzinfo=TAIPEI_TZ))
    monkeypatch.setattr(cache, "_read_disk_snapshot", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(cache, "_write_disk_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.web_dashboard.build_daily_pool_snapshot", lambda **kwargs: fresh_snapshot)

    snapshot = cache.get_snapshot(criteria=criteria, as_of_date=date(2026, 6, 16))

    assert snapshot.rows[0]["volume_rank_5d"] == [18, 12, 8, 5, 2]
    assert snapshot.source["cache_kind"] == "intraday"


def test_dashboard_cache_rebuilds_snapshot_with_unavailable_industry_map(monkeypatch):
    criteria = StockPoolCriteria(min_atr_percent=4.0)
    cache = DashboardCache(cache_dir="unused-cache-dir")
    key = cache._criteria_key(criteria, date(2026, 6, 16), "intraday")
    old_snapshot = _minimal_snapshot(criteria.min_atr_percent)
    old_snapshot.watchlist_rows.append(
        {
            "stock_id": "2330",
            "stock_name": "台積電",
            "volume": 1000,
            "spread_per": 1.0,
            "industry_category": "",
            "industry_group": "未分類",
        }
    )
    old_snapshot.source["industry_map_source"] = "unavailable"
    old_snapshot.source["industry_map_rows"] = 0
    cache.snapshots[key] = old_snapshot
    cache.loaded_at[key] = datetime(2026, 6, 16, 10, 0, 0, tzinfo=TAIPEI_TZ)

    fresh_snapshot = _minimal_snapshot(criteria.min_atr_percent)
    fresh_snapshot.watchlist_rows.append(
        {
            "stock_id": "2330",
            "stock_name": "台積電",
            "volume": 1000,
            "spread_per": 1.0,
            "industry_category": "半導體業",
            "industry_group": "電子股",
        }
    )

    monkeypatch.delenv("DASHBOARD_CACHE_SECONDS", raising=False)
    monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 16, 10, 0, 30, tzinfo=TAIPEI_TZ))
    monkeypatch.setattr(cache, "_read_disk_snapshot", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(cache, "_write_disk_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.web_dashboard.build_daily_pool_snapshot", lambda **kwargs: fresh_snapshot)

    snapshot = cache.get_snapshot(criteria=criteria, as_of_date=date(2026, 6, 16))

    assert snapshot.watchlist_rows[0]["industry_category"] == "半導體業"
    assert snapshot.watchlist_rows[0]["industry_group"] == "電子股"
    assert snapshot.source["cache_kind"] == "intraday"


def test_dashboard_cache_refreshes_today_snapshot_at_closing_checkpoint(monkeypatch):
    criteria = StockPoolCriteria(min_atr_percent=4.0)
    cache = DashboardCache(cache_dir="unused-cache-dir")
    key = cache._criteria_key(criteria, date(2026, 6, 16))
    cached_snapshot = _minimal_snapshot(criteria.min_atr_percent)
    fresh_snapshot = _minimal_snapshot(criteria.min_atr_percent)
    cache.snapshots[key] = cached_snapshot
    cache.loaded_at[key] = datetime(2026, 6, 16, 13, 59, 59, tzinfo=TAIPEI_TZ)
    build_calls = []

    def fake_build_snapshot(**kwargs):
        build_calls.append(kwargs)
        return fresh_snapshot

    def fail_build_snapshot(**kwargs):
        raise AssertionError("closing checkpoint should refresh only once per fresh snapshot")

    monkeypatch.delenv("DASHBOARD_CACHE_SECONDS", raising=False)
    monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 16, 14, 0, 0, tzinfo=TAIPEI_TZ))
    monkeypatch.setattr(cache, "_read_disk_snapshot", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(cache, "_write_disk_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.web_dashboard.build_daily_pool_snapshot", fake_build_snapshot)

    assert cache.get_snapshot(criteria=criteria, as_of_date=date(2026, 6, 16)) is fresh_snapshot
    assert build_calls

    monkeypatch.setattr("src.web_dashboard.build_daily_pool_snapshot", fail_build_snapshot)

    assert cache.get_snapshot(criteria=criteria, as_of_date=date(2026, 6, 16)) is fresh_snapshot


def test_dashboard_cache_ignores_intraday_snapshot_after_close(monkeypatch):
    criteria = StockPoolCriteria(min_atr_percent=4.0)
    cache = DashboardCache(cache_dir="unused-cache-dir")
    intraday_snapshot = _minimal_snapshot(criteria.min_atr_percent)
    intraday_snapshot.source["cache_kind"] = "intraday"
    intraday_snapshot.source["snapshot_stage"] = "intraday"
    intraday_snapshot.source["final_ready"] = False
    intraday_snapshot.source["final_readiness_reason"] = "交易時段使用 Fugle 即時層"
    intraday_key = cache._criteria_key(criteria, date(2026, 6, 16), "intraday")
    cache.snapshots[intraday_key] = intraday_snapshot
    cache.loaded_at[intraday_key] = datetime(2026, 6, 16, 13, 45, tzinfo=TAIPEI_TZ)
    final_pending = _minimal_snapshot(criteria.min_atr_percent)
    final_pending.source["snapshot_stage"] = "final_pending"
    final_pending.source["final_ready"] = False
    final_pending.source["final_readiness_reason"] = "FinMind 今日股票期貨日資料未完整：2 / 100 檔"

    monkeypatch.delenv("DASHBOARD_CACHE_SECONDS", raising=False)
    monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 16, 14, 0, 0, tzinfo=TAIPEI_TZ))
    monkeypatch.setattr(cache, "_read_disk_snapshot", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(cache, "_write_disk_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.web_dashboard.build_daily_pool_snapshot", lambda **kwargs: final_pending)

    snapshot = cache.get_snapshot(criteria=criteria, as_of_date=date(2026, 6, 16))

    assert snapshot.source["snapshot_stage"] == "final_pending"
    assert snapshot.source["final_ready"] is False
    assert snapshot.source["cache_kind"] == "final"
    assert "未完整" in snapshot.source["final_readiness_reason"]


def test_final_readiness_accepts_today_finmind_rows_as_final_even_with_low_coverage(monkeypatch):
    rows = []
    for index in range(100):
        rows.append(
            {
                "date": "2026-06-15",
                "finmind_futures_id": "P{:03d}F".format(index),
            }
        )
    for index in range(2):
        rows.append(
            {
                "date": "2026-06-16",
                "finmind_futures_id": "T{:03d}F".format(index),
            }
        )
    monkeypatch.setenv("DASHBOARD_FINAL_MIN_PRODUCTS", "20")
    monkeypatch.setenv("DASHBOARD_FINAL_MIN_PRODUCT_COVERAGE", "0.7")

    ready, reason, metrics = final_readiness_from_daily_history(
        pd.DataFrame(rows),
        date(2026, 6, 16),
        pd.DataFrame(),
    )

    assert ready is True
    assert "已取得" in reason
    assert metrics["daily_final_product_rows"] == 2
    assert metrics["daily_final_expected_rows"] == 100
    assert metrics["daily_final_low_coverage"] is True


def test_dashboard_cache_key_and_file_include_as_of_date():
    criteria = StockPoolCriteria(min_atr_percent=4.0)
    cache = DashboardCache(cache_dir="cache-dir")

    key = cache._criteria_key(criteria, date(2026, 6, 12))
    path = cache._cache_path(criteria, date(2026, 6, 12))

    assert key[0] == "2026-06-12"
    assert "dashboard_asof2026-06-12" in path
    assert "_final_" in path
    assert key[-1] == "final"
    assert "minatr4" in path


def test_dashboard_cache_default_before_open_serves_previous_final_snapshot(monkeypatch):
    criteria = StockPoolCriteria(min_atr_percent=4.0)
    cache = DashboardCache(cache_dir="unused-cache-dir")
    snapshot = replace(_minimal_snapshot(criteria.min_atr_percent), as_of_date="2026-06-17")
    snapshot.source["effective_as_of_date"] = "2026-06-17"
    key = cache._criteria_key(criteria, date(2026, 6, 17), "final")
    cache.snapshots[key] = snapshot
    cache.loaded_at[key] = datetime(2026, 6, 17, 14, 0, tzinfo=TAIPEI_TZ)

    def fail_build_snapshot(**kwargs):
        raise AssertionError("pre-open default should reuse the previous final snapshot")

    monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 18, 8, 30, tzinfo=TAIPEI_TZ))
    monkeypatch.setattr("src.web_dashboard.build_daily_pool_snapshot", fail_build_snapshot)

    assert cache.get_snapshot(criteria=criteria) is snapshot


def test_dashboard_cache_default_at_open_builds_today_intraday(monkeypatch):
    criteria = StockPoolCriteria(min_atr_percent=4.0)
    cache = DashboardCache(cache_dir="unused-cache-dir")
    fresh_snapshot = _minimal_snapshot(criteria.min_atr_percent)
    build_calls = []

    def fake_build_snapshot(**kwargs):
        build_calls.append(kwargs)
        return fresh_snapshot

    monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 18, 8, 45, tzinfo=TAIPEI_TZ))
    monkeypatch.setattr(cache, "_read_disk_snapshot", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(cache, "_write_disk_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.web_dashboard.build_daily_pool_snapshot", fake_build_snapshot)

    snapshot = cache.get_snapshot(criteria=criteria)

    assert snapshot.as_of_date == fresh_snapshot.as_of_date
    assert build_calls[0]["end_date"] == date(2026, 6, 18)
    assert snapshot.source["cache_kind"] == "intraday"


def test_dashboard_cache_reads_legacy_cache_filename():
    criteria = StockPoolCriteria(min_atr_percent=4.0)
    cache_dir = os.path.join(os.getcwd(), "data", "test-cache-{}".format(uuid.uuid4().hex))
    os.makedirs(cache_dir, exist_ok=True)
    try:
        cache = DashboardCache(cache_dir=cache_dir)
        snapshot = _minimal_snapshot(criteria.min_atr_percent)
        legacy_path = cache._legacy_cache_path(criteria, date(2026, 6, 12))
        with open(legacy_path, "w", encoding="utf-8") as cache_file:
            json.dump(snapshot.__dict__, cache_file, ensure_ascii=False)

        disk_snapshot, loaded_at = cache._read_disk_snapshot(criteria, date(2026, 6, 12), "intraday")

        assert disk_snapshot is not None
        assert disk_snapshot.as_of_date == snapshot.as_of_date
        assert loaded_at is not None
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_historical_snapshot_rolls_non_trading_date_back_and_skips_fugle(monkeypatch):
    all_dates = [value.date() for value in pd.bdate_range("2026-04-27", "2026-06-12")]
    captured = {}

    def fail_fugle_fetch(*args, **kwargs):
        raise AssertionError("historical snapshots should not call Fugle APIs")

    def fake_trading_dates(token, end_day, count, timeout=60):
        captured["requested_end_day"] = end_day
        assert end_day == date(2026, 6, 14)
        return all_dates[-count:]

    def fake_futures_history(token, trading_dates, required_days, timeout=60):
        captured["history_max_date"] = max(trading_dates)
        assert max(trading_dates) == date(2026, 6, 12)
        used_dates = trading_dates[-required_days:]
        return pd.DataFrame(_futures_daily_rows(used_dates)), used_dates

    monkeypatch.setattr("src.web_dashboard.get_finmind_token", lambda: "finmind-token")
    monkeypatch.setattr("src.web_dashboard.get_fugle_token", lambda: "fugle-token")
    monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 15, 10, 0, tzinfo=TAIPEI_TZ))
    monkeypatch.setattr("src.web_dashboard.fetch_fugle_stock_futures_products", fail_fugle_fetch)
    monkeypatch.setattr("src.web_dashboard.fetch_fugle_stock_futures_tickers", fail_fugle_fetch)
    monkeypatch.setattr("src.web_dashboard.fetch_fugle_near_month_quotes", fail_fugle_fetch)
    monkeypatch.setattr("src.web_dashboard.fetch_taifex_stock_futures_contracts", lambda timeout=60: _stock_futures_contract_rows())
    monkeypatch.setattr("src.web_dashboard.fetch_finmind_futopt_daily_info", lambda token, timeout=60: _stock_futures_product_info())
    monkeypatch.setattr("src.web_dashboard.fetch_recent_finmind_trading_dates", fake_trading_dates)
    monkeypatch.setattr("src.web_dashboard.fetch_recent_finmind_futures_daily_history", fake_futures_history)

    snapshot = build_daily_pool_snapshot(
        end_date=date(2026, 6, 14),
        criteria=StockPoolCriteria(min_atr_percent=2.0),
        timeout=1,
        trading_date_buffer=5,
    )

    assert captured["requested_end_day"] == date(2026, 6, 14)
    assert captured["history_max_date"] == date(2026, 6, 12)
    assert snapshot.as_of_date == "2026-06-12"
    assert snapshot.source["requested_as_of_date"] == "2026-06-14"
    assert snapshot.source["effective_as_of_date"] == "2026-06-12"
    assert snapshot.source["historical_mode"] is True
    assert snapshot.source["realtime_quote_enabled"] is False
    assert snapshot.source["fugle_quote_rows"] == 0
    assert snapshot.source["futures_volume_source"] == "FinMind TaiwanFuturesDaily"
    assert {row["date"] for row in snapshot.rows + snapshot.active_rows} == {"2026-06-12"}


def test_default_snapshot_before_open_uses_previous_trading_day_and_skips_fugle(monkeypatch):
    all_dates = [value.date() for value in pd.bdate_range("2026-04-28", "2026-06-17")]
    captured = {}

    def fail_fugle_fetch(*args, **kwargs):
        raise AssertionError("pre-open default should not call Fugle APIs")

    def fake_trading_dates(token, end_day, count, timeout=60):
        captured["requested_end_day"] = end_day
        assert end_day == date(2026, 6, 17)
        return all_dates[-count:]

    def fake_futures_history(token, trading_dates, required_days, timeout=60):
        captured["history_max_date"] = max(trading_dates)
        assert max(trading_dates) == date(2026, 6, 17)
        used_dates = trading_dates[-required_days:]
        return pd.DataFrame(_futures_daily_rows(used_dates)), used_dates

    monkeypatch.setattr("src.web_dashboard.get_finmind_token", lambda: "finmind-token")
    monkeypatch.setattr("src.web_dashboard.get_fugle_token", lambda: "fugle-token")
    monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 18, 8, 30, tzinfo=TAIPEI_TZ))
    monkeypatch.setattr("src.web_dashboard.fetch_fugle_stock_futures_products", fail_fugle_fetch)
    monkeypatch.setattr("src.web_dashboard.fetch_fugle_stock_futures_tickers", fail_fugle_fetch)
    monkeypatch.setattr("src.web_dashboard.fetch_fugle_near_month_quotes", fail_fugle_fetch)
    monkeypatch.setattr("src.web_dashboard.fetch_taifex_stock_futures_contracts", lambda timeout=60: _stock_futures_contract_rows())
    monkeypatch.setattr("src.web_dashboard.fetch_finmind_futopt_daily_info", lambda token, timeout=60: _stock_futures_product_info())
    monkeypatch.setattr("src.web_dashboard.fetch_recent_finmind_trading_dates", fake_trading_dates)
    monkeypatch.setattr("src.web_dashboard.fetch_recent_finmind_futures_daily_history", fake_futures_history)

    snapshot = build_daily_pool_snapshot(
        criteria=StockPoolCriteria(min_atr_percent=2.0),
        timeout=1,
        trading_date_buffer=5,
    )

    assert captured["requested_end_day"] == date(2026, 6, 17)
    assert captured["history_max_date"] == date(2026, 6, 17)
    assert snapshot.as_of_date == "2026-06-17"
    assert snapshot.source["requested_as_of_date"] == "2026-06-17"
    assert snapshot.source["effective_as_of_date"] == "2026-06-17"
    assert snapshot.source["historical_mode"] is True
    assert snapshot.source["realtime_quote_enabled"] is False
    assert snapshot.source["fugle_quote_rows"] == 0


def test_post_close_quote_window_starts_at_1400_taipei():
    assert is_taipei_post_close_quote_window(datetime(2026, 6, 16, 13, 59, tzinfo=TAIPEI_TZ)) is False
    assert is_taipei_post_close_quote_window(datetime(2026, 6, 16, 14, 0, tzinfo=TAIPEI_TZ)) is True
    assert is_taipei_post_close_quote_window(datetime(2026, 6, 20, 14, 0, tzinfo=TAIPEI_TZ)) is False


def test_post_close_snapshot_uses_fugle_quote_when_finmind_today_daily_is_missing(monkeypatch):
    all_dates = [value.date() for value in pd.bdate_range("2026-05-18", "2026-06-16")]
    captured = {}

    def fail_taifex_fetch(*args, **kwargs):
        raise AssertionError("Fugle post-close quote path should not need TAIFEX metadata")

    def fake_trading_dates(token, end_day, count, timeout=60):
        assert end_day == date(2026, 6, 16)
        return all_dates[-count:]

    def fake_futures_history(token, trading_dates, required_days, timeout=60):
        used_dates = [value for value in trading_dates if value < date(2026, 6, 16)][-required_days:]
        captured["used_dates"] = used_dates
        return pd.DataFrame(_futures_daily_rows(used_dates)), used_dates

    fugle_products = pd.DataFrame(
        [
            {
                "symbol": "CDF",
                "type": "FUTURE",
                "contractType": "S",
                "statusCode": "N",
                "name": "TSMC futures",
                "underlyingSymbol": "2330",
                "contractSize": 2000,
            },
            {
                "symbol": "QFF",
                "type": "FUTURE",
                "contractType": "S",
                "statusCode": "N",
                "name": "Small TSMC futures",
                "underlyingSymbol": "2330",
                "contractSize": 100,
            },
        ]
    )
    fugle_tickers = pd.DataFrame(
        [
            {"symbol": "CDFF6", "endDate": "2026-06-17"},
            {"symbol": "QFFF6", "endDate": "2026-06-17"},
        ]
    )
    fugle_quotes = pd.DataFrame(
        [
            {
                "date": "2026-06-16",
                "symbol": "CDFF6",
                "total": {"tradeVolume": 3600},
                "openPrice": 2400,
                "highPrice": 2450,
                "lowPrice": 2390,
                "closePrice": 2440,
                "lastPrice": 2440,
                "change": 40,
                "changePercent": 1.67,
            },
            {
                "date": "2026-06-16",
                "symbol": "QFFF6",
                "total": {"tradeVolume": 120},
                "openPrice": 2402,
                "highPrice": 2452,
                "lowPrice": 2392,
                "closePrice": 2442,
                "lastPrice": 2442,
                "change": 42,
                "changePercent": 1.75,
            },
        ]
    )

    monkeypatch.setenv("USE_FUGLE_POST_CLOSE_QUOTE", "1")
    monkeypatch.setattr("src.web_dashboard.get_finmind_token", lambda: "finmind-token")
    monkeypatch.setattr("src.web_dashboard.get_fugle_token", lambda: "fugle-token")
    monkeypatch.setattr("src.web_dashboard.taipei_now", lambda: datetime(2026, 6, 16, 14, 5, tzinfo=TAIPEI_TZ))
    monkeypatch.setattr("src.web_dashboard.fetch_fugle_stock_futures_products", lambda token, timeout=60: fugle_products)
    monkeypatch.setattr("src.web_dashboard.fetch_fugle_stock_futures_tickers", lambda token, timeout=60: fugle_tickers)
    monkeypatch.setattr("src.web_dashboard.fetch_fugle_near_month_quotes", lambda token, near_month_tickers, stock_futures, timeout=60: fugle_quotes)
    monkeypatch.setattr("src.web_dashboard.fetch_taifex_stock_futures_contracts", fail_taifex_fetch)
    monkeypatch.setattr("src.web_dashboard.fetch_finmind_futopt_daily_info", lambda token, timeout=60: pd.DataFrame())
    monkeypatch.setattr("src.web_dashboard.fetch_recent_finmind_trading_dates", fake_trading_dates)
    monkeypatch.setattr("src.web_dashboard.fetch_recent_finmind_futures_daily_history", fake_futures_history)

    snapshot = build_daily_pool_snapshot(
        end_date=date(2026, 6, 16),
        criteria=StockPoolCriteria(min_atr_percent=2.0),
        timeout=1,
        trading_date_buffer=5,
    )

    assert max(captured["used_dates"]) == date(2026, 6, 15)
    assert snapshot.as_of_date == "2026-06-16"
    assert snapshot.source["realtime_quote_enabled"] is False
    assert snapshot.source["snapshot_stage"] == "final"
    assert snapshot.source["final_ready"] is True
    assert snapshot.source["final_readiness_reason"] == "Fugle 近月 quote 收盤快取已取得：2 / 2 檔"
    assert snapshot.source["fugle_quote_mode"] == "post_close_final"
    assert snapshot.source["fugle_post_close_quote_ready"] is True
    assert snapshot.source["fugle_quote_rows"] == 2
    assert snapshot.source["fugle_usable_quote_rows"] == 2
    assert snapshot.source["fugle_quote_history_rows"] == 2
    assert snapshot.source["futures_volume_source"] == "FinMind TaiwanFuturesDaily + Fugle near-month close quote"
    assert snapshot.watchlist_rows[0]["date"] == "2026-06-16"
    assert snapshot.watchlist_rows[0]["volume"] == 3720.0
    assert snapshot.watchlist_rows[0]["close"] == 2440.0


def test_format_taipei_datetime_converts_from_utc():
    generated_at = format_taipei_datetime(datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc))

    assert generated_at == "2026-06-15 12:05:06 GMT+8"


def test_latest_closing_refresh_tracks_taipei_checkpoints():
    trading_day = date(2026, 6, 16)

    assert latest_closing_refresh_at(datetime(2026, 6, 16, 13, 29, tzinfo=TAIPEI_TZ), trading_day) is None
    assert latest_closing_refresh_at(datetime(2026, 6, 16, 13, 30, tzinfo=TAIPEI_TZ), trading_day) == datetime(
        2026,
        6,
        16,
        13,
        30,
        tzinfo=TAIPEI_TZ,
    )
    assert latest_closing_refresh_at(datetime(2026, 6, 16, 13, 44, tzinfo=TAIPEI_TZ), trading_day) == datetime(
        2026,
        6,
        16,
        13,
        30,
        tzinfo=TAIPEI_TZ,
    )
    assert latest_closing_refresh_at(datetime(2026, 6, 16, 13, 45, tzinfo=TAIPEI_TZ), trading_day) == datetime(
        2026,
        6,
        16,
        13,
        45,
        tzinfo=TAIPEI_TZ,
    )
    assert latest_closing_refresh_at(datetime(2026, 6, 16, 14, 0, tzinfo=TAIPEI_TZ), trading_day) == datetime(
        2026,
        6,
        16,
        14,
        0,
        tzinfo=TAIPEI_TZ,
    )


def test_fugle_connection_status_reflects_server_quote_health():
    products = pd.DataFrame([{"symbol": "CDF"}])
    tickers = pd.DataFrame([{"symbol": "CDFF6"}])
    quotes = pd.DataFrame([{"symbol": "CDFF6"}])

    assert fugle_connection_status("token", products, tickers, quotes, trading_session=True) == {
        "status": "online",
        "text": "Fugle quote 正常",
    }
    assert fugle_connection_status("token", products, tickers, pd.DataFrame(), trading_session=True) == {
        "status": "warning",
        "text": "Fugle quote 無資料",
    }
    assert fugle_connection_status("token", pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), trading_session=True) == {
        "status": "offline",
        "text": "Fugle 連線異常",
    }
    assert fugle_connection_status("token", products, tickers, quotes, trading_session=False) == {
        "status": "idle",
        "text": "Fugle 非交易時段待命",
    }


def test_pool_to_records_formats_numeric_values():
    pool = pd.DataFrame(
        [
            {
                "date": "2026-06-12",
                "stock_id": "2330",
                "stock_name": "台積電",
                "futures_id": "QF",
                "close": 1005.123,
                "spread": 12.345,
                "spread_per": 1.234,
                "atr_20": 36.789,
                "atr_20_percent": 3.659,
                "avg_volume_5d": 1234567.4,
                "best_volume_rank_5d": 2,
                "worst_volume_rank_5d": 19,
                "volume_rank_5d": [19, 12, 8, 5, 2],
                "volume_top_days": 5,
                "volume_window": "2026-06-08~2026-06-12",
                "atr_window": "2026-05-16~2026-06-12",
            }
        ]
    )

    records = pool_to_records(pool)

    assert records[0]["close"] == 1005.12
    assert records[0]["spread"] == 12.35
    assert records[0]["spread_per"] == 1.23
    assert records[0]["atr_20"] == 36.79
    assert records[0]["atr_20_percent"] == 3.66
    assert records[0]["avg_volume_5d"] == 1234567.0
    assert records[0]["volume_rank_5d"] == [19.0, 12.0, 8.0, 5.0, 2.0]


def test_build_stock_futures_watchlist_uses_all_stock_futures_volume():
    futures_daily = pd.DataFrame(
        [
            {
                "date": "2026-06-12",
                "futures_id": "CDF",
                "contract_date": "202606",
                "open": 1000,
                "max": 1020,
                "min": 998,
                "close": 1010,
                "spread": 10,
                "spread_per": 1.0,
                "volume": 2000,
                "open_interest": 6000,
                "trading_session": "position",
            },
            {
                "date": "2026-06-12",
                "futures_id": "CDF",
                "contract_date": "202607",
                "open": 1001,
                "max": 1020,
                "min": 998,
                "close": 1011,
                "spread": 11,
                "spread_per": 1.1,
                "volume": 400,
                "open_interest": 1000,
                "trading_session": "position",
            },
            {
                "date": "2026-06-12",
                "futures_id": "QFF",
                "contract_date": "202606",
                "open": 1000,
                "max": 1020,
                "min": 998,
                "close": 1015.123,
                "spread": 15,
                "spread_per": 1.5,
                "volume": 1000,
                "open_interest": 5432,
                "trading_session": "position",
            },
            {
                "date": "2026-06-12",
                "futures_id": "QFF",
                "contract_date": "202607",
                "open": 1001,
                "max": 1020,
                "min": 998,
                "close": 1016,
                "spread": 16,
                "spread_per": 1.6,
                "volume": 234,
                "open_interest": 789,
                "trading_session": "position",
            },
            {
                "date": "2026-06-12",
                "futures_id": "QFF",
                "contract_date": "202606/202607",
                "open": 0,
                "max": 0,
                "min": 0,
                "close": 0,
                "spread": 0,
                "spread_per": 0,
                "volume": 9999,
                "open_interest": 0,
                "trading_session": "position",
            },
        ]
    )
    contracts = pd.DataFrame(
        [
            {"futures_id": "CD", "stock_id": "2330", "stock_name": "台積電", "contract_size": 2000},
            {"futures_id": "QF", "stock_id": "2330", "stock_name": "台積電", "contract_size": 100},
            {"futures_id": "PU", "stock_id": "2454", "stock_name": "聯發科", "contract_size": 2000},
        ]
    )
    product_info = pd.DataFrame(
        [
            {"code": "CDF", "type": "TaiwanFuturesDaily", "name": "台積電期貨"},
            {"code": "QFF", "type": "TaiwanFuturesDaily", "name": "小型台積電期貨"},
            {"code": "PUF", "type": "TaiwanFuturesDaily", "name": "小型聯發科期貨"},
        ]
    )

    stock_futures = build_stock_futures_contract_map(contracts, product_info)
    history = build_stock_futures_volume_history(futures_daily, stock_futures)
    latest = build_stock_futures_latest_quotes(history, stock_futures)
    latest = enrich_latest_quotes_with_daily_prices(latest, futures_daily, stock_futures)
    records = watchlist_to_records(latest)

    assert len(records) == 1
    assert records[0]["stock_id"] == "2330"
    assert records[0]["close"] == 1010.0
    assert records[0]["volume"] == 3634.0
    assert records[0]["open_interest"] == 6000.0
    assert records[0]["finmind_futures_id"] == "CDF, QFF"
    assert records[0]["contract_date"] == "202606"


def test_fugle_quote_volume_overrides_same_day_history():
    stock_futures = pd.DataFrame(
        [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "futures_id": "CD",
                "finmind_futures_id": "CDF",
                "fugle_product_id": "CDF",
                "contract_size": 2000,
            },
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "futures_id": "QF",
                "finmind_futures_id": "QFF",
                "fugle_product_id": "QFF",
                "contract_size": 100,
            },
        ]
    )
    history = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-06-15"),
                "stock_id": "2330",
                "stock_name": "台積電",
                "futures_id": "CD, QF",
                "finmind_futures_id": "CDF, QFF",
                "Trading_Volume": 100,
            }
        ]
    )
    quotes = pd.DataFrame(
        [
            {
                "date": "2026-06-15",
                "symbol": "CDFF6",
                "total": {"tradeVolume": 3546},
                "openPrice": 2310,
                "highPrice": 2388,
                "lowPrice": 2305,
                "closePrice": 2370,
                "lastPrice": 2370,
                "change": 60,
                "changePercent": 2.6,
            },
            {
                "date": "2026-06-15",
                "symbol": "QFFF6",
                "total": {"tradeVolume": 120},
                "openPrice": 2312,
                "highPrice": 2390,
                "lowPrice": 2304,
                "closePrice": 2372,
                "lastPrice": 2372,
                "change": 62,
                "changePercent": 2.68,
            },
        ]
    )
    futures_daily = pd.DataFrame(
        [
            {
                "date": "2026-06-15",
                "futures_id": "CDF",
                "contract_date": "202606",
                "open": 2200,
                "max": 2250,
                "min": 2190,
                "close": 2240,
                "spread": -20,
                "spread_per": -0.88,
                "volume": 100,
                "open_interest": 5000,
                "trading_session": "position",
            },
            {
                "date": "2026-06-15",
                "futures_id": "QFF",
                "contract_date": "202606",
                "open": 2210,
                "max": 2260,
                "min": 2200,
                "close": 2250,
                "spread": -15,
                "spread_per": -0.66,
                "volume": 80,
                "open_interest": 1000,
                "trading_session": "position",
            },
        ]
    )

    realtime_history = build_fugle_quote_volume_history(quotes, stock_futures)
    merged = merge_realtime_volume_history(history, realtime_history)
    latest = build_stock_futures_latest_quotes(merged, stock_futures, quotes)
    latest = enrich_latest_quotes_with_daily_prices(latest, futures_daily, stock_futures)
    records = watchlist_to_records(latest)

    assert dict(zip(realtime_history["finmind_futures_id"], realtime_history["Trading_Volume"])) == {
        "CDF": 3546,
        "QFF": 120,
    }
    assert records[0]["date"] == "2026-06-15"
    assert records[0]["volume"] == 3666.0
    assert records[0]["open"] == 2310.0
    assert records[0]["high"] == 2388.0
    assert records[0]["low"] == 2305.0
    assert records[0]["close"] == 2370.0
    assert records[0]["spread"] == 60.0
    assert records[0]["spread_per"] == 2.6
    assert records[0]["contract_type_label"] == "大型, 小型"
    assert records[0]["source"] == "Fugle near-month quote"


def test_fugle_tickers_select_near_and_next_month_by_end_date():
    products = pd.DataFrame(
        [
            {
                "symbol": "CDF",
                "type": "FUTURE",
                "contractType": "S",
                "statusCode": "N",
                "name": "台積電期貨",
                "underlyingSymbol": "2330",
                "contractSize": 2000,
            }
        ]
    )
    tickers = pd.DataFrame(
        [
            {"symbol": "CDFI6", "endDate": "2026-09-16"},
            {"symbol": "CDFF6", "endDate": "2026-06-17"},
            {"symbol": "CDFG6", "endDate": "2026-07-15"},
        ]
    )

    stock_futures = build_stock_futures_contract_map_from_fugle(products)
    all_tickers, near_tickers = add_fugle_contract_months(tickers, stock_futures)

    assert list(near_tickers["symbol"]) == ["CDFF6"]
    buckets = dict(zip(all_tickers["symbol"], all_tickers["month_bucket"]))
    assert buckets["CDFF6"] == "near"
    assert buckets["CDFG6"] == "next"
    assert buckets["CDFI6"] == "other"


def test_realtime_quote_volume_uses_near_month_only():
    stock_futures = pd.DataFrame(
        [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "futures_id": "CD",
                "finmind_futures_id": "CDF",
                "fugle_product_id": "CDF",
                "contract_size": 2000,
            }
        ]
    )
    tickers = pd.DataFrame(
        [
            {"symbol": "CDFF6", "endDate": "2026-06-17"},
            {"symbol": "CDFG6", "endDate": "2026-07-15"},
        ]
    )
    _, near_tickers = add_fugle_contract_months(tickers, stock_futures)
    quotes = pd.DataFrame(
        [
            {
                "date": "2026-06-15",
                "symbol": "CDFF6",
                "total": {"tradeVolume": 3546},
                "closePrice": 2370,
            },
            {
                "date": "2026-06-15",
                "symbol": "CDFG6",
                "total": {"tradeVolume": 9999},
                "closePrice": 2385,
            },
        ]
    )
    near_quotes = quotes[quotes["symbol"].isin(near_tickers["symbol"])]

    realtime_history = build_fugle_quote_volume_history(near_quotes, stock_futures)
    latest = build_stock_futures_latest_quotes(realtime_history, stock_futures, near_quotes)
    records = watchlist_to_records(latest)

    assert records[0]["contract_date"] == "CDFF6"
    assert records[0]["volume"] == 3546.0
    assert records[0]["close"] == 2370.0


def test_futures_strategy_pools_use_product_daily_k_and_contract_type():
    dates = pd.date_range("2026-05-18", periods=21, freq="B")
    products = [
        ("CDF", "2330", "台積電", 2000, "regular", "大型", 150, 2500),
        ("QFF", "2330", "台積電", 100, "small", "小型", 1000, 3000),
        ("PUF", "2454", "聯發科", 2000, "regular", "大型", 300, 100),
    ]
    futures_rows = []
    stock_futures_rows = []
    for futures_id, stock_id, stock_name, size, contract_type, label, close_base, volume in products:
        stock_futures_rows.append(
            {
                "stock_id": stock_id,
                "stock_name": stock_name,
                "futures_id": futures_id[:-1],
                "finmind_futures_id": futures_id,
                "fugle_product_id": futures_id,
                "contract_size": size,
                "contract_type": contract_type,
                "contract_type_label": label,
            }
        )
        for index, trading_day in enumerate(dates):
            close = close_base + index
            futures_rows.append(
                {
                    "date": trading_day.strftime("%Y-%m-%d"),
                    "futures_id": futures_id,
                    "contract_date": "202606",
                    "open": close - 1,
                    "max": close * 1.04,
                    "min": close * 0.96,
                    "close": close,
                    "spread": 1,
                    "spread_per": 0.5,
                    "volume": volume,
                    "open_interest": 1000,
                    "trading_session": "position",
                }
            )

    history = build_futures_product_history(pd.DataFrame(futures_rows), pd.DataFrame(stock_futures_rows))
    high_price_pool = build_futures_strategy_pool(
        history,
        product_kind="small",
        criteria=StockPoolCriteria(volume_days=5, volume_top_n=2, atr_days=20, min_price=500, max_price=5000, min_atr_percent=3),
    )
    active_pool = build_futures_strategy_pool(
        history,
        product_kind="regular",
        criteria=StockPoolCriteria(volume_days=5, volume_top_n=2, atr_days=20, min_price=0, max_price=200, min_atr_percent=3),
    )

    assert list(high_price_pool["finmind_futures_id"]) == ["QFF"]
    assert list(active_pool["finmind_futures_id"]) == ["CDF"]
    assert high_price_pool.loc[0, "contract_type_label"] == "小型"
    assert active_pool.loc[0, "contract_type_label"] == "大型"
    assert high_price_pool.loc[0, "spread"] == 1
    assert active_pool.loc[0, "spread"] == 1
    assert high_price_pool.loc[0, "spread_per"] == 0.5
    assert active_pool.loc[0, "spread_per"] == 0.5
    assert high_price_pool.loc[0, "volume_top_days"] == 5
    assert active_pool.loc[0, "volume_top_days"] == 5


def test_futures_strategy_pools_rank_stock_level_combined_volume_before_contract_type():
    dates = pd.date_range("2026-05-18", periods=21, freq="B")
    products = [
        ("CDF", "2330", "台積電", 2000, "regular", "大型", 150, 60),
        ("QFF", "2330", "台積電", 100, "small", "小型", 1000, 60),
        ("PUF", "2454", "聯發科", 2000, "regular", "大型", 150, 100),
    ]
    futures_rows = []
    stock_futures_rows = []
    for futures_id, stock_id, stock_name, size, contract_type, label, close_base, volume in products:
        stock_futures_rows.append(
            {
                "stock_id": stock_id,
                "stock_name": stock_name,
                "futures_id": futures_id[:-1],
                "finmind_futures_id": futures_id,
                "fugle_product_id": futures_id,
                "contract_size": size,
                "contract_type": contract_type,
                "contract_type_label": label,
            }
        )
        for index, trading_day in enumerate(dates):
            close = close_base + index
            futures_rows.append(
                {
                    "date": trading_day.strftime("%Y-%m-%d"),
                    "futures_id": futures_id,
                    "contract_date": "202606",
                    "open": close - 1,
                    "max": close * 1.04,
                    "min": close * 0.96,
                    "close": close,
                    "spread": 1,
                    "spread_per": 0.5,
                    "volume": volume,
                    "open_interest": 1000,
                    "trading_session": "position",
                }
            )

    history = build_futures_product_history(pd.DataFrame(futures_rows), pd.DataFrame(stock_futures_rows))
    high_price_pool = build_futures_strategy_pool(
        history,
        product_kind="small",
        criteria=StockPoolCriteria(volume_days=5, volume_top_n=1, atr_days=20, min_price=500, max_price=5000, min_atr_percent=3),
    )
    active_pool = build_futures_strategy_pool(
        history,
        product_kind="regular",
        criteria=StockPoolCriteria(volume_days=5, volume_top_n=1, atr_days=20, min_price=0, max_price=200, min_atr_percent=3),
    )

    assert list(high_price_pool["finmind_futures_id"]) == ["QFF"]
    assert list(active_pool["finmind_futures_id"]) == ["CDF"]
    assert high_price_pool.loc[0, "avg_volume_5d"] == 120
    assert active_pool.loc[0, "avg_volume_5d"] == 120


def test_futures_strategy_pool_requires_top_50_volume_for_all_five_days():
    dates = pd.date_range("2026-05-18", periods=21, freq="B")
    bad_volume_date = dates[-3]
    rows = []

    def add_product_row(trading_day, futures_id, stock_id, contract_type, close, volume):
        rows.append(
            {
                "date": trading_day,
                "stock_id": stock_id,
                "stock_name": stock_id,
                "futures_id": futures_id[:-1],
                "finmind_futures_id": futures_id,
                "contract_type": contract_type,
                "contract_type_label": "small" if contract_type == "small" else "regular",
                "contract_date": "202606",
                "close": close,
                "max": close * 1.05,
                "min": close * 0.95,
                "Trading_Volume": volume,
                "spread_per": 1.0,
            }
        )

    for trading_day in dates:
        add_product_row(trading_day, "A00F", "1000", "small", 1000, 10000)
        b_volume = 1 if trading_day == bad_volume_date else 9000
        add_product_row(trading_day, "B00F", "1001", "small", 1000, b_volume)
        for index in range(50):
            add_product_row(trading_day, f"R{index:02d}F", f"20{index:02d}", "regular", 100, 8000)

    pool = build_futures_strategy_pool(
        pd.DataFrame(rows),
        product_kind="small",
        criteria=StockPoolCriteria(volume_days=5, volume_top_n=50, atr_days=20, min_price=500, max_price=5000, min_atr_percent=2),
    )

    assert list(pool["finmind_futures_id"]) == ["A00F"]
    assert pool.loc[0, "volume_top_days"] == 5
    assert pool.loc[0, "worst_volume_rank_5d"] == 1
    assert pool.loc[0, "volume_rank_5d"] == [1.0, 1.0, 1.0, 1.0, 1.0]


def test_candidate_stock_ids_from_futures_volume_prefilters_price_fetch():
    history = pd.DataFrame(
        [
            {"date": "2026-06-11", "stock_id": "2330", "Trading_Volume": 60},
            {"date": "2026-06-11", "stock_id": "2330", "Trading_Volume": 50},
            {"date": "2026-06-11", "stock_id": "2454", "Trading_Volume": 90},
            {"date": "2026-06-12", "stock_id": "2330", "Trading_Volume": 55},
            {"date": "2026-06-12", "stock_id": "2330", "Trading_Volume": 55},
            {"date": "2026-06-12", "stock_id": "2454", "Trading_Volume": 100},
        ]
    )

    stock_ids = candidate_stock_ids_from_futures_volume(
        history,
        as_of_date="2026-06-12",
        criteria=type("Criteria", (), {"volume_days": 2, "volume_top_n": 1})(),
    )

    assert stock_ids == ["2330"]


def test_new_entry_pool_finds_products_entering_top_50():
    rows = []
    for index in range(1, 61):
        futures_id = f"T{index:02d}F"
        rows.append(
            {
                "date": "2026-06-12",
                "stock_id": f"{index:04d}",
                "stock_name": f"股票{index:02d}",
                "futures_id": futures_id[:-1],
                "finmind_futures_id": futures_id,
                "contract_type": "regular",
                "contract_type_label": "大型",
                "contract_date": "202606",
                "close": 100 + index,
                "Trading_Volume": 1000 - index,
            }
        )
    for index in range(1, 61):
        futures_id = f"T{index:02d}F"
        volume = 2000 if index == 60 else 1000 - index
        rows.append(
            {
                "date": "2026-06-15",
                "stock_id": f"{index:04d}",
                "stock_name": f"股票{index:02d}",
                "futures_id": futures_id[:-1],
                "finmind_futures_id": futures_id,
                "contract_type": "regular",
                "contract_type_label": "大型",
                "contract_date": "202606",
                "close": 100 + index,
                "Trading_Volume": volume,
            }
        )

    entries = build_new_entry_pool(
        pd.DataFrame(rows),
        StockPoolCriteria(volume_days=5, volume_top_n=50),
    )
    records = new_entry_to_records(entries)

    assert records[0]["finmind_futures_id"] == "T60F"
    assert records[0]["current_rank"] == 1.0
    assert records[0]["previous_rank"] == 60.0
    assert records[0]["current_volume"] == 2000.0


def test_render_dashboard_html_contains_daily_pool_table():
    snapshot = DashboardSnapshot(
        generated_at="2026-06-15 12:00:00",
        as_of_date="2026-06-12",
        row_count=1,
        active_row_count=1,
        new_entry_count=1,
        watchlist_count=1,
        volume_window="2026-06-08~2026-06-12",
        atr_window="2026-05-16~2026-06-12",
        criteria={
            "volume_days": 5,
            "volume_top_n": 50,
            "atr_days": 20,
            "min_price": 500.0,
            "max_price": 5000.0,
            "min_atr_percent": 3.0,
        },
        rows=[
            {
                "date": "2026-06-12",
                "stock_id": "2330",
                "stock_name": "台積電",
                "futures_id": "QF",
                "finmind_futures_id": "QFF",
                "contract_type": "small",
                "contract_type_label": "小型",
                "contract_date": "202606",
                "close": 1005.12,
                "spread": 12.5,
                "spread_per": 1.25,
                "atr_20": 36.79,
                "atr_20_percent": 3.66,
                "avg_volume_5d": 1234567.0,
                "best_volume_rank_5d": 2.0,
                "worst_volume_rank_5d": 19.0,
                "volume_rank_5d": [19.0, 12.0, 8.0, 5.0, 2.0],
                "volume_top_days": 5.0,
                "volume_window": "2026-06-08~2026-06-12",
                "atr_window": "2026-05-16~2026-06-12",
            }
        ],
        new_entry_rows=[
            {
                "date": "2026-06-12",
                "previous_date": "2026-06-11",
                "stock_id": "8299",
                "stock_name": "群聯",
                "futures_id": "QN",
                "finmind_futures_id": "QNF",
                "contract_type": "small",
                "contract_type_label": "小型",
                "contract_date": "202606",
                "close": 2315.0,
                "current_volume": 12000.0,
                "previous_volume": 300.0,
                "current_rank": 8.0,
                "previous_rank": 55.0,
            }
        ],
        active_rows=[
            {
                "date": "2026-06-12",
                "stock_id": "2303",
                "stock_name": "聯電",
                "futures_id": "CC",
                "finmind_futures_id": "CCF",
                "contract_type": "regular",
                "contract_type_label": "大型",
                "contract_date": "202606",
                "close": 55.2,
                "spread": -1.4,
                "spread_per": -2.5,
                "atr_20": 2.1,
                "atr_20_percent": 3.8,
                "avg_volume_5d": 3210.0,
                "best_volume_rank_5d": 5.0,
                "worst_volume_rank_5d": 30.0,
                "volume_rank_5d": [30.0, 22.0, 18.0, 9.0, 5.0],
                "volume_top_days": 5.0,
                "volume_window": "2026-06-08~2026-06-12",
                "atr_window": "2026-05-16~2026-06-12",
            }
        ],
        watchlist_rows=[
            {
                "date": "2026-06-12",
                "stock_id": "2330",
                "stock_name": "台積電",
                "futures_id": "QF",
                "finmind_futures_id": "QFF",
                "contract_type": "small",
                "contract_type_label": "小型",
                "contract_date": "202606",
                "open": 1000.0,
                "high": 1020.0,
                "low": 998.0,
                "close": 1015.12,
                "volume": 1234567.0,
                "open_interest": 5432.0,
                "spread": 15.0,
                "spread_per": 1.5,
                "trading_session": "position",
                "source": "TaiwanFuturesDaily",
                "has_latest_trade": True,
            }
        ],
        source={"price_rows": 1200, "contract_rows": 300},
    )

    html = render_dashboard_html(snapshot)

    assert "每日股期股池" in html
    assert "盤後更新時間 13:30 13:45 14:00" in html
    assert "排名軌跡" in html
    assert "強弱勢排序" in html
    assert "股票期貨漲跌分流成交量" not in html
    assert "強勢股" in html
    assert "弱勢股" in html
    assert "butterfly-up-wing" in html
    assert "butterfly-down-wing" in html
    assert "intraday-mover-grid" in html
    assert "intraday-cumulative" not in html
    assert "台積電" in html
    assert "2330 / QFF" in html
    assert "2303 / CCF" in html
    assert "近 5 日所有股票期貨口數 Top 50" in html
    assert "精選股池" in html
    assert "小型股期股池" in html
    assert "大型股期股池" in html
    assert "今日速覽" in html
    assert "排序 : 成交口數Top50" in html
    assert 'id="today-overview-chart"' in html
    assert "成交口數Top50股票期貨今日速覽" in html
    assert "overview-x-label" in html
    assert "--overview-bg: #ffffff" in html
    assert "--overview-bg: #171d22" in html
    assert "background: var(--overview-bg)" in html
    assert 'class="overview-bg"' in html
    assert 'fill="#172337"' not in html
    assert "overview-wick" in html
    assert "--scrollbar-thumb" in html
    assert "--scrollbar-thumb-hover" in html
    assert "--scroll-fade-start" in html
    assert "::-webkit-scrollbar-thumb:hover" in html
    assert "scrollbar-width: thin" in html
    assert 'class="scroll-frame overview-frame"' in html
    assert html.count('class="scroll-frame"') >= 4
    assert ".scroll-frame::before" in html
    assert ".scroll-frame::after" in html
    assert "新進榜" in html
    assert "8299 / QNF" in html
    assert "活潑股股期股池" not in html
    assert "高價股股期股池" not in html
    assert "股票期貨 Watchlist" in html
    assert 'aria-label="股票期貨 Watchlist 分類"' in html
    assert 'data-watchlist-tab="all"' in html
    assert 'data-watchlist-tab="regular"' in html
    assert 'data-watchlist-tab="small"' in html
    assert 'id="watchlist-panel"' in html
    assert "全部股票期貨產品，即時報價一律取近月契約" in html
    assert "股期檔數" in html
    assert "漲跌幅%" in html
    assert 'title="依每日股期成交口數排序，紅色為前 10、黃色為前 25"' in html
    assert 'aria-label="近 5 日排名說明"' in html
    assert 'aria-label="盤中變化說明"' in html
    assert 'class="rank-pill hot">2</span>' in html
    assert '<span class="rank-summary"' not in html
    assert 'class="rank-delta is-empty">－</span>' in html
    assert 'class="rank-status">等待</span>' in html
    assert "padding-top: 9.6px" in html
    assert "padding-bottom: 9.6px" in html
    assert '<td class="number positive">1.25%</td>' in html
    assert '<td class="number negative">-2.50%</td>' in html
    assert '<td class="number positive">1.50%</td>' in html
    assert "資料列數" not in html
    assert "FinMind" not in html
    assert "Fugle 檢查中" not in html
    assert "session-status" in html
    assert 'aria-label="非交易時段"' in html
    assert 'class="connection-status is-offline"' in html
    assert "未平倉" not in html
    assert "來源" not in html
    assert "成交口數" in html
    assert "即時排序檢查中" in html
    assert "theme-toggle" in html
    assert "☾" in html
    assert "atr-filter" in html
    assert "as-of-filter" in html
    assert 'name="as_of"' in html
    assert "ATR門檻" in html
    assert '<option value="2">2%</option>' in html
    assert '<option value="3" selected>3%</option>' in html
    assert "<th>類型</th>" in html
    assert "小型" in html
    assert "期貨代碼" not in html
    assert 'class="pool-tab is-active"' in html
    assert 'data-pool-tab="small"' in html
    assert 'data-pool-tab="large"' in html
    assert 'data-pool-tab="new"' in html
    assert 'id="pool-panel-large"' in html
    assert 'id="pool-panel-new"' in html
    assert "<th>收盤價</th>\n      <th>漲跌幅%</th>\n      <th>ATR20%</th>" in html
    assert "<th>開盤累積" in html
    assert "aria-label=\"狀態說明\"" in html
    assert "依開盤第一個截點到最新截點的累積排名變化分類" in html
    assert "<th>股票</th>\n      <th>漲跌</th>\n      <th>漲跌%</th>\n      <th>成交口數</th>" in html
    assert "<th>收盤</th>\n      <th>類型</th>\n      <th>合約月份</th>" in html
    assert "<th>合約月份</th>\n      <th>日期</th>" in html
    assert '<td class="number positive">12.50</td>' not in html
    assert '<td class="number negative">-1.40</td>' not in html
    assert "歷史主力月份" not in html
    assert "QF / QFF" not in html
    assert "atr-meter" not in html
    assert "class=\"bar\"" not in html
    assert "口數區間" not in html
    assert "ATR 區間" not in html


def test_dashboard_shell_contains_realtime_ranking_script():
    html = render_dashboard_shell()

    assert "REALTIME_REFRESH_MS = 30000" in html
    assert "CLOSING_REFRESH_MINUTES" in html
    assert "isTradingSession" in html
    assert "shouldRunClosingRefresh" in html
    assert "completedClosingRefreshes" in html
    assert "buildPoolUrl" in html
    assert "renderTodayOverviewChart" in html
    assert "renderTodayOverviewChart(payload.watchlist_rows || [])" in html
    assert "renderButterflyChart" in html
    assert "renderButterflyChart(payload.watchlist_rows || [])" in html
    assert "--bg: #ffffff;" in html
    assert 'applyTheme("light")' in html
    assert "scrollbar-color: var(--scrollbar-thumb) var(--panel)" in html
    assert ".butterfly-wing::-webkit-scrollbar-thumb" in html
    assert "const cx = clamp(scale.x(row.volumeRate)" in html
    assert "const cy = clamp(scale.y(row.spread_per)" in html
    assert "量能偏離 %" in html
    assert "價格基準" in html
    assert "initWatchlistTabs" in html
    assert "switchWatchlistTab" in html
    assert "filterWatchlistRows" in html
    assert "watchlistMatchesTab" in html
    assert "data-watchlist-tab" in html
    assert 'regular: "只顯示大型股票期貨標的"' in html
    assert 'small: "只顯示小型股票期貨標的"' in html
    assert 'data-butterfly-limit="top50"' in html
    assert 'butterflyFilterState = { industry: "all", limit: "all", minVolume: 0 }' in html
    assert 'source.slice(0, INTRADAY_TOP_N)' in html
    assert "BUTTERFLY_SIDE_LIMIT" not in html
    assert "butterflyWingRows" in html
    assert "強勢股" in html
    assert "弱勢股" in html
    assert "新進 TopN" in html
    assert "08:45起累積變化" in html
    assert "新進 Top N" not in html
    assert 'const color = row.close_pct >= 0 ? "#ff3438" : "#00c92f";' in html
    assert "backgroundRefresh" in html
    assert "if (backgroundRefresh) loadPool();" in html
    assert 'params.set("min_atr_percent", currentAtrPercent())' in html
    assert "let asOfPinned" in html
    assert "if (!asOfPinned) return taipeiDateString();" in html
    assert "return !asOfPinned || currentAsOfDate() === taipeiDateString();" in html
    assert 'if (!asOfPinned) {' in html
    assert 'params.delete("as_of");' in html
    assert 'if (asOfPinned) params.set("as_of", currentAsOfDate());' in html
    assert "syncAsOfParam(params, currentAsOfDate())" in html
    assert "syncAsOfParam(syncedParams, effectiveAsOf)" in html
    assert 'params.delete("refresh")' in html
    assert "initAsOfFilter" in html
    assert "historical_mode" in html
    assert "即時排序中" in html
    assert "updateSessionStatus" in html
    assert "setSessionStatus" in html
    assert "交易時段" in html
    assert "非交易時段" in html
    assert "非交易時段 · 共用快照" in html
    assert "@media (max-width: 1024px)" in html
    assert "@media (max-width: 760px)" in html
    assert "@media (max-width: 520px)" in html
    assert "☼" in html
    assert "stock-futures-theme" in html
    assert "盤後更新時間 13:30 13:45 14:00" in html
    assert 'title="依每日股期成交口數排序，紅色為前 10、黃色為前 25"' in html
    assert "renderRankStrip" in html
    assert "renderIntradayChange" in html
    assert "tableSignatures" in html
    assert "row-updated" in html
    assert "scrollLeft" in html
    assert "overflow-anchor: none" in html
    assert "captureViewportScroll" in html
    assert "restoreViewportScroll(viewportScroll)" in html
    assert "tbody.innerHTML = bodyHtml" in html
    assert "renderErrorTables" in html
    assert 'hasRenderedSnapshot ? "更新中" : "資料載入中"' in html
    assert 'if (nextUrl === `${window.location.pathname}${window.location.search}`) return;' in html
    assert "refresh-button" not in html
    assert 'params.set("refresh", "1")' not in html
    assert "loadPool(true)" not in html
    assert "live || closingRefresh" in html
    assert "手動更新" not in html
    assert "renderPoolTable([])" not in html
    assert "renderActivePoolTable([])" not in html
    assert "renderNewEntryTable([])" not in html
    assert "renderWatchlistTable([])" not in html


def test_today_overview_rows_uses_watchlist_volume_top_50():
    rows = []
    for index in range(55):
        rows.append(
            {
                "stock_id": str(2000 + index),
                "stock_name": f"測試{index}",
                "futures_id": "QF",
                "finmind_futures_id": f"Q{index:02d}F",
                "open": 100.0,
                "high": 112.0,
                "low": 98.0,
                "close": 101.0,
                "spread": 1.0,
                "spread_per": 1.0,
                "volume": float(index),
            }
        )

    overview_rows = _today_overview_rows(rows)

    assert len(overview_rows) == 50
    assert overview_rows[0]["stock_name"] == "測試54"
    assert overview_rows[-1]["stock_name"] == "測試5"
    assert overview_rows[0]["open_pct"] == 0.0
    assert overview_rows[0]["close_pct"] == 1.0


def test_today_overview_chart_uses_taiwan_red_up_green_down_colors():
    html = _render_today_overview_chart(
        [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "futures_id": "CD",
                "finmind_futures_id": "CDF",
                "open": 110.0,
                "high": 112.0,
                "low": 99.0,
                "close": 105.0,
                "spread": 5.0,
                "spread_per": 5.0,
                "volume": 1000.0,
            },
            {
                "stock_id": "2303",
                "stock_name": "聯電",
                "futures_id": "CC",
                "finmind_futures_id": "CCF",
                "open": 90.0,
                "high": 101.0,
                "low": 89.0,
                "close": 97.0,
                "spread": -3.0,
                "spread_per": -3.0,
                "volume": 900.0,
            },
        ]
    )

    assert 'fill="#ff3438"' in html
    assert 'fill="#00c92f"' in html
    assert ">上漲</span>" in html
    assert ">下跌</span>" in html
