import sqlite3

from .indicators import fmt, is_new_high, macd, pct_change, rsi, sma, volume_ratio
from .names import short_name
from .signals import rank_signals


def print_stock_report(conn: sqlite3.Connection, code: str) -> None:
    stock = conn.execute("SELECT * FROM stocks WHERE code = ?", (code,)).fetchone()
    if not stock:
        print(f"Stock {code} was not found.")
        return

    summary = conn.execute(
        """
        SELECT COUNT(*) AS rows, MIN(date) AS first_date, MAX(date) AS last_date
        FROM prices
        WHERE stock_code = ?
        """,
        (code,),
    ).fetchone()
    latest = conn.execute(
        """
        SELECT date, open, high, low, close, volume
        FROM prices
        WHERE stock_code = ?
        ORDER BY date DESC
        LIMIT 1
        """,
        (code,),
    ).fetchone()

    print(f"{stock['code']} {stock['name']} ({stock['market']})")
    print(f"Yahoo symbol: {stock['yahoo_symbol']}")
    print(f"Industry: {stock['industry'] or 'N/A'}")
    print(f"Rows: {summary['rows']}")
    print(f"Date range: {summary['first_date']} -> {summary['last_date']}")
    if latest:
        print(
            "Latest: "
            f"{latest['date']} open={fmt(latest['open'])} high={fmt(latest['high'])} "
            f"low={fmt(latest['low'])} close={fmt(latest['close'])} volume={fmt(latest['volume'])}"
        )


def print_indicator_report(conn: sqlite3.Connection, code: str) -> None:
    rows = _price_rows(conn, code)
    if not rows:
        print(f"No price data for {code}.")
        return

    stock = conn.execute("SELECT * FROM stocks WHERE code = ?", (code,)).fetchone()
    closes = [row["close"] for row in rows if row["close"] is not None]
    volumes = [row["volume"] or 0 for row in rows]
    macd_values = macd(closes)

    if stock:
        print(f"{stock['code']} {stock['name']}")
    print(f"Latest date: {rows[-1]['date']}")
    print(f"Close: {fmt(closes[-1])}")
    print(f"Return 5D: {fmt(pct_change(closes, 5))}%")
    print(f"Return 20D: {fmt(pct_change(closes, 20))}%")
    print(f"SMA 5: {fmt(sma(closes, 5))}")
    print(f"SMA 20: {fmt(sma(closes, 20))}")
    print(f"SMA 60: {fmt(sma(closes, 60))}")
    print(f"RSI 14: {fmt(rsi(closes, 14))}")
    print(f"MACD: {fmt(macd_values['macd'])}")
    print(f"MACD signal: {fmt(macd_values['signal'])}")
    print(f"MACD histogram: {fmt(macd_values['histogram'])}")
    print(f"Volume ratio 5D/20D: {fmt(volume_ratio(volumes))}")
    print(f"New 60D high: {fmt(is_new_high(closes, 60))}")


def print_market_scan(conn: sqlite3.Connection, limit: int) -> None:
    stocks = conn.execute("SELECT code, name, market FROM stocks ORDER BY code").fetchall()
    results = []

    for stock in stocks:
        rows = _price_rows(conn, stock["code"])
        if len(rows) < 60:
            continue
        closes = [row["close"] for row in rows if row["close"] is not None]
        volumes = [row["volume"] or 0 for row in rows]
        if len(closes) < 60:
            continue
        ma20 = sma(closes, 20)
        ma60 = sma(closes, 60)
        results.append(
            {
                "code": stock["code"],
                "name": stock["name"],
                "market": stock["market"],
                "date": rows[-1]["date"],
                "close": closes[-1],
                "ret20": pct_change(closes, 20),
                "ret60": pct_change(closes, 60),
                "vol_ratio": volume_ratio(volumes),
                "rsi14": rsi(closes, 14),
                "new_high60": is_new_high(closes, 60),
                "above_ma20": ma20 is not None and closes[-1] > ma20,
                "above_ma60": ma60 is not None and closes[-1] > ma60,
            }
        )

    print(f"Scanned stocks with enough data: {len(results)}")
    print("")
    _print_table("Top 20D return", sorted(results, key=lambda x: x["ret20"] or -9999, reverse=True), limit)
    print("")
    _print_table("Top volume expansion", sorted(results, key=lambda x: x["vol_ratio"] or -9999, reverse=True), limit)
    print("")
    high_count = sum(1 for item in results if item["new_high60"])
    above_ma20 = sum(1 for item in results if item["above_ma20"])
    above_ma60 = sum(1 for item in results if item["above_ma60"])
    print(f"New 60D highs: {high_count}")
    print(f"Above SMA20: {above_ma20}")
    print(f"Above SMA60: {above_ma60}")


def _price_rows(conn: sqlite3.Connection, code: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT date, open, high, low, close, volume
        FROM prices
        WHERE stock_code = ?
        ORDER BY date
        """,
        (code,),
    ).fetchall()


def _print_table(title: str, rows: list[dict], limit: int) -> None:
    print(title)
    print("code  name                  close     ret20%    ret60%    volx     rsi")
    print("----  --------------------  --------  --------  --------  -------  ------")
    for item in rows[:limit]:
        name = short_name(item["name"], 10)
        print(
            f"{item['code']:<4}  {name:<20}  "
            f"{fmt(item['close']):>8}  {fmt(item['ret20']):>8}  "
            f"{fmt(item['ret60']):>8}  {fmt(item['vol_ratio']):>7}  {fmt(item['rsi14']):>6}"
        )


def print_signal_report(conn: sqlite3.Connection, limit: int) -> None:
    result = rank_signals(conn, limit)
    print(f"Scored stocks with enough data: {result['count']}")
    print(f"Summary: {result['summary']}")
    print("")
    print("code  name                  score  signal   close     ret20%    ret60%    volx    reasons")
    print("----  --------------------  -----  -------  --------  --------  --------  ------  ------------------------------")
    for item in result["top_signals"]:
        name = item.get("short_name") or short_name(item["name"], 10)
        reasons = ", ".join(item["reasons"])
        print(
            f"{item['code']:<4}  {name:<20}  {item['score']:>5}  {item['signal']:<7}  "
            f"{fmt(item['close']):>8}  {fmt(item['return_20d']):>8}  "
            f"{fmt(item['return_60d']):>8}  {fmt(item['volume_ratio']):>6}  {reasons}"
        )


def print_watchlist(conn: sqlite3.Connection, limit: int = 5) -> None:
    result = rank_signals(conn, limit)
    print(f"Today watchlist Top {limit}")
    print("Suggested holding/observation window: 10-20 trading days")
    print("")
    for idx, item in enumerate(result["top_signals"], start=1):
        name = item.get("short_name") or short_name(item["name"], 10)
        cautions = f" | risk: {', '.join(item['cautions'])}" if item.get("cautions") else ""
        print(
            f"{idx}. {item['code']} {name} "
            f"score {item['score']} / {item['signal']} / "
            f"20D {fmt(item['return_20d'])}% / 60D {fmt(item['return_60d'])}% / "
            f"vol {fmt(item['volume_ratio'])}{cautions}"
        )
