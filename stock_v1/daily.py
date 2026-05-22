import sqlite3
from pathlib import Path

from . import db
from .config import DEFAULT_DB_PATH
from .notify import DEFAULT_CONFIG_PATH, build_daily_message, load_config, send_telegram
from .update import update_prices, update_universe


def run_daily(
    conn: sqlite3.Connection,
    db_path: Path = DEFAULT_DB_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    years: int = 3,
    pause: float = 0.2,
    limit: int = 10,
    send: bool = True,
    skip_universe_on_error: bool = True,
) -> dict:
    run_id = db.start_run(conn, "daily")
    failures = []
    try:
        print("Step 1/3: syncing stock universe...")
        try:
            stock_count = update_universe(conn)
            print(f"Synced {stock_count} stocks.")
        except Exception as exc:
            existing = len(db.list_stocks(conn))
            if not skip_universe_on_error or existing == 0:
                raise
            stock_count = existing
            print(f"Universe sync failed, using existing {existing} stocks: {exc}")

        print("Step 2/3: updating daily prices...")
        price_result = update_prices(conn, years=years, codes=None, pause=pause)
        failures = price_result["failures"]
        print(
            f"Updated {price_result['rows']} Yahoo price rows and "
            f"{price_result.get('official_rows', 0)} official close rows."
        )

        print("Step 3/3: building notification...")
        message = build_daily_message(db_path, limit)
        if send:
            send_telegram(message, load_config(config_path))
            print("Telegram report sent.")
        else:
            print(message)

        status_value = "success" if not failures else "partial_success"
        summary = (
            f"stocks={stock_count}, rows={price_result['rows']}, "
            f"official_rows={price_result.get('official_rows', 0)}, "
            f"failures={len(failures)}, telegram={'sent' if send else 'preview'}"
        )
        db.finish_run(conn, run_id, status_value, summary)
        return {
            "run_id": run_id,
            "status": status_value,
            "stocks": stock_count,
            "rows": price_result["rows"],
            "official_rows": price_result.get("official_rows", 0),
            "failures": failures,
        }
    except Exception as exc:
        db.finish_run(conn, run_id, "failed", str(exc))
        raise
