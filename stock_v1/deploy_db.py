import argparse
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import db
from .config import DEFAULT_DB_PATH


PUBLIC_TABLES = [
    "watchlist",
    "financial_kpis",
    "fund_holdings",
]


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
        public_counts = _copy_public_tables(src, dst)
        dst.commit()
        dst.execute("VACUUM")
        return {
            "target": str(target),
            "stocks": len(stocks),
            "prices": len(price_rows),
            "public_tables": public_counts,
            "latest": latest,
            "cutoff": cutoff,
            "size_mb": round(target.stat().st_size / 1024 / 1024, 1),
        }


def _copy_public_tables(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict:
    counts = {}
    for table in PUBLIC_TABLES:
        if not _table_exists(src, table):
            continue
        _ensure_table_schema(src, dst, table)
        counts[table] = _copy_table_rows(src, dst, table)
    return counts


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return bool(row)


def _table_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"PRAGMA table_info({table})").fetchall()


def _ensure_table_schema(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> None:
    if not _table_exists(dst, table):
        row = src.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row and row["sql"]:
            dst.execute(row["sql"])
    source_columns = _table_columns(src, table)
    target_columns = {row["name"] for row in _table_columns(dst, table)}
    for column in source_columns:
        name = column["name"]
        if name in target_columns:
            continue
        col_type = column["type"] or "TEXT"
        default = f" DEFAULT {column['dflt_value']}" if column["dflt_value"] is not None else ""
        dst.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}{default}")


def _copy_table_rows(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> int:
    source_columns = [row["name"] for row in _table_columns(src, table)]
    target_columns = {row["name"] for row in _table_columns(dst, table)}
    columns = [column for column in source_columns if column in target_columns]
    if not columns:
        return 0
    quoted = ", ".join(columns)
    placeholders = ", ".join(f":{column}" for column in columns)
    rows = src.execute(f"SELECT {quoted} FROM {table}").fetchall()
    dst.execute(f"DELETE FROM {table}")
    if rows:
        dst.executemany(
            f"INSERT INTO {table} ({quoted}) VALUES ({placeholders})",
            [{column: row[column] for column in columns} for row in rows],
        )
    return len(rows)


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
