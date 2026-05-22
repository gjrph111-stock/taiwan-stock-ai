import json
import sqlite3
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .ai_monitor import build_ai_monitor, format_monitor_message
from .config import DEFAULT_DB_PATH
from .indicators import macd, pct_change, rsi, sma, volume_ratio
from .names import short_name
from .signals import rank_signals, risk_adjusted_score, score_stock
from .web import api_scan, api_status, api_strategy


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "notify.json"


def build_daily_message(db_path: Path = DEFAULT_DB_PATH, limit: int = 5) -> str:
    status = api_status(db_path)
    scan = api_scan(db_path, limit)
    strategy = api_strategy(db_path).get("market_context", {})
    watch_items = build_watchlist_advice(db_path, limit=limit)
    monitor = build_ai_monitor(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        signals = rank_signals(conn, limit)

    lines = [
        "台股智研｜觀察名單技術建議",
        f"資料日期：{status['last_date']}",
        "",
        f"盤面狀態：{strategy.get('stance', '資料不足')}",
        f"部位建議：{strategy.get('position_suggestion', '先觀察，等待資料完整')}",
        f"盤面廣度：站上月線 {scan['summary']['above_sma20']} 檔 / 站上季線 {scan['summary']['above_sma60']} 檔 / 60日新高 {scan['summary']['new_high_60']} 檔",
        "",
        "我的觀察名單",
    ]
    if watch_items:
        for item in watch_items:
            lines.extend(_format_watch_advice(item))
    else:
        lines.append("目前觀察名單沒有可分析標的，請先在網頁加入股票。")

    lines.extend(format_monitor_message(monitor, limit=limit))

    lines.extend(["", f"AI 訊號備選 Top {limit}"])
    for item in signals["top_signals"]:
        cautions = f" / risk: {', '.join(item['cautions'][:1])}" if item.get("cautions") else ""
        lines.extend([
            "",
            f"{item['code']} {item.get('short_name') or short_name(item['name'])}｜score {item['score']}｜{item['signal']}",
            f"動能：20D {item['return_20d']:.2f}%｜60D {item['return_60d']:.2f}%｜量比 {item.get('volume_ratio', 0):.2f}",
            f"建倉位：{item.get('entry_zone', '無資料')}",
            f"出倉位：{item.get('exit_zone', '無資料')}",
            f"回撤：{item.get('drawdown_pct', 0):.2f}%{('｜' + item.get('drawdown_alert')) if item.get('drawdown_alert') else ''}",
            f"停損位：{item.get('stop', '無資料')}{cautions}",
        ])

    lines.extend([
        "",
        "提醒：以上是依日線資料、技術指標與風控條件產生的研究建議，不是保證獲利或直接下單指令。進出場請自行搭配即時價格、成交量與停損紀律。",
    ])
    return "\n".join(lines)


def build_watchlist_advice(db_path: Path = DEFAULT_DB_PATH, limit: int | None = None) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_watchlist(conn)
        rows = conn.execute(
            """
            SELECT w.code, s.name, s.market
            FROM watchlist w
            JOIN stocks s ON s.code = w.code
            ORDER BY w.created_at, w.code
            """
        ).fetchall()
        if limit:
            rows = rows[:limit]
        return [_analyze_watch_code(conn, row) for row in rows]


def _analyze_watch_code(conn: sqlite3.Connection, stock: sqlite3.Row) -> dict:
    prices = conn.execute(
        """
        SELECT date, open, high, low, close, volume
        FROM prices
        WHERE stock_code = ? AND close IS NOT NULL
        ORDER BY date
        """,
        (stock["code"],),
    ).fetchall()
    if len(prices) < 60:
        return {
            "code": stock["code"],
            "name": short_name(stock["name"]),
            "summary": "資料不足",
            "advice": "先觀察，等待至少 60 筆日線資料。",
        }

    closes = [row["close"] for row in prices]
    highs = [row["high"] for row in prices if row["high"] is not None]
    lows = [row["low"] for row in prices if row["low"] is not None]
    volumes = [row["volume"] or 0 for row in prices]
    latest = prices[-1]
    close = closes[-1]
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    rsi14 = rsi(closes, 14)
    macd_hist = macd(closes)["histogram"]
    volx = volume_ratio(volumes)
    ret20 = pct_change(closes, 20)
    ret60 = pct_change(closes, 60)
    recent_high = max(highs[-20:]) if len(highs) >= 20 else max(highs)
    recent_low = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    support = max(value for value in [recent_low, ma20, ma60] if value is not None and value <= close)
    resistance_candidates = [value for value in [recent_high, ma20, ma60] if value is not None and value >= close]
    resistance = min(resistance_candidates) if resistance_candidates else recent_high
    buy_low = support
    buy_high = min(close, support * 1.03)
    stop = support * 0.97
    breakout = resistance * 1.01
    take_profit = max(resistance, close * 1.08)
    sell_low = resistance
    sell_high = take_profit

    signal = score_stock(stock, prices)
    score = risk_adjusted_score(signal) if signal else None
    drawdown_alert = signal.get("drawdown_alert") if signal else ""
    drawdown_pct = signal.get("drawdown_pct") if signal else None
    trend = _trend_label(close, ma20, ma60, macd_hist)
    advice = _trade_advice(close, ma20, ma60, rsi14, macd_hist, ret20, score)
    summary = (
        f"收 {close:.2f} / 20D {ret20:.2f}% / 60D {ret60:.2f}% / "
        f"RSI {rsi14:.1f} / 量比 {volx:.2f}"
    )
    return {
        "code": stock["code"],
        "name": short_name(stock["name"]),
        "date": latest["date"],
        "close": close,
        "score": score,
        "drawdown_pct": drawdown_pct,
        "drawdown_alert": drawdown_alert,
        "trend": trend,
        "summary": summary,
        "buy_zone": (buy_low, buy_high),
        "sell_zone": (sell_low, sell_high),
        "breakout": breakout,
        "stop": stop,
        "resistance": resistance,
        "take_profit": take_profit,
        "advice": advice,
    }


def _format_watch_advice(item: dict) -> list[str]:
    if "buy_zone" not in item:
        return [
            "",
            f"{item['code']} {item['name']}",
            f"狀態：{item['summary']}",
            "買點：資料不足",
            "賣點：資料不足",
            "停損：資料不足",
            f"建議：{item['advice']}",
        ]
    buy_low, buy_high = item["buy_zone"]
    sell_low, sell_high = item["sell_zone"]
    drawdown_text = f"{item['drawdown_pct']:.2f}%" if item.get("drawdown_pct") is not None else "無資料"
    return [
        "",
        f"{item['code']} {item['name']}｜{item['trend']}｜分數 {item['score']}",
        f"技術：{item['summary']}",
        f"買點：拉回 {buy_low:.2f} - {buy_high:.2f} 可觀察；放量突破 {item['breakout']:.2f} 可列追蹤點",
        f"賣點：壓力 {item['resistance']:.2f}；分批獲利區 {sell_low:.2f} - {sell_high:.2f}",
        f"回撤提醒：{drawdown_text}{('｜' + item['drawdown_alert']) if item.get('drawdown_alert') else '｜未達提醒線'}",
        f"停損：跌破 {item['stop']:.2f} 視為轉弱，需降低部位或退出觀察",
        f"建議：{item['advice']}",
    ]


def _trend_label(close, ma20, ma60, macd_hist) -> str:
    if ma20 is not None and ma60 is not None and close > ma20 > ma60 and (macd_hist or 0) > 0:
        return "多頭排列"
    if ma20 is not None and close > ma20 and (macd_hist or 0) >= 0:
        return "偏多整理"
    if ma20 is not None and close < ma20 and (macd_hist or 0) < 0:
        return "轉弱觀察"
    return "區間震盪"


def _trade_advice(close, ma20, ma60, rsi14, macd_hist, ret20, score) -> str:
    if rsi14 is not None and rsi14 >= 75:
        return "短線偏熱，不建議追高；已有部位可分批停利，等回測支撐再評估。"
    if ret20 is not None and ret20 > 25:
        return "20 日漲幅偏大，等待拉回或突破後回測確認，不宜一次買滿。"
    if ma20 is not None and ma60 is not None and close > ma20 > ma60 and (macd_hist or 0) > 0:
        return "趨勢偏多，可用拉回月線附近分批布局，停損放在支撐下方。"
    if ma20 is not None and close < ma20:
        return "收盤仍在月線下方，先觀望；等重新站回月線且量能回溫再考慮。"
    if score is not None and score >= 65:
        return "訊號分數佳，可列入優先觀察，但仍需分批與停損。"
    return "適合放在觀察名單，等待量價或均線結構更明確。"


def _ensure_watchlist(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            code TEXT PRIMARY KEY,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (code) REFERENCES stocks(code)
        )
        """
    )
    existing = conn.execute("SELECT COUNT(*) AS n FROM watchlist").fetchone()["n"]
    if existing == 0:
        conn.executemany(
            "INSERT OR IGNORE INTO watchlist (code, created_at) VALUES (?, datetime('now'))",
            [(code,) for code in ["2330", "2317", "2454", "2308", "2412", "2882"]],
        )
        conn.commit()


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. Copy config/notify.example.json to config/notify.json first."
        )
    return json.loads(config_path.read_text(encoding="utf-8"))


def send_telegram(message: str, config: dict) -> dict:
    telegram = config.get("telegram", {})
    bot_token = telegram.get("bot_token")
    chat_id = telegram.get("chat_id")
    if not bot_token or not chat_id or "PASTE_" in bot_token or "PASTE_" in chat_id:
        raise ValueError("Telegram bot_token/chat_id is not configured in config/notify.json.")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }
    return _post_json(url, payload)


def send_line(message: str, config: dict) -> dict:
    line = config.get("line", {})
    token = line.get("channel_access_token")
    to = line.get("to")
    if not token or not to or "PASTE_" in token or "PASTE_" in to:
        raise ValueError("LINE channel_access_token/to is not configured in config/notify.json.")

    url = "https://api.line.me/v2/bot/message/push"
    payload = {
        "to": to,
        "messages": [{"type": "text", "text": message[:4900]}],
    }
    return _post_json(url, payload, headers={"Authorization": f"Bearer {token}"})


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc)) from exc
    return json.loads(raw) if raw else {"ok": True}
