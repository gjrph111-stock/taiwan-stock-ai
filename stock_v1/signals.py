import sqlite3
from datetime import date, timedelta

from .indicators import is_new_high, macd, pct_change, rsi, sma, volume_ratio
from .names import short_name


def rank_signals(conn: sqlite3.Connection, limit: int = 20) -> dict:
    ranked = []
    for stock, rows in load_signal_rows(conn):
        signal = score_stock(stock, rows)
        if signal and passes_risk_filter(signal):
            signal["risk_adjusted_score"] = risk_adjusted_score(signal)
            ranked.append(signal)

    ranked.sort(
        key=lambda item: (item["risk_adjusted_score"], item["score"], item["return_20d"] or -9999),
        reverse=True,
    )
    return {
        "count": len(ranked),
        "top_signals": ranked[:limit],
        "summary": {
            "strong": sum(1 for item in ranked if item["signal"] == "strong"),
            "watch": sum(1 for item in ranked if item["signal"] == "watch"),
            "neutral": sum(1 for item in ranked if item["signal"] == "neutral"),
            "weak": sum(1 for item in ranked if item["signal"] == "weak"),
        },
    }


def load_signal_rows(conn: sqlite3.Connection, lookback_days: int = 260) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
    stocks = conn.execute("SELECT code, name, market FROM stocks ORDER BY code").fetchall()
    stock_map = {stock["code"]: stock for stock in stocks}
    rows_by_code = {stock["code"]: [] for stock in stocks}
    start_date = (date.today() - timedelta(days=lookback_days)).isoformat()

    for row in conn.execute(
        """
        SELECT stock_code, date, open, high, low, close, volume
        FROM prices
        WHERE date >= ?
        ORDER BY stock_code, date
        """,
        (start_date,),
    ):
        rows = rows_by_code.get(row["stock_code"])
        if rows is not None:
            rows.append(row)

    return [
        (stock_map[code], rows)
        for code, rows in rows_by_code.items()
        if len(rows) >= 80
    ]


def score_stock(stock: sqlite3.Row, rows: list[sqlite3.Row]) -> dict | None:
    clean_rows = [row for row in rows if row["close"] is not None]
    if len(clean_rows) < 80:
        return None

    closes = [row["close"] for row in clean_rows]
    volumes = [row["volume"] or 0 for row in clean_rows]
    close = closes[-1]
    ret20 = pct_change(closes, 20)
    ret60 = pct_change(closes, 60)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    rsi14 = rsi(closes, 14)
    volx = volume_ratio(volumes)
    macd_values = macd(closes)
    high60 = is_new_high(closes, 60)
    trade = trade_levels(clean_rows, close, ma20, ma60)
    drawdown = drawdown_from_high(clean_rows, close)

    score = 35
    reasons = []
    cautions = []

    if ma20 and close > ma20:
        score += 4
        reasons.append("above SMA20")
    else:
        score -= 8
        cautions.append("below SMA20")

    if ma60 and close > ma60:
        score += 5
        reasons.append("above SMA60")
    else:
        score -= 8
        cautions.append("below SMA60")

    if ret20 is not None:
        if 3 <= ret20 <= 15:
            score += 6
            reasons.append(f"20D return {ret20:.1f}%")
        elif 15 < ret20 <= 28:
            score += 12
            reasons.append(f"20D return {ret20:.1f}%")
        elif 28 < ret20 <= 45:
            score += 14
            reasons.append(f"20D return {ret20:.1f}%")
            cautions.append("short-term move is extended")
        elif ret20 > 45:
            score -= 12
            cautions.append("20D move may be overheated")
        elif ret20 < -8:
            score -= 10
            cautions.append(f"20D return {ret20:.1f}%")

    if ret60 is not None:
        if 5 <= ret60 <= 25:
            score += 5
            reasons.append(f"60D return {ret60:.1f}%")
        elif 25 < ret60 <= 55:
            score += 11
            reasons.append(f"60D return {ret60:.1f}%")
        elif 55 < ret60 <= 90:
            score += 13
            reasons.append(f"60D return {ret60:.1f}%")
            cautions.append("60D move is extended")
        elif ret60 < -12:
            score -= 8
            cautions.append(f"60D return {ret60:.1f}%")

    if volx is not None:
        if 1.15 <= volx <= 2.8:
            score += 6
            reasons.append(f"volume x{volx:.2f}")
        elif volx > 4:
            score -= 5
            cautions.append("volume spike may be unstable")

    if rsi14 is not None:
        if 45 <= rsi14 <= 70:
            score += 4
            reasons.append(f"RSI {rsi14:.1f}")
        elif rsi14 > 82:
            score -= 8
            cautions.append("RSI overheated")
        elif rsi14 < 35:
            score -= 8
            cautions.append("RSI weak")

    histogram = macd_values["histogram"]
    if histogram is not None:
        if histogram > 0:
            score += 2
            reasons.append("MACD positive")
        else:
            score -= 4
            cautions.append("MACD negative")

    if high60:
        score += 6
        reasons.append("new 60D high")

    score = max(0, min(100, round(score)))
    signal = _label(score)
    drawdown_alert = drawdown_warning(signal, drawdown)
    if drawdown_alert:
        cautions.append(drawdown_alert)
    return {
        "code": stock["code"],
        "name": stock["name"],
        "short_name": short_name(stock["name"]),
        "market": stock["market"],
        "date": clean_rows[-1]["date"],
        "close": close,
        "score": score,
        "signal": signal,
        "return_20d": ret20,
        "return_60d": ret60,
        "volume_ratio": volx,
        "rsi_14": rsi14,
        "new_high_60": high60,
        "drawdown_pct": drawdown,
        "drawdown_alert": drawdown_alert,
        "entry_zone": trade["entry_zone"],
        "exit_zone": trade["exit_zone"],
        "stop": trade["stop"],
        "reasons": reasons[:4],
        "cautions": cautions[:3],
    }


def drawdown_from_high(rows: list[sqlite3.Row], close: float, window: int = 120) -> float | None:
    recent = rows[-window:] if len(rows) >= window else rows
    highs = [row["high"] for row in recent if "high" in row.keys() and row["high"] is not None]
    if not highs:
        highs = [row["close"] for row in recent if row["close"] is not None]
    if not highs:
        return None
    peak = max(highs)
    if not peak:
        return None
    return (close / peak - 1) * 100


def drawdown_warning(signal: str, drawdown: float | None) -> str:
    if drawdown is None:
        return ""
    threshold = -25 if signal == "strong" else -35
    if drawdown <= threshold:
        label = "強勢股" if signal == "strong" else "一般股"
        return f"{label}回撤達 {abs(drawdown):.1f}%（提醒線 {abs(threshold)}%）"
    return ""


def trade_levels(rows: list[sqlite3.Row], close: float, ma20: float | None, ma60: float | None) -> dict:
    recent = rows[-20:] if len(rows) >= 20 else rows
    lows = [row["low"] for row in recent if "low" in row.keys() and row["low"] is not None]
    highs = [row["high"] for row in recent if "high" in row.keys() and row["high"] is not None]
    if not lows or not highs:
        buy_low = close * 0.97
        buy_high = close
        sell_low = close * 1.06
        sell_high = close * 1.12
        stop = close * 0.94
    else:
        recent_low = min(lows)
        recent_high = max(highs)
        supports = [value for value in (recent_low, ma20, ma60) if value is not None and value <= close]
        resistances = [value for value in (recent_high, ma20, ma60) if value is not None and value >= close]
        support = max(supports) if supports else recent_low
        resistance = min(resistances) if resistances else recent_high
        buy_low = support
        buy_high = min(close, support * 1.03)
        sell_low = resistance
        sell_high = max(resistance, close * 1.08)
        stop = support * 0.97
    return {
        "entry_zone": f"{buy_low:.2f}-{buy_high:.2f}",
        "exit_zone": f"{sell_low:.2f}-{sell_high:.2f}",
        "stop": f"{stop:.2f}",
    }


def passes_risk_filter(signal: dict) -> bool:
    ret20 = signal.get("return_20d")
    ret60 = signal.get("return_60d")
    rsi14 = signal.get("rsi_14")
    volx = signal.get("volume_ratio")

    if ret20 is not None and ret20 > 35:
        return False
    if ret60 is not None and ret60 > 80:
        return False
    if rsi14 is not None and rsi14 > 78:
        return False
    if volx is not None and volx > 3.2:
        return False
    return True


def risk_adjusted_score(signal: dict) -> int:
    score = signal["score"]
    ret20 = signal.get("return_20d")
    ret60 = signal.get("return_60d")
    rsi14 = signal.get("rsi_14")
    volx = signal.get("volume_ratio")

    penalty = 0
    if ret20 is not None and ret20 > 25:
        penalty += min(12, (ret20 - 25) * 0.7)
    if ret60 is not None and ret60 > 55:
        penalty += min(10, (ret60 - 55) * 0.25)
    if rsi14 is not None and rsi14 > 70:
        penalty += min(10, (rsi14 - 70) * 0.8)
    if volx is not None and volx > 2.2:
        penalty += min(8, (volx - 2.2) * 3)
    return max(0, round(score - penalty))


def _label(score: int) -> str:
    if score >= 75:
        return "strong"
    if score >= 60:
        return "watch"
    if score >= 45:
        return "neutral"
    return "weak"
