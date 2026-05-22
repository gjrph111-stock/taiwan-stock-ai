from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from time import sleep

from . import db
from .finmind import fetch_finmind_stock_price
from .official_close import fetch_official_close_prices
from .universe import sync_universe
from .yahoo import fetch_daily_prices


def update_universe(conn) -> int:
    stocks = sync_universe()
    return db.upsert_stocks(conn, stocks)


def update_prices(conn, years: int, codes: list[str] | None, pause: float) -> dict:
    if not db.list_stocks(conn):
        update_universe(conn)

    stocks = db.list_stocks(conn, codes)
    if codes and len(stocks) != len(set(codes)):
        found = {row["code"] for row in stocks}
        missing = sorted(set(codes) - found)
        raise ValueError(f"Stocks not found in universe: {', '.join(missing)}")

    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    market_date = _latest_possible_market_date(today)
    default_start = market_date - timedelta(days=years * 365)
    total_rows = 0
    official_rows = 0
    failures = []
    wanted_codes = {stock["code"] for stock in stocks}

    try:
        official = [row for row in fetch_official_close_prices(market_date) if row["stock_code"] in wanted_codes]
        official_rows = db.upsert_prices(conn, official)
        if official_rows:
            print(f"[official] {market_date.isoformat()} +{official_rows}")
    except Exception as exc:
        failures.append({"code": "OFFICIAL", "name": "TWSE/TPEX close", "error": str(exc)})
        print(f"[official] {market_date.isoformat()} failed: {exc}")

    for idx, stock in enumerate(stocks, start=1):
        latest = db.latest_price_date(conn, stock["code"])
        start = default_start
        if latest:
            start = datetime.fromisoformat(latest).date() + timedelta(days=1)
        if start > market_date:
            continue

        try:
            rows, source = _fetch_price_rows(stock, start, market_date + timedelta(days=1))
            total_rows += db.upsert_prices(conn, rows)
            print(f"[{idx}/{len(stocks)}] {stock['code']} {stock['name']} +{len(rows)} ({source})")
        except Exception as exc:
            failures.append({"code": stock["code"], "name": stock["name"], "error": str(exc)})
            print(f"[{idx}/{len(stocks)}] {stock['code']} {stock['name']} failed: {exc}")

        if pause > 0:
            sleep(pause)

    return {
        "stocks": len(stocks),
        "rows": total_rows,
        "official_rows": official_rows,
        "failures": failures,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _fetch_price_rows(stock, start, end) -> tuple[list[dict], str]:
    yahoo_error = None
    try:
        rows = fetch_daily_prices(stock["code"], stock["yahoo_symbol"], start, end)
        if rows:
            return rows, "yahoo"
        yahoo_error = "Yahoo returned 0 rows"
    except Exception as exc:
        yahoo_error = str(exc)

    try:
        rows = fetch_finmind_stock_price(stock["code"], start, end)
        if rows:
            return rows, "finmind"
        raise RuntimeError("FinMind returned 0 rows")
    except Exception as exc:
        raise RuntimeError(f"Yahoo failed: {yahoo_error}; FinMind failed: {exc}") from exc


def _latest_possible_market_date(today):
    market_date = today
    while market_date.weekday() >= 5:
        market_date -= timedelta(days=1)
    return market_date
