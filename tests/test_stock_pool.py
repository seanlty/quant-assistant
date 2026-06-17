import pandas as pd

from src.data_sources import parse_taifex_stock_futures_contracts
from src.stock_pool import StockPoolCriteria, build_stock_futures_pool, get_small_stock_futures_contracts


def make_price_data():
    rows = []
    dates = pd.date_range("2026-01-01", periods=21, freq="D")
    for idx, trade_date in enumerate(dates):
        for stock_id, base_close, volume in [
            ("2330", 1000, 10000),
            ("2454", 800, 9000),
            ("3008", 2500, 100),
        ]:
            close = base_close + idx
            if stock_id == "3008" and idx == len(dates) - 1:
                volume = 11000
            rows.append(
                {
                    "date": trade_date.strftime("%Y-%m-%d"),
                    "stock_id": stock_id,
                    "Trading_Volume": volume,
                    "max": close * 1.025,
                    "min": close * 0.975,
                    "close": close,
                }
            )
    return pd.DataFrame(rows)


def make_contracts():
    return pd.DataFrame(
        [
            {"futures_id": "QF", "stock_id": "2330", "stock_name": "台積電", "contract_size": 100},
            {"futures_id": "PU", "stock_id": "2454", "stock_name": "聯發科", "contract_size": 2000},
            {"futures_id": "OL", "stock_id": "3008", "stock_name": "大立光", "contract_size": 100},
        ]
    )


def make_futures_volume_data():
    rows = []
    dates = pd.date_range("2026-01-17", periods=5, freq="D")
    for trade_date in dates:
        for stock_id, volume in [
            ("2330", 5000),
            ("2454", 4000),
            ("3008", 100),
        ]:
            rows.append(
                {
                    "date": trade_date.strftime("%Y-%m-%d"),
                    "stock_id": stock_id,
                    "Trading_Volume": volume,
                }
            )
    return pd.DataFrame(rows)


def test_build_stock_futures_pool_filters_by_all_rules():
    criteria = StockPoolCriteria(volume_top_n=2)

    pool = build_stock_futures_pool(
        make_price_data(),
        make_contracts(),
        as_of_date="2026-01-21",
        criteria=criteria,
        futures_volume_data=make_futures_volume_data(),
    )

    assert list(pool["stock_id"]) == ["2330"]
    assert pool.loc[0, "stock_name"] == "台積電"
    assert pool.loc[0, "futures_id"] == "QF"
    assert pool.loc[0, "volume_top_days"] == 5
    assert pool.loc[0, "volume_rank_5d"] == [1.0, 1.0, 1.0, 1.0, 1.0]
    assert pool.loc[0, "atr_20_percent"] >= 3.0


def test_get_small_stock_futures_contracts_keeps_contract_size_100():
    contracts = get_small_stock_futures_contracts(make_contracts())

    assert set(contracts["stock_id"]) == {"2330", "3008"}


def test_parse_taifex_stock_futures_contracts():
    html = """
    <html><body>
      <table>
        <tr>
          <td>QF</td><td>台灣積體電路製造股份有限公司</td><td>2330</td><td>台積電</td>
          <td>●</td><td></td><td></td><td>◎</td><td></td><td></td><td></td>
          <td>100</td><td>8:45~13:45</td><td>17:25~次日05:00</td>
        </tr>
      </table>
      <p>註：最新更新(生效)日期：2026年5月4日。</p>
    </body></html>
    """

    contracts = parse_taifex_stock_futures_contracts(html)

    assert contracts.loc[0, "futures_id"] == "QF"
    assert contracts.loc[0, "stock_id"] == "2330"
    assert contracts.loc[0, "contract_size"] == 100
    assert contracts.loc[0, "market_type"] == "listed_stock"
    assert contracts.loc[0, "effective_date"] == "2026-05-04"
