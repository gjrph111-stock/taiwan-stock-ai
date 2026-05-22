import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('TWSE', 'TPEX')),
    yahoo_symbol TEXT NOT NULL,
    industry TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prices (
    stock_code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    adj_close REAL,
    volume INTEGER,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (stock_code, date),
    FOREIGN KEY (stock_code) REFERENCES stocks(code)
);

CREATE TABLE IF NOT EXISTS update_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS watchlist (
    code TEXT PRIMARY KEY,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (code) REFERENCES stocks(code)
);

CREATE TABLE IF NOT EXISTS app_users (
    user_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    telegram_chat_id TEXT,
    telegram_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_watchlist (
    user_key TEXT NOT NULL,
    code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_key, code),
    FOREIGN KEY (user_key) REFERENCES app_users(user_key),
    FOREIGN KEY (code) REFERENCES stocks(code)
);

CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);
CREATE INDEX IF NOT EXISTS idx_stocks_market ON stocks(market);
CREATE INDEX IF NOT EXISTS idx_watchlist_created_at ON watchlist(created_at);
CREATE INDEX IF NOT EXISTS idx_user_watchlist_user ON user_watchlist(user_key, created_at);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_stocks(conn: sqlite3.Connection, stocks: list[dict]) -> int:
    conn.executemany(
        """
        INSERT INTO stocks (code, name, market, yahoo_symbol, industry, updated_at)
        VALUES (:code, :name, :market, :yahoo_symbol, :industry, :updated_at)
        ON CONFLICT(code) DO UPDATE SET
            name = excluded.name,
            market = excluded.market,
            yahoo_symbol = excluded.yahoo_symbol,
            industry = excluded.industry,
            updated_at = excluded.updated_at
        """,
        stocks,
    )
    conn.commit()
    return len(stocks)


def upsert_prices(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO prices (
            stock_code, date, open, high, low, close, adj_close, volume, source, updated_at
        )
        VALUES (
            :stock_code, :date, :open, :high, :low, :close, :adj_close, :volume, :source, :updated_at
        )
        ON CONFLICT(stock_code, date) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            adj_close = excluded.adj_close,
            volume = excluded.volume,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def latest_price_date(conn: sqlite3.Connection, code: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) AS latest_date FROM prices WHERE stock_code = ?",
        (code,),
    ).fetchone()
    return row["latest_date"] if row and row["latest_date"] else None


def list_stocks(conn: sqlite3.Connection, codes: list[str] | None = None) -> list[sqlite3.Row]:
    if codes:
        placeholders = ",".join("?" for _ in codes)
        return conn.execute(
            f"SELECT * FROM stocks WHERE code IN ({placeholders}) ORDER BY code",
            codes,
        ).fetchall()
    return conn.execute("SELECT * FROM stocks ORDER BY code").fetchall()


def status(conn: sqlite3.Connection) -> dict:
    stocks = conn.execute("SELECT COUNT(*) AS n FROM stocks").fetchone()["n"]
    prices = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
    markets = conn.execute(
        "SELECT market, COUNT(*) AS n FROM stocks GROUP BY market ORDER BY market"
    ).fetchall()
    date_range = conn.execute(
        "SELECT MIN(date) AS first_date, MAX(date) AS last_date FROM prices"
    ).fetchone()
    return {
        "stocks": stocks,
        "prices": prices,
        "markets": {row["market"]: row["n"] for row in markets},
        "first_date": date_range["first_date"],
        "last_date": date_range["last_date"],
    }


def start_run(conn: sqlite3.Connection, task: str) -> int:
    row = conn.execute(
        """
        INSERT INTO update_runs (task, started_at, status)
        VALUES (?, datetime('now'), 'running')
        """,
        (task,),
    )
    conn.commit()
    return int(row.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status_value: str, message: str) -> None:
    conn.execute(
        """
        UPDATE update_runs
        SET finished_at = datetime('now'), status = ?, message = ?
        WHERE id = ?
        """,
        (status_value, message[:4000], run_id),
    )
    conn.commit()


def latest_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, task, started_at, finished_at, status, message
        FROM update_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
