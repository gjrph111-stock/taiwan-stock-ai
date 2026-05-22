import argparse
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import db
from .config import DEFAULT_DB_PATH


def build_deploy_db(source: Path = DEFAULT_DB_PATH, target: Path | None = None, days: int = 365) -> dict:
    target = target or source.with_name("tw_stocks_deploy.sqlite")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    with sqlite3.connect(source) as src, db.connect(target) as dst:
        src.row_factory = sqlite3.Row
        latest = src.execute("SELECT MAX(date) AS d FROM prices").fetchone()["d"]
        if not latest:
            raise RuntimeError("Source database has no price data.")
        cutoff = (date.fromisoformat(latest) - timedelta(days=days)).isoformat()

        stocks = src.execute("SELECT * FROM stocks ORDER BY code").fetchall()
        dst.executemany(
            """
            INSERT INTO stocks (code, name, market, yahoo_symbol, industry, updated_at)
            VALUES (:code, :name, :market, :yahoo_symbol, :industry, :updated_at)
            """,
            [dict(row) for row in stocks],
        )
        price_rows = src.execute(
            """
            SELECT stock_code, date, open, high, low, close, adj_close, volume, source, updated_at
            FROM prices
            WHERE date >= ?
            ORDER BY stock_code, date
            """,
            (cutoff,),
        ).fetchall()
        dst.executemany(
            """
            INSERT INTO prices (stock_code, date, open, high, low, close, adj_close, volume, source, updated_at)
            VALUES (:stock_code, :date, :open, :high, :low, :close, :adj_close, :volume, :source, :updated_at)
            """,
            [dict(row) for row in price_rows],
        )
        dst.commit()
        dst.execute("VACUUM")
        return {
            "target": str(target),
            "stocks": len(stocks),
            "prices": len(price_rows),
            "latest": latest,
            "cutoff": cutoff,
            "size_mb": round(target.stat().st_size / 1024 / 1024, 1),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a smaller SQLite database for web deployment.")
    parser.add_argument("--source", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--target", default=str(DEFAULT_DB_PATH.with_name("tw_stocks_deploy.sqlite")))
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()
    result = build_deploy_db(Path(args.source), Path(args.target), args.days)
    print(result)


if __name__ == "__main__":
    main()
