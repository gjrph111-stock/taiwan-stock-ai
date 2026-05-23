import json
import os
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import REQUEST_HEADERS


BASE_URL = "https://api.finmindtrade.com/api/v4/data"


def fetch_finmind_stock_price(stock_code: str, start_date: date, end_date: date) -> list[dict]:
    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": stock_code,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    payload = _download_json(params)
    if payload.get("status") != 200:
        raise RuntimeError(f"FinMind {stock_code} failed: {payload.get('msg')}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for item in payload.get("data") or []:
        rows.append(
            {
                "stock_code": str(item["stock_id"]),
                "date": item["date"],
                "open": item.get("open"),
                "high": item.get("max"),
                "low": item.get("min"),
                "close": item.get("close"),
                "adj_close": item.get("close"),
                "volume": item.get("Trading_Volume"),
                "source": "finmind",
                "updated_at": now,
            }
        )
    return rows


def fetch_recent_finmind_prices(stock_code: str, days: int = 10) -> list[dict]:
    today = date.today()
    return fetch_finmind_stock_price(stock_code, today - timedelta(days=days), today)


def fetch_finmind_kbar(stock_code: str, target_date: date) -> list[dict]:
    params = {
        "dataset": "TaiwanStockKBar",
        "data_id": stock_code,
        "start_date": target_date.isoformat(),
    }
    payload = _download_json(params)
    if payload.get("status") != 200:
        raise RuntimeError(f"FinMind KBar {stock_code} failed: {payload.get('msg')}")
    rows = []
    for item in payload.get("data") or []:
        rows.append(
            {
                "date": item.get("date"),
                "time": item.get("minute"),
                "label": item.get("minute") or item.get("date"),
                "open": item.get("open"),
                "high": item.get("high"),
                "low": item.get("low"),
                "close": item.get("close"),
                "volume": item.get("volume"),
            }
        )
    return rows


def fetch_finmind_institutional(stock_code: str, start_date: date, end_date: date) -> list[dict]:
    params = {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
        "data_id": stock_code,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    payload = _download_json(params)
    if payload.get("status") != 200:
        raise RuntimeError(f"FinMind institutional {stock_code} failed: {payload.get('msg')}")
    rows = []
    for item in payload.get("data") or []:
        buy = _num(item.get("buy") or item.get("Buy") or item.get("buy_volume"))
        sell = _num(item.get("sell") or item.get("Sell") or item.get("sell_volume"))
        rows.append(
            {
                "date": item.get("date"),
                "name": item.get("name") or item.get("investor") or item.get("institutional_investors") or "法人",
                "buy": buy,
                "sell": sell,
                "net": buy - sell,
            }
        )
    return rows


def _num(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _download_json(params: dict) -> dict:
    headers = dict(REQUEST_HEADERS)
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{BASE_URL}?{urlencode(params)}", headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))
