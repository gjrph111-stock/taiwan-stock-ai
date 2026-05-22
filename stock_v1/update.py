from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from time import sleep

from . import db
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
    default_start = today - timedelta(days=years * 365)
    total_rows = 0
    official_rows = 0
    failures = []
    wanted_codes = {stock["code"] for stock in stocks}

    try:
        official = [row for row in fetch_official_close_prices(today) if row["stock_code"] in wanted_codes]
        official_rows = db.upsert_prices(conn, official)
        if official_rows:
            print(f"[official] {today.isoformat()} +{official_rows}")
    except Exception as exc:
        failures.append({"code": "OFFICIAL", "name": "TWSE/TPEX close", "error": str(exc)})
        print(f"[official] {today.isoformat()} failed: {exc}")

    for idx, stock in enumerate(stocks, start=1):
        latest = db.latest_price_date(conn, stock["code"])
        start = default_start
        if latest:
            start = datetime.fromisoformat(latest).date() + timedelta(days=1)
        if start > today:
            continue

        try:
            rows = fetch_daily_prices(stock["code"], stock["yahoo_symbol"], start, today + timedelta(days=1))
            total_rows += db.upsert_prices(conn, rows)
            print(f"[{idx}/{len(stocks)}] {stock['code']} {stock['name']} +{len(rows)}")
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
