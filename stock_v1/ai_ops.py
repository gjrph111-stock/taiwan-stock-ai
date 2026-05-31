import sqlite3

from .backtest import load_histories
from .names import short_name
from .signals import passes_risk_filter, risk_adjusted_score, score_stock


INITIAL_CAPITAL = 200000.0
MAX_POSITIONS = 5
HOLD_DAYS = 10
COST_RATE = 0.002


def run_daily_ai_ops(conn: sqlite3.Connection, initial_capital: float = INITIAL_CAPITAL) -> dict:
    _ensure_ai_ops_tables(conn)
    latest_date = _latest_date(conn)
    if not latest_date:
        return _empty_payload(initial_capital)
    _ensure_account(conn, initial_capital)
    existing = conn.execute("SELECT 1 FROM ai_ops_runs WHERE run_date = ?", (latest_date,)).fetchone()
    if not existing:
        _execute_ops_for_date(conn, latest_date)
        conn.execute(
            "INSERT INTO ai_ops_runs (run_date, created_at) VALUES (?, datetime('now'))",
            (latest_date,),
        )
        conn.commit()
    return build_ai_ops_payload(conn, initial_capital)


def build_ai_ops_payload(conn: sqlite3.Connection, initial_capital: float = INITIAL_CAPITAL) -> dict:
    _ensure_ai_ops_tables(conn)
    latest_date = _latest_date(conn)
    account = _ensure_account(conn, initial_capital)
    open_positions = _open_positions(conn)
    open_rows = [_position_payload(conn, row, latest_date) for row in open_positions]
    closed_rows = [_trade_payload(row) for row in conn.execute(
        """
        SELECT *
        FROM ai_ops_trades
        WHERE action = 'SELL'
        ORDER BY trade_date DESC, id DESC
        LIMIT 20
        """
    ).fetchall()]
    entry_rows = [_trade_payload(row) for row in conn.execute(
        """
        SELECT *
        FROM ai_ops_trades
        WHERE action = 'BUY'
        ORDER BY trade_date DESC, id DESC
        LIMIT 20
        """
    ).fetchall()]
    curve = [dict(row) for row in conn.execute(
        "SELECT date, total_value AS capital FROM ai_ops_equity ORDER BY date"
    ).fetchall()]
    total_value = curve[-1]["capital"] if curve else float(account["cash"])
    all_closed = conn.execute(
        "SELECT return_pct FROM ai_ops_trades WHERE action = 'SELL' AND return_pct IS NOT NULL"
    ).fetchall()
    closed_returns = [float(row["return_pct"]) for row in all_closed]
    win_rate = (sum(1 for value in closed_returns if value > 0) / len(closed_returns) * 100) if closed_returns else 0
    median_trade = _median(closed_returns) if closed_returns else 0
    max_drawdown = _max_drawdown([row["capital"] for row in curve])
    return {
        "summary": {
            "max_positions": MAX_POSITIONS,
            "horizon": HOLD_DAYS,
            "step": 1,
            "cost_bps": int(COST_RATE * 10000),
            "start_date": curve[0]["date"] if curve else latest_date,
            "trades": len(closed_returns),
            "open_positions": len(open_rows),
            "initial_capital": initial_capital,
            "final_capital": total_value,
            "total_return": (total_value / initial_capital - 1) * 100 if initial_capital else 0,
            "win_rate": win_rate,
            "median_trade_return": median_trade,
            "max_drawdown": max_drawdown,
            "cash": float(account["cash"]),
        },
        "curve": curve,
        "recent_entries": entry_rows,
        "closed_trades": closed_rows,
        "recent_trades": closed_rows,
        "open_positions": open_rows,
        "today_actions": [dict(row) for row in conn.execute(
            "SELECT * FROM ai_ops_trades WHERE trade_date = ? ORDER BY id",
            (latest_date,),
        ).fetchall()],
    }


def _execute_ops_for_date(conn: sqlite3.Connection, date_value: str) -> None:
    account = _ensure_account(conn, INITIAL_CAPITAL)
    for position in _open_positions(conn):
        decision = _exit_decision(conn, position, date_value)
        if decision:
            _sell_position(conn, position, date_value, decision)
    account = conn.execute("SELECT * FROM ai_ops_account WHERE id = 1").fetchone()
    open_codes = {row["code"] for row in _open_positions(conn)}
    slots = MAX_POSITIONS - len(open_codes)
    if slots > 0:
        candidates = _rank_candidates(conn, date_value, open_codes)
        for candidate in candidates[:slots]:
            account = conn.execute("SELECT * FROM ai_ops_account WHERE id = 1").fetchone()
            cash = float(account["cash"])
            if cash < 10000:
                break
            allocation = min(cash * 0.95, max(INITIAL_CAPITAL / MAX_POSITIONS, cash / max(1, slots)))
            _buy_candidate(conn, candidate, date_value, allocation)
            slots -= 1
            if slots <= 0:
                break
    _mark_equity(conn, date_value)


def _rank_candidates(conn: sqlite3.Connection, date_value: str, open_codes: set[str]) -> list[dict]:
    histories = load_histories(conn)
    rows = []
    for code, item in histories.items():
        if code in open_codes:
            continue
        idx = item["date_index"].get(date_value)
        if idx is None or idx < 79:
            continue
        history = item["rows"][: idx + 1]
        signal = score_stock(item["stock"], history)
        if not signal or not passes_risk_filter(signal):
            continue
        signal["risk_adjusted_score"] = risk_adjusted_score(signal)
        if signal["risk_adjusted_score"] < 62:
            continue
        rows.append({**signal, "price": float(history[-1]["close"])})
    rows.sort(key=lambda row: (row["risk_adjusted_score"], row.get("return_20d") or -999), reverse=True)
    return rows


def _buy_candidate(conn: sqlite3.Connection, candidate: dict, date_value: str, allocation: float) -> None:
    price = float(candidate["price"])
    shares = int((allocation * (1 - COST_RATE)) // price)
    if shares <= 0:
        return
    amount = shares * price
    stop = _parse_first_price(candidate.get("stop")) or price * 0.92
    target = _parse_last_price(candidate.get("exit_zone")) or price * 1.08
    conn.execute(
        """
        INSERT INTO ai_ops_positions
        (code, name, entry_date, entry_price, shares, cost, stop_price, target_price, score, signal, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', datetime('now'))
        """,
        (
            candidate["code"],
            candidate.get("short_name") or short_name(candidate.get("name", candidate["code"])),
            date_value,
            price,
            shares,
            amount,
            stop,
            target,
            candidate.get("risk_adjusted_score") or candidate.get("score"),
            candidate.get("signal"),
        ),
    )
    conn.execute("UPDATE ai_ops_account SET cash = cash - ?, updated_at = datetime('now') WHERE id = 1", (amount * (1 + COST_RATE),))
    conn.execute(
        """
        INSERT INTO ai_ops_trades
        (trade_date, action, code, name, price, shares, amount, score, reason, created_at)
        VALUES (?, 'BUY', ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (date_value, candidate["code"], candidate.get("short_name") or short_name(candidate.get("name", candidate["code"])), price, shares, amount, candidate.get("risk_adjusted_score"), "AI 經理人開倉"),
    )


def _sell_position(conn: sqlite3.Connection, position: sqlite3.Row, date_value: str, reason: str) -> None:
    price = _price_on(conn, position["code"], date_value)
    if price is None:
        return
    shares = int(position["shares"])
    amount = shares * price
    pnl_pct = (price / float(position["entry_price"]) - 1) * 100
    conn.execute(
        """
        UPDATE ai_ops_positions
        SET status='CLOSED', exit_date=?, exit_price=?, exit_reason=?, updated_at=datetime('now')
        WHERE id=?
        """,
        (date_value, price, reason, position["id"]),
    )
    conn.execute("UPDATE ai_ops_account SET cash = cash + ?, updated_at = datetime('now') WHERE id = 1", (amount * (1 - COST_RATE),))
    conn.execute(
        """
        INSERT INTO ai_ops_trades
        (trade_date, action, code, name, price, shares, amount, score, return_pct, reason, created_at)
        VALUES (?, 'SELL', ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (date_value, position["code"], position["name"], price, shares, amount, position["score"], pnl_pct, reason),
    )


def _exit_decision(conn: sqlite3.Connection, position: sqlite3.Row, date_value: str) -> str | None:
    price = _price_on(conn, position["code"], date_value)
    if price is None:
        return None
    if price <= float(position["stop_price"]):
        return "跌破停損"
    if price >= float(position["target_price"]):
        return "到達停利區"
    held_days = conn.execute(
        """
        SELECT COUNT(*) AS n FROM prices
        WHERE stock_code = ? AND date > ? AND date <= ?
        """,
        (position["code"], position["entry_date"], date_value),
    ).fetchone()["n"]
    if held_days >= HOLD_DAYS:
        return "持有期滿"
    histories = load_histories(conn)
    item = histories.get(position["code"])
    if item and date_value in item["date_index"]:
        idx = item["date_index"][date_value]
        signal = score_stock(item["stock"], item["rows"][: idx + 1])
        if signal:
            score = risk_adjusted_score(signal)
            if signal.get("signal") == "weak" or score < 48:
                return "訊號轉弱"
    return None


def _mark_equity(conn: sqlite3.Connection, date_value: str) -> None:
    account = conn.execute("SELECT * FROM ai_ops_account WHERE id = 1").fetchone()
    cash = float(account["cash"])
    position_value = 0.0
    for position in _open_positions(conn):
        price = _price_on(conn, position["code"], date_value) or float(position["entry_price"])
        position_value += int(position["shares"]) * price
    total = cash + position_value
    conn.execute(
        """
        INSERT INTO ai_ops_equity (date, cash, position_value, total_value, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(date) DO UPDATE SET cash=excluded.cash, position_value=excluded.position_value, total_value=excluded.total_value
        """,
        (date_value, cash, position_value, total),
    )


def _position_payload(conn: sqlite3.Connection, row: sqlite3.Row, date_value: str | None) -> dict:
    last = _price_on(conn, row["code"], date_value) if date_value else None
    last = last or float(row["entry_price"])
    return {
        "entry_date": row["entry_date"],
        "exit_date": row["exit_date"] or "持有中",
        "code": row["code"],
        "name": row["name"],
        "entry_price": row["entry_price"],
        "exit_price": last,
        "score": row["score"],
        "return": (last / float(row["entry_price"]) - 1) * 100,
        "capital": int(row["shares"]) * last,
        "shares": row["shares"],
        "stop": row["stop_price"],
        "target": row["target_price"],
    }


def _trade_payload(row: sqlite3.Row) -> dict:
    return {
        "entry_date": row["trade_date"] if row["action"] == "BUY" else "",
        "exit_date": row["trade_date"] if row["action"] == "SELL" else "",
        "date": row["trade_date"],
        "action": row["action"],
        "code": row["code"],
        "name": row["name"],
        "entry_price": row["price"],
        "exit_price": row["price"],
        "score": row["score"],
        "return": row["return_pct"],
        "capital": row["amount"],
        "entry_value": row["amount"],
        "reason": row["reason"],
    }


def _latest_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(date) AS latest FROM prices WHERE close IS NOT NULL").fetchone()
    return row["latest"] if row and row["latest"] else None


def _price_on(conn: sqlite3.Connection, code: str, date_value: str | None) -> float | None:
    if not date_value:
        return None
    row = conn.execute(
        "SELECT close FROM prices WHERE stock_code = ? AND date = ? AND close IS NOT NULL",
        (code, date_value),
    ).fetchone()
    return float(row["close"]) if row else None


def _open_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM ai_ops_positions WHERE status = 'OPEN' ORDER BY entry_date, id").fetchall()


def _ensure_account(conn: sqlite3.Connection, initial_capital: float) -> sqlite3.Row:
    account = conn.execute("SELECT * FROM ai_ops_account WHERE id = 1").fetchone()
    if not account:
        conn.execute(
            "INSERT INTO ai_ops_account (id, initial_capital, cash, updated_at) VALUES (1, ?, ?, datetime('now'))",
            (initial_capital, initial_capital),
        )
        conn.commit()
        account = conn.execute("SELECT * FROM ai_ops_account WHERE id = 1").fetchone()
    return account


def _ensure_ai_ops_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ai_ops_account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            initial_capital REAL NOT NULL,
            cash REAL NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_ops_runs (
            run_date TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_ops_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            entry_price REAL NOT NULL,
            shares INTEGER NOT NULL,
            cost REAL NOT NULL,
            stop_price REAL NOT NULL,
            target_price REAL NOT NULL,
            score REAL,
            signal TEXT,
            status TEXT NOT NULL,
            exit_date TEXT,
            exit_price REAL,
            exit_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ai_ops_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            action TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            shares INTEGER NOT NULL,
            amount REAL NOT NULL,
            score REAL,
            return_pct REAL,
            reason TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_ops_equity (
            date TEXT PRIMARY KEY,
            cash REAL NOT NULL,
            position_value REAL NOT NULL,
            total_value REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _parse_first_price(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(str(text).split("-")[0])
    except ValueError:
        return None


def _parse_last_price(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(str(text).split("-")[-1])
    except ValueError:
        return None


def _median(values: list[float]) -> float:
    clean = sorted(values)
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2


def _max_drawdown(values: list[float]) -> float:
    peak = None
    worst = 0.0
    for value in values:
        peak = value if peak is None else max(peak, value)
        if peak:
            worst = min(worst, (value / peak - 1) * 100)
    return worst


def _empty_payload(initial_capital: float) -> dict:
    return {
        "summary": {
            "max_positions": MAX_POSITIONS,
            "horizon": HOLD_DAYS,
            "step": 1,
            "cost_bps": int(COST_RATE * 10000),
            "start_date": None,
            "trades": 0,
            "open_positions": 0,
            "initial_capital": initial_capital,
            "final_capital": initial_capital,
            "total_return": 0,
            "win_rate": 0,
            "median_trade_return": 0,
            "max_drawdown": 0,
        },
        "curve": [],
        "recent_entries": [],
        "closed_trades": [],
        "recent_trades": [],
        "open_positions": [],
        "today_actions": [],
    }
