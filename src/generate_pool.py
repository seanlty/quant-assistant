"""CLI for generating the daily small stock futures pool."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

import pandas as pd

from .data_sources import fetch_taifex_stock_futures_contracts
from .stock_pool import StockPoolCriteria, build_stock_futures_pool


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the daily small stock futures pool.")
    parser.add_argument("--prices", required=True, help="CSV with FinMind TaiwanStockPrice rows.")
    parser.add_argument("--contracts", help="CSV with TAIFEX stock futures contracts. Fetches TAIFEX when omitted.")
    parser.add_argument("--as-of", help="Target date. Defaults to the latest date in --prices.")
    parser.add_argument("--output", help="Output CSV path.")
    parser.add_argument("--volume-days", type=int, default=5)
    parser.add_argument("--volume-top-n", type=int, default=50)
    parser.add_argument("--atr-days", type=int, default=20)
    parser.add_argument("--min-price", type=float, default=500.0)
    parser.add_argument("--max-price", type=float, default=5000.0)
    parser.add_argument("--min-atr-percent", type=float, default=3.0)
    args = parser.parse_args(argv)

    prices = pd.read_csv(args.prices)
    if args.contracts:
        contracts = pd.read_csv(args.contracts)
    else:
        contracts = fetch_taifex_stock_futures_contracts()

    criteria = StockPoolCriteria(
        volume_days=args.volume_days,
        volume_top_n=args.volume_top_n,
        atr_days=args.atr_days,
        min_price=args.min_price,
        max_price=args.max_price,
        min_atr_percent=args.min_atr_percent,
    )
    pool = build_stock_futures_pool(prices, contracts, as_of_date=args.as_of, criteria=criteria)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pool.to_csv(output_path, index=False, encoding="utf-8-sig")
    else:
        print(pool.to_csv(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
