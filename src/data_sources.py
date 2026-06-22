"""External data source clients for the stock futures dashboard."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Dict, List, Optional

import pandas as pd
import requests


FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_FUTURES_SNAPSHOT_URL = "https://api.finmindtrade.com/api/v4/taiwan_futures_snapshot"
FUGLE_FUTOPT_BASE_URL = "https://api.fugle.tw/marketdata/v1.0/futopt"
TAIFEX_STOCK_LISTS_URL = "https://www.taifex.com.tw/cht/2/stockLists"

TAIFEX_CONTRACT_COLUMNS = (
    "futures_id",
    "underlying_name",
    "stock_id",
    "stock_name",
    "contract_size",
    "market_type",
    "regular_session",
    "after_hours_session",
    "effective_date",
)


def fetch_finmind_stock_prices(
    start_date: str,
    end_date: Optional[str] = None,
    token: Optional[str] = None,
    data_id: Optional[str] = None,
    timeout: int = 30,
) -> pd.DataFrame:
    """Fetch FinMind TaiwanStockPrice rows."""
    params = {
        "dataset": "TaiwanStockPrice",
        "start_date": start_date,
    }
    if end_date:
        params["end_date"] = end_date
    if data_id:
        params["data_id"] = data_id

    headers = {"Authorization": "Bearer {}".format(token)} if token else {}
    response = requests.get(FINMIND_DATA_URL, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != 200:
        raise RuntimeError("FinMind error: {}".format(payload.get("msg", payload)))
    return pd.DataFrame(payload.get("data", []))


def fetch_finmind_futures_daily(
    start_date: str,
    end_date: Optional[str] = None,
    token: Optional[str] = None,
    data_id: Optional[str] = None,
    timeout: int = 30,
) -> pd.DataFrame:
    """Fetch FinMind TaiwanFuturesDaily rows."""
    params = {
        "dataset": "TaiwanFuturesDaily",
        "start_date": start_date,
    }
    if end_date:
        params["end_date"] = end_date
    if data_id:
        params["data_id"] = data_id

    headers = {"Authorization": "Bearer {}".format(token)} if token else {}
    response = requests.get(FINMIND_DATA_URL, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != 200:
        raise RuntimeError("FinMind futures daily error: {}".format(payload.get("msg", payload)))
    return pd.DataFrame(payload.get("data", []))


def fetch_finmind_futopt_daily_info(token: Optional[str] = None, timeout: int = 30) -> pd.DataFrame:
    """Fetch FinMind futures/options product metadata."""
    params = {"dataset": "TaiwanFutOptDailyInfo"}
    headers = {"Authorization": "Bearer {}".format(token)} if token else {}
    response = requests.get(FINMIND_DATA_URL, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != 200:
        raise RuntimeError("FinMind futopt info error: {}".format(payload.get("msg", payload)))
    return pd.DataFrame(payload.get("data", []))


def fetch_finmind_futures_snapshot(
    data_id: str,
    token: Optional[str] = None,
    timeout: int = 30,
) -> pd.DataFrame:
    """Fetch FinMind taiwan_futures_snapshot rows for a futures product."""
    headers = {"Authorization": "Bearer {}".format(token)} if token else {}
    response = requests.get(
        FINMIND_FUTURES_SNAPSHOT_URL,
        params={"data_id": data_id},
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != 200:
        raise RuntimeError("FinMind futures snapshot error: {}".format(payload.get("msg", payload)))
    return pd.DataFrame(payload.get("data", []))


def fetch_fugle_futopt_products(
    api_key: str,
    session: str = "REGULAR",
    timeout: int = 30,
) -> pd.DataFrame:
    """Fetch Fugle intraday stock-futures product metadata."""
    payload = _fetch_fugle_futopt_json(
        path="/intraday/products",
        api_key=api_key,
        params={
            "type": "FUTURE",
            "exchange": "TAIFEX",
            "session": session,
            "contractType": "S",
        },
        timeout=timeout,
    )
    return pd.DataFrame(payload.get("data", []))


def fetch_fugle_futopt_tickers(
    api_key: str,
    session: str = "REGULAR",
    product: Optional[str] = None,
    timeout: int = 30,
) -> pd.DataFrame:
    """Fetch Fugle intraday stock-futures ticker symbols."""
    params = {
        "type": "FUTURE",
        "exchange": "TAIFEX",
        "session": session,
        "contractType": "S",
    }
    if product:
        params["product"] = product
    payload = _fetch_fugle_futopt_json(
        path="/intraday/tickers",
        api_key=api_key,
        params=params,
        timeout=timeout,
    )
    return pd.DataFrame(payload.get("data", []))


def fetch_fugle_futopt_quote(
    symbol: str,
    api_key: str,
    session: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, object]:
    """Fetch a single Fugle intraday futures/options quote."""
    params = {"session": session} if session else None
    return _fetch_fugle_futopt_json(
        path="/intraday/quote/{}".format(symbol),
        api_key=api_key,
        params=params,
        timeout=timeout,
    )


def fetch_fugle_futopt_candles(
    symbol: str,
    api_key: str,
    timeframe: str = "5",
    session: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, object]:
    """Fetch Fugle intraday futures/options candles for one ticker symbol."""
    params: Dict[str, object] = {"timeframe": timeframe}
    if session:
        params["session"] = session
    return _fetch_fugle_futopt_json(
        path="/intraday/candles/{}".format(symbol),
        api_key=api_key,
        params=params,
        timeout=timeout,
    )


def _fetch_fugle_futopt_json(
    path: str,
    api_key: str,
    params: Optional[Dict[str, object]] = None,
    timeout: int = 30,
) -> Dict[str, object]:
    headers = {"X-API-KEY": api_key}
    response = requests.get(
        FUGLE_FUTOPT_BASE_URL + path,
        params=params,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Fugle returned an unexpected payload")
    return payload


def fetch_taifex_stock_futures_contracts(timeout: int = 30) -> pd.DataFrame:
    """Fetch stock futures contract metadata from TAIFEX."""
    response = requests.get(TAIFEX_STOCK_LISTS_URL, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    html = response.text
    return parse_taifex_stock_futures_contracts(html)


def parse_taifex_stock_futures_contracts(html: str) -> pd.DataFrame:
    """Parse TAIFEX stock futures contract rows into a normalized DataFrame."""
    rows = _extract_table_rows(html)
    effective_date = _parse_effective_date(html)
    records = []
    for row in rows:
        record = _parse_taifex_contract_row(row, effective_date)
        if record:
            records.append(record)

    if not records:
        records = _parse_contract_rows_from_text(html, effective_date)

    return pd.DataFrame(records, columns=list(TAIFEX_CONTRACT_COLUMNS))


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        HTMLParser.__init__(self)
        self.rows: List[List[str]] = []
        self._current_row: Optional[List[str]] = None
        self._current_cell: Optional[List[str]] = None

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        if tag == "tr":
            self._current_row = []
        elif tag in ("td", "th") and self._current_row is not None:
            self._current_cell = []

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._current_cell is not None and self._current_row is not None:
            cell = _normalize_space("".join(self._current_cell))
            self._current_row.append(cell)
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None:
            if any(cell for cell in self._current_row):
                self.rows.append(self._current_row)
            self._current_row = None


def _extract_table_rows(html: str) -> List[List[str]]:
    parser = _TableParser()
    parser.feed(html)
    return parser.rows


def _parse_taifex_contract_row(row: List[str], effective_date: str) -> Optional[Dict[str, object]]:
    if len(row) < 12:
        return None
    futures_id = row[0].strip()
    stock_id = row[2].strip()
    if not re.match(r"^[A-Z]{2}$", futures_id):
        return None
    if not re.match(r"^\d{4,6}[A-Z]?$", stock_id):
        return None

    contract_size = _parse_int(row[11])
    if contract_size is None:
        return None

    return {
        "futures_id": futures_id,
        "underlying_name": row[1].strip(),
        "stock_id": stock_id,
        "stock_name": row[3].strip(),
        "contract_size": contract_size,
        "market_type": _derive_market_type(row),
        "regular_session": row[12].strip() if len(row) > 12 else "",
        "after_hours_session": row[13].strip() if len(row) > 13 else "",
        "effective_date": effective_date,
    }


def _parse_contract_rows_from_text(html: str, effective_date: str) -> List[Dict[str, object]]:
    text = _normalize_space(unescape(re.sub(r"<[^>]+>", " ", html)))
    pattern = re.compile(
        r"\b(?P<futures_id>[A-Z]{2})\s+"
        r"(?P<underlying_name>.+?)\s+"
        r"(?P<stock_id>\d{4,6}[A-Z]?)\s+"
        r"(?P<stock_name>\S+)\s+"
        r".*?\s(?P<contract_size>100|1,000|2,000|10,000)\s+"
        r"(?P<regular_session>\d{1,2}:\d{2}~[^ ]+|-)",
    )
    records = []
    for match in pattern.finditer(text):
        records.append(
            {
                "futures_id": match.group("futures_id"),
                "underlying_name": match.group("underlying_name").strip(),
                "stock_id": match.group("stock_id"),
                "stock_name": match.group("stock_name"),
                "contract_size": _parse_int(match.group("contract_size")),
                "market_type": "",
                "regular_session": match.group("regular_session"),
                "after_hours_session": "",
                "effective_date": effective_date,
            }
        )
    return records


def _derive_market_type(row: List[str]) -> str:
    market_cells = [
        ("listed_stock", 7),
        ("otc_stock", 8),
        ("listed_etf", 9),
        ("otc_etf", 10),
    ]
    for market_type, index in market_cells:
        if index < len(row) and _is_yes(row[index]):
            return market_type
    return ""


def _is_yes(value: str) -> bool:
    return "●" in value or "◎" in value or "是" in value


def _parse_effective_date(html: str) -> str:
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    match = re.search(r"最新更新\(生效\)日期：\s*(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if not match:
        return ""
    year, month, day = match.groups()
    return "{}-{:02d}-{:02d}".format(int(year), int(month), int(day))


def _parse_int(value: object) -> Optional[int]:
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
