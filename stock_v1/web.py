import json
import os
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from .ai_monitor import analyze_stock, build_ai_monitor
from .backtest import high_win_strategy_backtest, realistic_strategy_backtest
from . import db
from .config import DEFAULT_DB_PATH
from .finmind import fetch_finmind_institutional, fetch_finmind_kbar, fetch_recent_finmind_prices
from .fundamental import build_fundamental_analysis
from .indicators import is_new_high, macd, pct_change, rsi, sma, volume_ratio
from .industry import industry_profile
from .names import short_name
from .news import fetch_stock_news
from .signals import load_signal_rows, rank_signals, risk_adjusted_score, score_stock


_FUNDAMENTAL_CACHE: dict[tuple[str, str | None], dict] = {}
_STRATEGY_CACHE: dict[str | None, dict] = {}


def run(host: str = "127.0.0.1", port: int = 8765, db_path: Path = DEFAULT_DB_PATH) -> None:
    handler = _make_handler(db_path)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


def _make_handler(db_path: Path):
    class StockHandler(BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            self.send_response(204)
            self._send_common_headers()
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            try:
                if parsed.path == "/":
                    self._send_html(INDEX_HTML)
                elif parsed.path == "/api/status":
                    self._send_json(api_status(db_path))
                elif parsed.path == "/api/public-config":
                    self._send_json(api_public_config())
                elif parsed.path == "/api/stock":
                    self._send_json(api_stock(db_path, _param(params, "code", "2330")))
                elif parsed.path == "/api/indicators":
                    self._send_json(api_indicators(db_path, _param(params, "code", "2330")))
                elif parsed.path == "/api/prices":
                    code = _param(params, "code", "2330")
                    limit = int(_param(params, "limit", "120"))
                    self._send_json(api_prices(db_path, code, limit))
                elif parsed.path == "/api/stock-signal":
                    self._send_json(api_stock_signal(db_path, _param(params, "code", "2330")))
                elif parsed.path == "/api/scan":
                    limit = int(_param(params, "limit", "20"))
                    self._send_json(api_scan(db_path, limit))
                elif parsed.path == "/api/signals":
                    limit = int(_param(params, "limit", "20"))
                    self._send_json(api_signals(db_path, limit))
                elif parsed.path == "/api/strategy":
                    self._send_json(api_strategy(db_path))
                elif parsed.path == "/api/watch":
                    self._send_json(api_watch(db_path))
                elif parsed.path == "/api/ai-monitor":
                    self._send_json(api_ai_monitor(db_path))
                elif parsed.path == "/api/ai-monitor-stock":
                    self._send_json(api_ai_monitor_stock(db_path, _param(params, "code", "2330")))
                elif parsed.path == "/api/fundamental":
                    self._send_json(api_fundamental(db_path, _param(params, "code", "2330")))
                elif parsed.path == "/api/news":
                    self._send_json(api_news(db_path, _param(params, "code", "2330")))
                elif parsed.path == "/api/watchlist":
                    self._send_json(api_watchlist(db_path, _param(params, "codes", "")))
                elif parsed.path == "/api/watchlist/add":
                    self._send_json(api_watchlist_add(db_path, _param(params, "code", "")))
                elif parsed.path == "/api/watchlist/remove":
                    self._send_json(api_watchlist_remove(db_path, _param(params, "code", "")))
                elif parsed.path == "/api/user/create":
                    self._send_json(api_user_create(db_path, _param(params, "name", "")))
                elif parsed.path == "/api/user/profile":
                    self._send_json(api_user_profile(db_path, _param(params, "user_key", "")))
                elif parsed.path == "/api/user/watchlist":
                    self._send_json(api_user_watchlist(db_path, _param(params, "user_key", "")))
                elif parsed.path == "/api/user/watchlist/add":
                    self._send_json(api_user_watchlist_add(db_path, _param(params, "user_key", ""), _param(params, "code", "")))
                elif parsed.path == "/api/user/watchlist/remove":
                    self._send_json(api_user_watchlist_remove(db_path, _param(params, "user_key", ""), _param(params, "code", "")))
                elif parsed.path == "/api/user/telegram/save":
                    self._send_json(api_user_telegram_save(db_path, _param(params, "user_key", ""), _param(params, "chat_id", "")))
                elif parsed.path == "/api/user/telegram/test":
                    self._send_json(api_user_telegram_test(db_path, _param(params, "user_key", "")))
                elif parsed.path == "/api/realtime":
                    codes = _param(params, "codes", "2330,2317,2454,2308,2412,2882")
                    self._send_json(api_realtime(db_path, codes))
                elif parsed.path == "/api/realtime-trend":
                    self._send_json(api_realtime_trend(db_path, _param(params, "code", "2330"), _param(params, "interval", "1d")))
                elif parsed.path == "/api/institutional":
                    self._send_json(api_institutional(db_path, _param(params, "code", "2330")))
                else:
                    self._send_json({"error": "not found"}, status=404)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)

        def do_POST(self):
            parsed = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
                if parsed.path == "/api/telegram/webhook":
                    self._send_json(api_telegram_webhook(db_path, payload))
                else:
                    self._send_json({"error": "not found"}, status=404)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)

        def log_message(self, format, *args):
            return

        def _send_html(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._send_common_headers()
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, payload: dict, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_common_headers()
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_common_headers(self) -> None:
            origin = self.headers.get("Origin")
            allowed = _allowed_origin(origin)
            if allowed:
                self.send_header("Access-Control-Allow-Origin", allowed)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    return StockHandler


def _allowed_origin(origin: str | None) -> str | None:
    allowed_origins = os.environ.get("STOCK_V1_ALLOWED_ORIGINS", "").strip()
    if not allowed_origins:
        return None
    if allowed_origins == "*":
        return "*"
    allowed = {item.strip().rstrip("/") for item in allowed_origins.split(",") if item.strip()}
    if origin and origin.rstrip("/") in allowed:
        return origin
    return None


def _public_demo_mode() -> bool:
    return os.environ.get("STOCK_V1_PUBLIC_DEMO", "").strip().lower() in {"1", "true", "yes", "on"}


def api_status(db_path: Path) -> dict:
    with _connect(db_path) as conn:
        stocks = conn.execute("SELECT COUNT(*) AS n FROM stocks").fetchone()["n"]
        prices = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
        markets = conn.execute(
            "SELECT market, COUNT(*) AS n FROM stocks GROUP BY market ORDER BY market"
        ).fetchall()
        date_range = conn.execute(
            "SELECT MIN(date) AS first_date, MAX(date) AS last_date FROM prices"
        ).fetchone()
        latest_count = conn.execute(
            "SELECT COUNT(*) AS n FROM prices WHERE date = ?",
            (date_range["last_date"],),
        ).fetchone()["n"] if date_range["last_date"] else 0
        official_count = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM prices
            WHERE date = ? AND source IN ('official_twse', 'official_tpex')
            """,
            (date_range["last_date"],),
        ).fetchone()["n"] if date_range["last_date"] else 0
    return {
        "stocks": stocks,
        "prices": prices,
        "markets": {row["market"]: row["n"] for row in markets},
        "first_date": date_range["first_date"],
        "last_date": date_range["last_date"],
        "latest_count": latest_count,
        "official_count": official_count,
    }


def api_stock(db_path: Path, code: str) -> dict:
    with _connect(db_path) as conn:
        stock = conn.execute("SELECT * FROM stocks WHERE code = ?", (code,)).fetchone()
        if not stock:
            return {"error": f"Stock {code} was not found."}
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
            SELECT date, open, high, low, close, volume, source, updated_at
            FROM prices
            WHERE stock_code = ?
            ORDER BY date DESC
            LIMIT 1
            """,
            (code,),
        ).fetchone()
    industry = industry_profile(stock["code"], stock["name"], stock["industry"])
    return {
        "code": stock["code"],
        "name": stock["name"],
        "short_name": short_name(stock["name"]),
        "market": stock["market"],
        "industry": industry["category"],
        "industry_raw": industry["raw"],
        "industry_profile": industry,
        "yahoo_symbol": stock["yahoo_symbol"],
        "rows": summary["rows"],
        "first_date": summary["first_date"],
        "last_date": summary["last_date"],
        "latest": dict(latest) if latest else None,
    }


def api_indicators(db_path: Path, code: str) -> dict:
    with _connect(db_path) as conn:
        stock = conn.execute("SELECT code, name, market FROM stocks WHERE code = ?", (code,)).fetchone()
        rows = _price_rows(conn, code)
    if not rows:
        return {"error": f"No price data for {code}."}
    closes = [row["close"] for row in rows if row["close"] is not None]
    volumes = [row["volume"] or 0 for row in rows]
    macd_values = macd(closes)
    return {
        "stock": dict(stock) if stock else {"code": code},
        "latest_date": rows[-1]["date"],
        "close": closes[-1],
        "return_5d": pct_change(closes, 5),
        "return_20d": pct_change(closes, 20),
        "return_60d": pct_change(closes, 60),
        "sma_5": sma(closes, 5),
        "sma_20": sma(closes, 20),
        "sma_60": sma(closes, 60),
        "rsi_14": rsi(closes, 14),
        "macd": macd_values["macd"],
        "macd_signal": macd_values["signal"],
        "macd_histogram": macd_values["histogram"],
        "volume_ratio": volume_ratio(volumes),
        "new_high_60": is_new_high(closes, 60),
    }


def api_prices(db_path: Path, code: str, limit: int = 120) -> dict:
    with _connect(db_path) as conn:
        stock = conn.execute("SELECT code, name FROM stocks WHERE code = ?", (code,)).fetchone()
        rows = conn.execute(
            """
            SELECT date, open, high, low, close, volume
            FROM prices
            WHERE stock_code = ? AND close IS NOT NULL
            ORDER BY date DESC
            LIMIT ?
            """,
            (code, limit),
        ).fetchall()
    ordered = [dict(row) for row in reversed(rows)]
    return {
        "stock": dict(stock) if stock else {"code": code, "name": code},
        "prices": ordered,
    }


def api_stock_signal(db_path: Path, code: str) -> dict:
    with _connect(db_path) as conn:
        stock = conn.execute("SELECT code, name, market FROM stocks WHERE code = ?", (code,)).fetchone()
        rows = _price_rows(conn, code)
    if not stock or not rows:
        return {"error": f"No signal data for {code}."}
    signal = score_stock(stock, rows)
    if not signal:
        return {"error": f"Not enough data for {code}."}
    signal["risk_adjusted_score"] = risk_adjusted_score(signal)
    signal["intelli_score"] = round(signal["risk_adjusted_score"] / 10, 1)
    signal["sentiment"] = _sentiment_label(signal.get("rsi_14"))
    return signal


def api_scan(db_path: Path, limit: int = 20) -> dict:
    with _connect(db_path) as conn:
        results = []
        for stock, rows in load_signal_rows(conn):
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
                    "short_name": short_name(stock["name"]),
                    "market": stock["market"],
                    "date": rows[-1]["date"],
                    "close": closes[-1],
                    "return_20d": pct_change(closes, 20),
                    "return_60d": pct_change(closes, 60),
                    "volume_ratio": volume_ratio(volumes),
                    "rsi_14": rsi(closes, 14),
                    "new_high_60": is_new_high(closes, 60),
                    "above_sma20": ma20 is not None and closes[-1] > ma20,
                    "above_sma60": ma60 is not None and closes[-1] > ma60,
                }
            )
    return {
        "count": len(results),
        "top_return_20d": sorted(
            results, key=lambda item: item["return_20d"] if item["return_20d"] is not None else -9999, reverse=True
        )[:limit],
        "top_volume_expansion": sorted(
            results, key=lambda item: item["volume_ratio"] if item["volume_ratio"] is not None else -9999, reverse=True
        )[:limit],
        "summary": {
            "new_high_60": sum(1 for item in results if item["new_high_60"]),
            "above_sma20": sum(1 for item in results if item["above_sma20"]),
            "above_sma60": sum(1 for item in results if item["above_sma60"]),
        },
    }


def api_signals(db_path: Path, limit: int = 20) -> dict:
    with _connect(db_path) as conn:
        return rank_signals(conn, limit)


def api_strategy(db_path: Path) -> dict:
    strategy_start = "2026-05-01"
    strategy_capital = 200000.0
    with _connect(db_path) as conn:
        row = conn.execute("SELECT MAX(date) AS latest FROM prices").fetchone()
        latest = row["latest"] if row else None
    cache_key = f"{latest}|ai-ops-202605|200000"
    if cache_key in _STRATEGY_CACHE:
        return _STRATEGY_CACHE[cache_key]
    with _connect(db_path) as conn:
        result = realistic_strategy_backtest(
            conn,
            max_positions=5,
            horizon=5,
            step=5,
            max_days=None,
            initial_capital=strategy_capital,
            cost_bps=20,
            start_date=strategy_start,
        )
        high_win = high_win_strategy_backtest(conn, max_days=45)
        context = _strategy_market_context(conn, result)
    payload = {
        "summary": {
            "max_positions": result["max_positions"],
            "horizon": result["horizon"],
            "step": result["step"],
            "cost_bps": result["cost_bps"],
            "start_date": result.get("start_date") or strategy_start,
            "trades": result["trades"],
            "open_positions": result["open_positions"],
            "initial_capital": result["initial_capital"],
            "final_capital": result["final_capital"],
            "total_return": result["total_return"],
            "win_rate": result["win_rate"],
            "median_trade_return": result["median_trade_return"],
            "max_drawdown": result["max_drawdown"],
        },
        "market_context": context,
        "curve": result["curve"],
        "recent_entries": result.get("recent_entries", [])[-10:],
        "closed_trades": result["recent_trades"][-10:],
        "recent_trades": result["recent_trades"][-10:],
        "open_positions": result["open_position_details"],
        "high_win_strategy": high_win,
    }
    if len(_STRATEGY_CACHE) > 8:
        _STRATEGY_CACHE.clear()
    _STRATEGY_CACHE[cache_key] = payload
    return payload


def _strategy_market_context(conn: sqlite3.Connection, result: dict) -> dict:
    rows = []
    for stock, history in load_signal_rows(conn):
        if len(history) < 80:
            continue
        closes = [row["close"] for row in history if row["close"] is not None]
        volumes = [row["volume"] or 0 for row in history]
        if len(closes) < 80:
            continue
        signal = score_stock(stock, history)
        if not signal:
            continue
        signal["risk_adjusted_score"] = risk_adjusted_score(signal)
        ma20 = sma(closes, 20)
        ma60 = sma(closes, 60)
        rows.append(
            {
                "date": history[-1]["date"],
                "code": stock["code"],
                "name": short_name(stock["name"]),
                "close": closes[-1],
                "return_20d": pct_change(closes, 20),
                "return_60d": pct_change(closes, 60),
                "rsi_14": rsi(closes, 14),
                "volume_ratio": volume_ratio(volumes),
                "new_high_60": is_new_high(closes, 60),
                "above_sma20": ma20 is not None and closes[-1] > ma20,
                "above_sma60": ma60 is not None and closes[-1] > ma60,
                "signal": signal["signal"],
                "score": signal["risk_adjusted_score"],
            }
        )
    if not rows:
        return {
            "date": None,
            "stance": "資料不足",
            "position_suggestion": "暫不建立 AI 實操部位",
            "headline": "目前資料不足，請先更新市場資料。",
            "actions": ["先執行資料更新，再重新載入 AI 實操。"],
            "risks": [],
            "metrics": {},
            "leaders": [],
        }

    total = len(rows)
    above20 = sum(1 for row in rows if row["above_sma20"])
    above60 = sum(1 for row in rows if row["above_sma60"])
    highs = sum(1 for row in rows if row["new_high_60"])
    strong = sum(1 for row in rows if row["signal"] == "strong")
    watch = sum(1 for row in rows if row["signal"] == "watch")
    weak = sum(1 for row in rows if row["signal"] == "weak")
    rsi_values = [row["rsi_14"] for row in rows if row["rsi_14"] is not None]
    ret20_values = [row["return_20d"] for row in rows if row["return_20d"] is not None]
    ret60_values = [row["return_60d"] for row in rows if row["return_60d"] is not None]
    breadth20 = above20 / total
    breadth60 = above60 / total
    actionable = (strong + watch) / total
    avg_rsi = sum(rsi_values) / len(rsi_values) if rsi_values else None
    avg20 = sum(ret20_values) / len(ret20_values) if ret20_values else None
    avg60 = sum(ret60_values) / len(ret60_values) if ret60_values else None

    if breadth20 >= 0.55 and breadth60 >= 0.45 and actionable >= 0.08:
        stance = "偏多進攻"
        position = "建議 60% - 75% 研究資金，分批布局強勢但未過熱標的"
        headline = "市場廣度偏強，AI 實操可以主動找多頭續航股。"
    elif breadth20 >= 0.45 and breadth60 >= 0.35:
        stance = "中性偏多"
        position = "建議 40% - 60% 研究資金，保留現金等待回測後的高勝率切入點"
        headline = "盤面仍有多方支撐，但不宜追高過度擴張部位。"
    elif breadth20 < 0.35 or breadth60 < 0.30:
        stance = "防守觀望"
        position = "建議 20% - 35% 研究資金，以觀察名單和停損控管為主"
        headline = "市場廣度偏弱，AI 實操重點應放在防守與等待訊號轉強。"
    else:
        stance = "震盪選股"
        position = "建議 30% - 50% 研究資金，只挑量價結構明確的個股"
        headline = "盤面不是全面多頭，適合用個股強弱差做精選。"

    risks = []
    if avg_rsi is not None and avg_rsi > 62:
        risks.append("市場平均 RSI 偏高，追價時需要降低單筆部位。")
    if avg20 is not None and avg20 > 12:
        risks.append("20 日平均漲幅偏大，短線容易出現震盪洗盤。")
    if result.get("max_drawdown") is not None and result["max_drawdown"] < -8:
        risks.append("AI 實操歷史最大回撤偏大，建議把停損與持股上限放在第一優先。")
    if weak / total > 0.45:
        risks.append("弱勢訊號占比偏高，避免把資金平均分散到落後股。")

    actions = [
        "優先從 AI 訊號與觀察名單交集找標的，避免只看單日漲幅。",
        "進場分兩到三批，不用一次買滿，並以 20 日線或最近低點作為風控參考。",
        "若盤面跌破月線家數快速增加，AI 實操自動降到防守模式。",
    ]
    if stance in ("偏多進攻", "中性偏多"):
        actions.insert(0, "可優先研究 20 日與 60 日動能同時為正、RSI 未過熱的股票。")
    else:
        actions.insert(0, "先縮小股票池，只追蹤相對抗跌或量能轉強的股票。")

    leaders = sorted(rows, key=lambda row: (row["score"], row["return_20d"] or -9999), reverse=True)[:8]
    return {
        "date": rows[0]["date"],
        "stance": stance,
        "position_suggestion": position,
        "headline": headline,
        "actions": actions,
        "risks": risks or ["目前主要風險可控，但仍需依停損與部位規則執行。"],
        "metrics": {
            "stocks": total,
            "above_sma20": above20,
            "above_sma20_pct": breadth20 * 100,
            "above_sma60": above60,
            "above_sma60_pct": breadth60 * 100,
            "new_high_60": highs,
            "new_high_60_pct": highs / total * 100,
            "strong": strong,
            "watch": watch,
            "weak": weak,
            "actionable_pct": actionable * 100,
            "avg_rsi": avg_rsi,
            "avg_return_20d": avg20,
            "avg_return_60d": avg60,
        },
        "leaders": leaders,
    }


def api_watch(db_path: Path) -> dict:
    scan = api_scan(db_path, 8)
    signals = api_signals(db_path, 8)
    majors = [_watch_snapshot(db_path, code) for code in _watchlist_codes(db_path)]
    majors = [row for row in majors if row]
    return {
        "summary": scan["summary"],
        "signals": signals["top_signals"],
        "top_return_20d": scan["top_return_20d"],
        "top_volume_expansion": scan["top_volume_expansion"],
        "majors": majors,
    }


def api_ai_monitor(db_path: Path) -> dict:
    session = _market_session()
    if not session["is_intraday"]:
        return {
            "items": [],
            "summary": {
                "stance": "盤後模式",
                "urgent": 0,
                "watch": 0,
                "positive": 0,
                "message": "AI 盯盤只在台股盤中顯示。盤後請看觀察名單、AI 實操與個股分析。",
                **session,
            },
        }
    result = build_ai_monitor(db_path)
    result["summary"] = {**result.get("summary", {}), **session}
    return result


def api_ai_monitor_stock(db_path: Path, code: str) -> dict:
    with _connect(db_path) as conn:
        stock = conn.execute("SELECT code, name, market FROM stocks WHERE code = ?", (code.strip(),)).fetchone()
        if not stock:
            return {"error": f"找不到股票代號 {code}。"}
        return analyze_stock(conn, stock)


def api_fundamental(db_path: Path, code: str) -> dict:
    code = code.strip()
    latest = None
    with _connect(db_path) as conn:
        row = conn.execute("SELECT MAX(date) AS latest FROM prices WHERE stock_code = ?", (code,)).fetchone()
        latest = row["latest"] if row else None
    cache_key = (code, latest)
    if cache_key not in _FUNDAMENTAL_CACHE:
        if len(_FUNDAMENTAL_CACHE) > 80:
            _FUNDAMENTAL_CACHE.clear()
        _FUNDAMENTAL_CACHE[cache_key] = build_fundamental_analysis(db_path, code)
    return _FUNDAMENTAL_CACHE[cache_key]


def api_news(db_path: Path, code: str) -> dict:
    with _connect(db_path) as conn:
        stock = conn.execute("SELECT code, name FROM stocks WHERE code = ?", (code.strip(),)).fetchone()
        if not stock:
            return {"error": f"找不到股票代號 {code}。", "items": []}
        return fetch_stock_news(stock["code"], short_name(stock["name"]))


def _market_session() -> dict:
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    is_weekday = now.weekday() < 5
    intraday = is_weekday and time(9, 0) <= now.time() <= time(13, 30)
    return {
        "is_intraday": intraday,
        "session_label": "盤中盯盤" if intraday else "盤後整理",
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def api_public_config() -> dict:
    return {"public_demo": _public_demo_mode()}


def api_watchlist(db_path: Path, raw_codes: str = "") -> dict:
    codes = [code.strip() for code in raw_codes.split(",") if code.strip()]
    if not codes:
        codes = _watchlist_codes(db_path)
    rows = [_watch_snapshot(db_path, code) for code in codes]
    rows = [row for row in rows if row]
    return {"watchlist": rows, "codes": [row["code"] for row in rows]}


def api_watchlist_add(db_path: Path, code: str) -> dict:
    if _public_demo_mode():
        return {"error": "公開展示模式不會修改主機觀察名單，請使用瀏覽器本機觀察名單。"}
    code = code.strip()
    if not code:
        return {"error": "請輸入股票代號。"}
    with _connect(db_path) as conn:
        _ensure_watchlist(conn)
        stock = conn.execute("SELECT code FROM stocks WHERE code = ?", (code,)).fetchone()
        if not stock:
            return {"error": f"找不到股票代號 {code}。"}
        conn.execute(
            """
            INSERT INTO watchlist (code, created_at)
            VALUES (?, datetime('now'))
            ON CONFLICT(code) DO UPDATE SET created_at = watchlist.created_at
            """,
            (code,),
        )
        conn.commit()
    return api_watchlist(db_path)


def api_watchlist_remove(db_path: Path, code: str) -> dict:
    if _public_demo_mode():
        return {"error": "公開展示模式不會修改主機觀察名單，請使用瀏覽器本機觀察名單。"}
    code = code.strip()
    if not code:
        return {"error": "請輸入股票代號。"}
    with _connect(db_path) as conn:
        _ensure_watchlist(conn)
        conn.execute("DELETE FROM watchlist WHERE code = ?", (code,))
        conn.commit()
    return api_watchlist(db_path)


def api_user_create(db_path: Path, name: str) -> dict:
    display_name = name.strip()[:40] or "朋友"
    user_key = secrets.token_urlsafe(18)
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        conn.execute(
            """
            INSERT INTO app_users (user_key, display_name, created_at, updated_at)
            VALUES (?, ?, datetime('now'), datetime('now'))
            """,
            (user_key, display_name),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO user_watchlist (user_key, code, created_at) VALUES (?, ?, datetime('now'))",
            [(user_key, code) for code in ["2330", "2367", "2454"]],
        )
        conn.commit()
    return api_user_profile(db_path, user_key)


def api_user_profile(db_path: Path, user_key: str) -> dict:
    user = _user_row(db_path, user_key)
    if not user:
        return {"error": "找不到使用者，請重新建立個人推播設定。"}
    return {
        "user": {
            "user_key": user["user_key"],
            "display_name": user["display_name"],
            "telegram_chat_id": user["telegram_chat_id"] or "",
            "telegram_enabled": bool(user["telegram_enabled"]),
            "created_at": user["created_at"],
        }
    }


def api_user_watchlist(db_path: Path, user_key: str) -> dict:
    if not _user_row(db_path, user_key):
        return {"error": "找不到使用者，請先建立個人推播設定。"}
    codes = _user_watchlist_codes(db_path, user_key)
    return api_watchlist(db_path, ",".join(codes))


def api_user_watchlist_add(db_path: Path, user_key: str, code: str) -> dict:
    code = code.strip()
    if not code:
        return {"error": "請輸入股票代號。"}
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        user = conn.execute("SELECT user_key FROM app_users WHERE user_key = ?", (user_key,)).fetchone()
        if not user:
            return {"error": "找不到使用者，請先建立個人推播設定。"}
        stock = conn.execute("SELECT code FROM stocks WHERE code = ?", (code,)).fetchone()
        if not stock:
            return {"error": f"找不到股票代號 {code}。"}
        conn.execute(
            "INSERT OR IGNORE INTO user_watchlist (user_key, code, created_at) VALUES (?, ?, datetime('now'))",
            (user_key, code),
        )
        conn.commit()
    return api_user_watchlist(db_path, user_key)


def api_user_watchlist_remove(db_path: Path, user_key: str, code: str) -> dict:
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        conn.execute("DELETE FROM user_watchlist WHERE user_key = ? AND code = ?", (user_key, code.strip()))
        conn.commit()
    return api_user_watchlist(db_path, user_key)


def api_user_telegram_save(db_path: Path, user_key: str, chat_id: str) -> dict:
    chat_id = chat_id.strip()
    if not chat_id:
        return {"error": "請輸入 Telegram chat_id。"}
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        row = conn.execute("SELECT user_key FROM app_users WHERE user_key = ?", (user_key,)).fetchone()
        if not row:
            return {"error": "找不到使用者，請先建立個人推播設定。"}
        conn.execute(
            """
            UPDATE app_users
            SET telegram_chat_id = ?, telegram_enabled = 1, updated_at = datetime('now')
            WHERE user_key = ?
            """,
            (chat_id, user_key),
        )
        conn.commit()
    return api_user_profile(db_path, user_key)


def api_user_telegram_test(db_path: Path, user_key: str) -> dict:
    user = _user_row(db_path, user_key)
    if not user:
        return {"error": "找不到使用者，請先建立個人推播設定。"}
    chat_id = user["telegram_chat_id"]
    if not chat_id:
        return {"error": "尚未設定 Telegram chat_id。"}
    message = _build_user_daily_message(db_path, user_key, user["display_name"], limit=5)
    _send_telegram_to_chat(chat_id, message)
    return {"ok": True, "message": "已送出 Telegram 測試推播。"}


def send_enabled_user_telegrams(db_path: Path, limit: int = 5) -> dict:
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        users = conn.execute(
            """
            SELECT user_key, display_name, telegram_chat_id
            FROM app_users
            WHERE telegram_enabled = 1 AND telegram_chat_id IS NOT NULL AND telegram_chat_id != ''
            ORDER BY created_at
            """
        ).fetchall()
    sent = 0
    failures = []
    for user in users:
        try:
            message = _build_user_daily_message(db_path, user["user_key"], user["display_name"], limit=limit)
            _send_telegram_to_chat(user["telegram_chat_id"], message)
            sent += 1
        except Exception as exc:
            failures.append({"user_key": user["user_key"], "name": user["display_name"], "error": str(exc)})
    return {"users": len(users), "sent": sent, "failures": failures}


def api_telegram_webhook(db_path: Path, payload: dict) -> dict:
    message = payload.get("message") or payload.get("edited_message") or {}
    text = str(message.get("text") or "").strip()
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    if not chat_id or not text:
        return {"ok": True, "ignored": True}
    first_name = str(chat.get("first_name") or chat.get("username") or "Telegram 使用者")
    reply = handle_telegram_text(db_path, chat_id, text, first_name)
    _send_telegram_to_chat(chat_id, reply)
    return {"ok": True}


def handle_telegram_text(db_path: Path, chat_id: str, text: str, display_name: str = "Telegram 使用者") -> str:
    normalized = text.strip()
    lowered = normalized.lower()
    if lowered in {"/start", "start", "開始"}:
        user = _ensure_telegram_user(db_path, chat_id, display_name)
        return (
            f"已建立/綁定個人設定：{user['display_name']}\n"
            "你可以直接輸入股票代號，例如 2330。\n\n"
            + _telegram_help_text()
        )
    if lowered in {"/help", "help", "幫助", "說明"}:
        return _telegram_help_text()

    user = _ensure_telegram_user(db_path, chat_id, display_name)
    code = _extract_stock_code(normalized)

    if normalized.startswith(("加入", "新增", "+")) and code:
        api_user_watchlist_add(db_path, user["user_key"], code)
        return f"已加入 {code} 到你的個人觀察名單。"
    if normalized.startswith(("移除", "刪除", "-")) and code:
        api_user_watchlist_remove(db_path, user["user_key"], code)
        return f"已從你的個人觀察名單移除 {code}。"
    if normalized in {"我的觀察名單", "觀察名單", "watchlist"}:
        return _format_telegram_watchlist(db_path, user["user_key"])
    if normalized.upper() in {"TOP5", "AI", "AI智選", "AI 智選"}:
        return _format_telegram_top_signals(db_path, 5)
    if code:
        return _format_telegram_stock_answer(db_path, code)
    return "我看不懂這個指令。\n\n" + _telegram_help_text()


def poll_telegram_updates(db_path: Path, once: bool = False, interval: int = 3) -> None:
    offset = None
    print("Telegram polling started. Press Ctrl+C to stop.")
    while True:
        params = {"timeout": 25}
        if offset is not None:
            params["offset"] = offset
        payload = _telegram_api("getUpdates", params)
        for update in payload.get("result") or []:
            offset = int(update["update_id"]) + 1
            api_telegram_webhook(db_path, update)
        if once:
            return
        import time as _time

        _time.sleep(max(1, interval))


def set_telegram_webhook(url: str) -> dict:
    return _telegram_api("setWebhook", {"url": url})


def delete_telegram_webhook() -> dict:
    return _telegram_api("deleteWebhook", {"drop_pending_updates": False})


def api_realtime(db_path: Path, raw_codes: str) -> dict:
    codes = [code.strip() for code in raw_codes.split(",") if code.strip()]
    with _connect(db_path) as conn:
        stocks = conn.execute(
            f"SELECT code, name, market FROM stocks WHERE code IN ({','.join('?' for _ in codes)})",
            codes,
        ).fetchall() if codes else []
    if not stocks:
        return {"quotes": [], "message": "沒有可查詢的股票代號"}

    quotes = []
    failures = []
    with _connect(db_path) as conn:
        for stock in stocks:
            try:
                rows = fetch_recent_finmind_prices(stock["code"], days=3)
                rows = [row for row in rows if row.get("close") is not None]
                if rows:
                    db.upsert_prices(conn, rows)
                    latest = rows[-1]
                    previous = rows[-2] if len(rows) > 1 else None
                    price = latest["close"]
                    prev_price = previous["close"] if previous else None
                    change = price - prev_price if price is not None and prev_price not in (None, 0) else None
                    change_percent = (change / prev_price * 100) if change is not None and prev_price else None
                    quotes.append(
                        {
                            "code": stock["code"],
                            "name": short_name(stock["name"]),
                            "market": stock["market"],
                            "price": price,
                            "change": change,
                            "change_percent": change_percent,
                            "volume": latest["volume"],
                            "date": latest["date"],
                            "time": None,
                            "source": "FinMind TaiwanStockPrice",
                        }
                    )
                    continue
            except Exception as exc:
                failures.append(f"{stock['code']}: {exc}")

            local = _local_quote(conn, stock)
            if local:
                quotes.append(local)

    message = "已使用 FinMind TaiwanStockPrice API 更新看盤資料。"
    if failures:
        message += " 部分股票已切回本機資料：" + "；".join(failures[:3])
    return {"quotes": quotes, "message": message, "source": "FinMind"}


def api_realtime_trend(db_path: Path, code: str, interval: str = "1d") -> dict:
    code = code.strip()
    interval = interval.strip().lower()
    if not code:
        return {"error": "請輸入股票代號。"}
    stock = api_stock(db_path, code)
    if "error" in stock:
        return stock

    today = date.today()
    if interval in {"5", "10", "15", "30", "5m", "10m", "15m", "30m"}:
        minutes = int(interval.replace("m", ""))
        try:
            rows = fetch_finmind_kbar(code, today)
            rows = [row for row in rows if row.get("close") is not None]
            rows = _aggregate_intraday_rows(rows, minutes)
            if rows:
                return {
                    "code": code,
                    "name": stock.get("short_name") or stock.get("name"),
                    "mode": "intraday",
                    "interval": f"{minutes}m",
                    "source": "FinMind TaiwanStockKBar",
                    "message": f"已使用 FinMind 分 K 資料並聚合為 {minutes} 分鐘。",
                    "rows": rows,
                }
        except Exception as exc:
            kbar_error = str(exc)
        else:
            kbar_error = "FinMind KBar 無資料"
    else:
        kbar_error = "使用日線走勢"

    try:
        if interval in {"1d", "day", "daily"}:
            raise RuntimeError("日線模式")
        rows = fetch_finmind_kbar(code, today)
        rows = [row for row in rows if row.get("close") is not None]
        if rows:
            return {
                "code": code,
                "name": stock.get("short_name") or stock.get("name"),
                "mode": "intraday",
                "source": "FinMind TaiwanStockKBar",
                "message": "已使用 FinMind 分 K 資料。若無資料，請確認帳號是否有 Sponsor 權限。",
                "rows": rows,
            }
    except Exception as exc:
        if kbar_error == "使用日線走勢":
            kbar_error = str(exc)
    else:
        kbar_error = "FinMind KBar 無資料"

    try:
        daily = fetch_recent_finmind_prices(code, days=3)
        daily = [row for row in daily if row.get("close") is not None]
        with _connect(db_path) as conn:
            if daily:
                db.upsert_prices(conn, daily)
            stored = conn.execute(
                """
                SELECT date, open, high, low, close, volume
                FROM prices
                WHERE stock_code = ?
                ORDER BY date DESC
                LIMIT 45
                """,
                (code,),
            ).fetchall()
        rows = [
            {
                "date": row["date"],
                "time": None,
                "label": row["date"][5:],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
            }
            for row in reversed(stored)
        ]
        return {
            "code": code,
            "name": stock.get("short_name") or stock.get("name"),
            "mode": "daily",
            "source": "FinMind TaiwanStockPrice + 本機資料",
            "message": f"FinMind 分 K 不可用，已改用近期日線走勢。原因：{kbar_error}",
            "rows": rows,
        }
    except Exception as exc:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT date, open, high, low, close, volume
                FROM prices
                WHERE stock_code = ?
                ORDER BY date DESC
                LIMIT 45
                """,
                (code,),
            ).fetchall()
        return {
            "code": code,
            "name": stock.get("short_name") or stock.get("name"),
            "mode": "daily",
            "source": "本機資料庫",
            "message": f"FinMind 走勢不可用，已使用本機資料。原因：{exc}",
            "rows": [dict(row) | {"label": dict(row)["date"][5:], "time": None} for row in reversed(rows)],
        }


def api_institutional(db_path: Path, code: str) -> dict:
    code = code.strip()
    stock = api_stock(db_path, code)
    if "error" in stock:
        return stock
    end = date.today()
    start = end - timedelta(days=420)
    with _connect(db_path) as conn:
        price_dates = conn.execute(
            """
            SELECT date
            FROM prices
            WHERE stock_code = ?
            ORDER BY date DESC
            LIMIT 260
            """,
            (code,),
        ).fetchall()
    if price_dates:
        sorted_dates = sorted(row["date"] for row in price_dates)
        start = datetime.strptime(sorted_dates[0], "%Y-%m-%d").date()
        end = datetime.strptime(sorted_dates[-1], "%Y-%m-%d").date()
    try:
        raw = fetch_finmind_institutional(code, start, end)
        grouped: dict[str, dict] = {}
        for row in raw:
            day = row.get("date")
            if not day:
                continue
            item = grouped.setdefault(day, {"date": day, "foreign": 0, "investment": 0, "dealer": 0})
            name = str(row.get("name") or "")
            net = float(row.get("net") or 0)
            if "外資" in name or "Foreign" in name:
                item["foreign"] += net
            elif "投信" in name or "Investment" in name:
                item["investment"] += net
            elif "自營" in name or "Dealer" in name:
                item["dealer"] += net
        rows = []
        for day in sorted(grouped):
            item = grouped[day]
            institutional_net = item["foreign"] + item["investment"] + item["dealer"]
            item["retail_proxy"] = -institutional_net
            rows.append(item)
        return {
            "code": code,
            "name": stock.get("short_name") or stock.get("name"),
            "source": "FinMind TaiwanStockInstitutionalInvestorsBuySell",
            "message": "外資、投信、自營商為公開法人買賣超；散戶為法人反向代理值。",
            "rows": rows,
        }
    except Exception as exc:
        return {"code": code, "name": stock.get("short_name") or stock.get("name"), "rows": [], "message": f"法人資料暫不可用：{exc}"}


def _aggregate_intraday_rows(rows: list[dict], minutes: int) -> list[dict]:
    if minutes <= 1:
        return rows
    grouped = []
    current = None
    for row in rows:
        label = str(row.get("time") or row.get("label") or "")
        try:
            hour, minute = [int(part) for part in label[:5].split(":")]
            bucket_minute = (minute // minutes) * minutes
            bucket = f"{hour:02d}:{bucket_minute:02d}"
        except Exception:
            bucket = label
        if current is None or current["label"] != bucket:
            current = {
                "date": row.get("date"),
                "time": bucket,
                "label": bucket,
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume") or 0,
            }
            grouped.append(current)
            continue
        current["high"] = max(value for value in [current.get("high"), row.get("high")] if value is not None)
        current["low"] = min(value for value in [current.get("low"), row.get("low")] if value is not None)
        current["close"] = row.get("close")
        current["volume"] = (current.get("volume") or 0) + (row.get("volume") or 0)
    return grouped


def _local_realtime_quotes(db_path: Path, stocks: list[sqlite3.Row], message: str) -> dict:
    quotes = []
    with _connect(db_path) as conn:
        for stock in stocks:
            quote = _local_quote(conn, stock)
            if quote:
                quotes.append(quote)
    return {"quotes": quotes, "message": message, "fallback": True}


def _local_quote(conn: sqlite3.Connection, stock: sqlite3.Row) -> dict | None:
    rows = conn.execute(
        """
        SELECT date, close, volume
        FROM prices
        WHERE stock_code = ?
        ORDER BY date DESC
        LIMIT 2
        """,
        (stock["code"],),
    ).fetchall()
    latest = rows[0] if rows else None
    if not latest:
        return None
    previous = rows[1] if len(rows) > 1 else None
    price = latest["close"]
    prev_price = previous["close"] if previous else None
    change = price - prev_price if price is not None and prev_price not in (None, 0) else None
    change_percent = (change / prev_price * 100) if change is not None and prev_price else None
    return {
        "code": stock["code"],
        "name": short_name(stock["name"]),
        "market": stock["market"],
        "price": price,
        "change": change,
        "change_percent": change_percent,
        "volume": latest["volume"],
        "date": latest["date"],
        "time": None,
        "source": "本機資料庫",
    }


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_created_at ON watchlist(created_at)")


def _ensure_user_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_users (
            user_key TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            telegram_chat_id TEXT,
            telegram_enabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_watchlist (
            user_key TEXT NOT NULL,
            code TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_key, code),
            FOREIGN KEY (user_key) REFERENCES app_users(user_key),
            FOREIGN KEY (code) REFERENCES stocks(code)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_watchlist_user ON user_watchlist(user_key, created_at)")


def _user_row(db_path: Path, user_key: str) -> sqlite3.Row | None:
    key = user_key.strip()
    if not key:
        return None
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        return conn.execute("SELECT * FROM app_users WHERE user_key = ?", (key,)).fetchone()


def _user_watchlist_codes(db_path: Path, user_key: str) -> list[str]:
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        rows = conn.execute(
            "SELECT code FROM user_watchlist WHERE user_key = ? ORDER BY created_at, code",
            (user_key,),
        ).fetchall()
    return [row["code"] for row in rows]


def _ensure_telegram_user(db_path: Path, chat_id: str, display_name: str) -> sqlite3.Row:
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        row = conn.execute(
            "SELECT * FROM app_users WHERE telegram_chat_id = ?",
            (chat_id,),
        ).fetchone()
        if row:
            return row
        user_key = secrets.token_urlsafe(18)
        conn.execute(
            """
            INSERT INTO app_users (user_key, display_name, telegram_chat_id, telegram_enabled, created_at, updated_at)
            VALUES (?, ?, ?, 1, datetime('now'), datetime('now'))
            """,
            (user_key, display_name[:40] or "Telegram 使用者", chat_id),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO user_watchlist (user_key, code, created_at) VALUES (?, ?, datetime('now'))",
            [(user_key, code) for code in ["2330", "2367", "2454"]],
        )
        conn.commit()
        return conn.execute("SELECT * FROM app_users WHERE user_key = ?", (user_key,)).fetchone()


def _extract_stock_code(text: str) -> str | None:
    for token in text.replace("：", " ").replace(":", " ").replace(",", " ").split():
        clean = token.strip().lstrip("+-#")
        if clean.isdigit() and 4 <= len(clean) <= 6:
            return clean[:6]
    if text.strip().isdigit() and 4 <= len(text.strip()) <= 6:
        return text.strip()
    return None


def _telegram_help_text() -> str:
    return "\n".join(
        [
            "可用指令：",
            "2330｜查個股分析",
            "分析 2454｜查個股買點、賣點、停損",
            "加入 2367｜加入個人觀察名單",
            "移除 2367｜移除個人觀察名單",
            "我的觀察名單｜查看自己的股票提醒",
            "AI智選｜查看 AI Top 5",
        ]
    )


def _format_telegram_stock_answer(db_path: Path, code: str) -> str:
    stock = api_stock(db_path, code)
    if stock.get("error"):
        return stock["error"]
    signal = api_stock_signal(db_path, code)
    watch = _watch_snapshot(db_path, code) or {}
    latest = stock.get("latest") or {}
    lines = [
        f"{stock['code']} {stock.get('short_name') or stock['name']}",
        f"產業：{stock.get('industry', '無資料')}",
        f"日期：{latest.get('date', '無資料')}",
        f"收盤：{fmt_value(latest.get('close'))}",
    ]
    if not signal.get("error"):
        lines.extend(
            [
                f"AI 分數：{fmt_value(signal.get('risk_adjusted_score'), 1)}｜{signal.get('signal', '資料不足')}",
                f"20D：{fmt_value(signal.get('return_20d'))}%｜60D：{fmt_value(signal.get('return_60d'))}%",
                f"回撤：{fmt_value(signal.get('drawdown_pct'))}%",
            ]
        )
    lines.extend(
        [
            f"買點：{watch.get('buy_zone', '無資料')}",
            f"賣點：{watch.get('sell_zone', '無資料')}",
            f"停損：{watch.get('stop', '無資料')}",
            f"建議：{_simple_watch_advice(watch) if watch else '資料不足'}",
            "",
            "提醒：這是研究輔助，不是保證獲利或直接下單指令。",
        ]
    )
    return "\n".join(lines)


def _format_telegram_watchlist(db_path: Path, user_key: str) -> str:
    rows = api_user_watchlist(db_path, user_key).get("watchlist", [])
    if not rows:
        return "你的觀察名單目前是空的。可以輸入：加入 2330"
    lines = ["你的觀察名單"]
    for item in rows[:8]:
        lines.extend(
            [
                "",
                f"{item['code']} {item['name']}｜AI {fmt_value(item.get('score'), 1)}｜{item.get('signal', '資料不足')}",
                f"收盤 {fmt_value(item.get('close'))}｜20D {fmt_value(item.get('return_20d'))}%｜RSI {fmt_value(item.get('rsi_14'))}",
                f"買點 {item.get('buy_zone', '無資料')}｜停損 {item.get('stop', '無資料')}",
            ]
        )
    return "\n".join(lines)


def _format_telegram_top_signals(db_path: Path, limit: int = 5) -> str:
    data = api_signals(db_path, limit)
    rows = data.get("top_signals") or []
    if not rows:
        return "目前沒有 AI 智選資料。"
    lines = [f"AI 智選 Top {limit}"]
    for item in rows[:limit]:
        lines.extend(
            [
                "",
                f"{item['code']} {item.get('short_name') or short_name(item['name'])}｜{item['score']}｜{item['signal']}",
                f"收盤 {fmt_value(item.get('close'))}｜20D {fmt_value(item.get('return_20d'))}%｜量比 {fmt_value(item.get('volume_ratio'))}",
                f"買點：{item.get('entry_zone', '無資料')}",
                f"停損：{item.get('stop', '無資料')}",
            ]
        )
    return "\n".join(lines)


def _build_user_daily_message(db_path: Path, user_key: str, display_name: str, limit: int = 5) -> str:
    status = api_status(db_path)
    codes = _user_watchlist_codes(db_path, user_key)
    rows = api_watchlist(db_path, ",".join(codes)).get("watchlist", [])[:limit]
    lines = [
        f"台股智研｜{display_name} 的個人觀察名單",
        f"資料日期：{status.get('last_date', '無資料')}",
        "",
        "觀察名單技術建議",
    ]
    if not rows:
        lines.append("目前尚未加入觀察股票，請先回網站加入觀察名單。")
    for item in rows:
        lines.extend([
            "",
            f"{item['code']} {item['name']}｜AI {item.get('score', '無資料')}｜{item.get('signal', '資料不足')}",
            f"收盤：{fmt_value(item.get('close'))}｜20D {fmt_value(item.get('return_20d'))}%｜RSI {fmt_value(item.get('rsi_14'))}",
            f"買點：{item.get('buy_zone', '無資料')}",
            f"賣點：{item.get('sell_zone', '無資料')}",
            f"停損：{item.get('stop', '無資料')}",
            f"建議：{_simple_watch_advice(item)}",
        ])
    lines.extend([
        "",
        "提醒：這是系統依技術面與風控條件產生的研究提醒，不是保證獲利或直接下單指令。",
    ])
    return "\n".join(lines)


def _send_telegram_to_chat(chat_id: str, message: str) -> dict:
    return _telegram_api(
        "sendMessage",
        {"chat_id": chat_id, "text": message[:3900], "disable_web_page_preview": True},
    )


def _telegram_api(method: str, payload: dict) -> dict:
    bot_token = _telegram_bot_token()
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram 發送失敗：HTTP {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Telegram 發送失敗：{exc}") from exc
    return json.loads(raw) if raw else {"ok": True}


def _telegram_bot_token() -> str:
    config_path = Path(__file__).resolve().parents[1] / "config" / "notify.json"
    bot_token = os.environ.get("STOCK_V1_TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token and config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        bot_token = str((config.get("telegram") or {}).get("bot_token") or "").strip()
    if not bot_token or "PASTE_" in str(bot_token):
        raise RuntimeError("主機 Telegram Bot token 尚未設定完成。請設定 STOCK_V1_TELEGRAM_BOT_TOKEN。")
    return bot_token


def fmt_value(value, digits: int = 2) -> str:
    if value is None:
        return "無資料"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def _simple_watch_advice(item: dict) -> str:
    score = item.get("score")
    ret20 = item.get("return_20d")
    rsi14 = item.get("rsi_14")
    if rsi14 is not None and rsi14 >= 75:
        return "短線偏熱，已有部位可分批停利，未進場不宜追高。"
    if ret20 is not None and ret20 <= -10:
        return "短線轉弱，先等止跌與重新站回月線再評估。"
    if score is not None and score >= 70:
        return "訊號偏強，可用買點區分批觀察，務必搭配停損。"
    return "維持觀察，等待量價與均線結構更明確。"


def _watchlist_codes(db_path: Path) -> list[str]:
    default_codes = ["2330", "2317", "2454", "2308", "2412", "2882"]
    with _connect(db_path) as conn:
        _ensure_watchlist(conn)
        existing = conn.execute("SELECT COUNT(*) AS n FROM watchlist").fetchone()["n"]
        if existing == 0:
            conn.executemany(
                "INSERT OR IGNORE INTO watchlist (code, created_at) VALUES (?, datetime('now'))",
                [(code,) for code in default_codes],
            )
            conn.commit()
        rows = conn.execute("SELECT code FROM watchlist ORDER BY created_at, code").fetchall()
    return [row["code"] for row in rows]


def _watch_snapshot(db_path: Path, code: str) -> dict | None:
    stock = api_stock(db_path, code)
    ind = api_indicators(db_path, code)
    signal = api_stock_signal(db_path, code)
    if "error" in stock or "error" in ind:
        return None
    with _connect(db_path) as conn:
        price_rows = conn.execute(
            """
            SELECT date, open, high, low, close, volume
            FROM prices
            WHERE stock_code = ?
            ORDER BY date DESC
            LIMIT 80
            """,
            (code,),
        ).fetchall()
    trade = _watch_trade_points([dict(row) for row in reversed(price_rows)], ind)
    return {
        "code": code,
        "name": stock.get("short_name") or stock.get("name"),
        "market": stock.get("market"),
        "close": stock["latest"]["close"] if stock.get("latest") else None,
        "return_5d": ind.get("return_5d"),
        "return_20d": ind.get("return_20d"),
        "return_60d": ind.get("return_60d"),
        "rsi_14": ind.get("rsi_14"),
        "volume_ratio": ind.get("volume_ratio"),
        "signal": signal.get("signal") if "error" not in signal else "資料不足",
        "score": signal.get("risk_adjusted_score") if "error" not in signal else None,
        "sentiment": signal.get("sentiment") if "error" not in signal else "資料不足",
        "buy_zone": trade["buy_zone"],
        "sell_zone": trade["sell_zone"],
        "stop": trade["stop"],
    }


def _watch_trade_points(rows: list[dict], ind: dict) -> dict:
    if not rows:
        return {"buy_zone": "資料不足", "sell_zone": "資料不足", "stop": "資料不足"}
    if rows[-1].get("close") is None:
        return {"buy_zone": "資料不足", "sell_zone": "資料不足", "stop": "資料不足"}
    close = float(rows[-1]["close"])
    recent = rows[-20:] if len(rows) >= 20 else rows
    lows = [float(row["low"]) for row in recent if row.get("low") is not None]
    highs = [float(row["high"]) for row in recent if row.get("high") is not None]
    if not lows or not highs:
        return {"buy_zone": "資料不足", "sell_zone": "資料不足", "stop": "資料不足"}
    recent_low = min(lows)
    recent_high = max(highs)
    supports = [
        value
        for value in (recent_low, ind.get("sma_20"), ind.get("sma_60"))
        if value is not None and float(value) <= close
    ]
    resistances = [
        value
        for value in (recent_high, ind.get("sma_20"), ind.get("sma_60"))
        if value is not None and float(value) >= close
    ]
    support = max(map(float, supports)) if supports else recent_low
    resistance = min(map(float, resistances)) if resistances else recent_high
    buy_low = support
    buy_high = support * 1.03
    stop = support * 0.97
    sell_low = resistance
    sell_high = resistance * 1.05
    return {
        "buy_zone": f"{buy_low:.2f}-{buy_high:.2f}",
        "sell_zone": f"{sell_low:.2f}-{sell_high:.2f}",
        "stop": f"{stop:.2f}",
    }


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


def _param(params: dict, name: str, default: str) -> str:
    values = params.get(name)
    return values[0].strip() if values and values[0].strip() else default


def _sentiment_label(rsi_value) -> str:
    if rsi_value is None:
        return "資料不足"
    if rsi_value >= 75:
        return "過熱"
    if rsi_value >= 60:
        return "偏多"
    if rsi_value >= 40:
        return "中性"
    return "偏弱"


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>台股資料儀表板</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef3f5;
      --panel: #ffffff;
      --panel-2: #f7fbfa;
      --ink: #111827;
      --text: #1f2937;
      --muted: #667085;
      --line: #d8e2e0;
      --line-strong: #adc4be;
      --accent: #00a884;
      --accent-dark: #007c67;
      --accent-soft: #e4f7f1;
      --gold: #c1841d;
      --blue: #2563eb;
      --navy: #111827;
      --cyan: #0891b2;
      --down: #c24135;
      --topbar: #101418;
      --topbar-2: #1d2830;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        linear-gradient(rgba(125,211,252,.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(125,211,252,.035) 1px, transparent 1px),
        radial-gradient(circle at 12% 0%, rgba(8,145,178,.26), transparent 32%),
        radial-gradient(circle at 92% 10%, rgba(0,168,132,.20), transparent 30%),
        linear-gradient(180deg, #050b14 0, #101827 300px, #eef3f5 780px, #eef3f5 100%);
      background-size: 42px 42px, 42px 42px, auto, auto, auto;
      color: var(--text);
      font-family: Arial, "Microsoft JhengHei", sans-serif;
      font-size: 15px;
      line-height: 1.45;
    }
    header {
      background:
        linear-gradient(135deg, var(--topbar), var(--topbar-2) 58%, #17372f);
      color: white;
      padding: 18px 28px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      border-bottom: 1px solid #334155;
      box-shadow: 0 8px 28px rgba(15, 23, 42, .22);
    }
    .brand {
      display: grid;
      gap: 4px;
    }
    h1 {
      font-size: 24px;
      margin: 0;
      font-weight: 700;
      letter-spacing: 0;
    }
    .subtitle {
      color: #a7f3d0;
      font-size: 13px;
      font-weight: 700;
    }
    .version {
      color: #cbd5e1;
      font-size: 14px;
      text-align: right;
    }
    main {
      padding: 24px;
      display: grid;
      gap: 16px;
    }
    .app-shell {
      display: grid;
      grid-template-columns: 248px minmax(0, 1fr);
      gap: 18px;
      max-width: 1440px;
      margin: 0 auto;
      padding: 20px;
    }
    .sidebar {
      background: linear-gradient(180deg, rgba(15,23,42,.94), rgba(15,35,44,.88));
      border: 1px solid rgba(125, 211, 252, .18);
      border-radius: 12px;
      box-shadow: 0 18px 48px rgba(2, 6, 23, .16);
      padding: 16px;
      height: fit-content;
      position: sticky;
      top: 18px;
      backdrop-filter: blur(16px);
    }
    .sidebar-title {
      font-size: 13px;
      color: #7dd3fc;
      font-weight: 800;
      margin-bottom: 10px;
    }
    .nav-list {
      display: grid;
      gap: 7px;
    }
    .nav-item {
      display: grid;
      grid-template-columns: 28px 1fr;
      align-items: center;
      gap: 8px;
      color: #cbd5e1;
      text-decoration: none;
      padding: 10px;
      border-radius: 8px;
      font-weight: 800;
      border: 1px solid transparent;
    }
    .nav-item:hover {
      background: rgba(14,165,233,.12);
      border-color: rgba(125,211,252,.32);
    }
    .nav-item.active {
      background: linear-gradient(90deg, rgba(8,145,178,.42), rgba(0,168,132,.28));
      color: white;
      border-color: rgba(125,211,252,.35);
    }
    .nav-icon {
      width: 28px;
      height: 28px;
      display: inline-grid;
      place-items: center;
      border-radius: 8px;
      background: rgba(125,211,252,.10);
      color: #7dd3fc;
      font-size: 13px;
    }
    .nav-item.active .nav-icon {
      background: rgba(255,255,255,.14);
      color: #a7f3d0;
    }
    .workspace {
      min-width: 0;
      padding: 0;
    }
    .page { display: none; }
    .page.active { display: grid; gap: 16px; }
    body.stock-focus #statusLine { display: none; }
    body.stock-focus #stockPage.page.active { gap: 8px; }
    body.stock-focus #stockPage .grid { gap: 8px; }
    .overview-only.hidden { display: none; }
    .toolbar, .metrics, .grid {
      display: grid;
      gap: 12px;
    }
    .toolbar {
      grid-template-columns: minmax(150px, 220px) repeat(5, auto);
      align-items: center;
      background: rgba(255, 255, 255, .92);
      border: 1px solid rgba(173, 196, 190, .8);
      border-radius: 10px;
      padding: 12px;
      box-shadow: 0 14px 34px rgba(16, 24, 40, .08);
      backdrop-filter: blur(10px);
    }
    input, button {
      height: 42px;
      border: 1px solid var(--line);
      border-radius: 6px;
      font: inherit;
    }
    input {
      padding: 0 12px;
      background: #fbfdff;
      border-color: var(--line-strong);
      font-weight: 700;
      letter-spacing: 0;
    }
    input:focus {
      outline: 2px solid rgba(0, 133, 111, .2);
      border-color: var(--accent);
    }
    button {
      padding: 0 14px;
      background: #ffffff;
      cursor: pointer;
      color: #1f2937;
      font-weight: 700;
      transition: background .15s ease, border-color .15s ease, transform .15s ease;
    }
    button:hover {
      background: #eef6f4;
      border-color: #7bc7b8;
    }
    button:active {
      transform: translateY(1px);
    }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }
    button.primary:hover {
      background: var(--accent-dark);
      border-color: var(--accent-dark);
    }
    button.compact {
      height: 32px;
      padding: 0 10px;
      font-size: 13px;
    }
    .metric, section {
      background: rgba(255,255,255,.94);
      border: 1px solid rgba(210,226,224,.92);
      border-radius: 10px;
      box-shadow: 0 16px 38px rgba(15, 23, 42, .075);
    }
    .metric {
      padding: 16px;
      border-top: 4px solid var(--accent);
      position: relative;
      overflow: hidden;
    }
    .metric::after {
      content: "";
      position: absolute;
      inset: auto 14px 12px auto;
      width: 34px;
      height: 34px;
      border: 1px solid #b8ece0;
      border-radius: 50%;
      opacity: .45;
    }
    .metric span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 8px;
      font-weight: 700;
    }
    .metric strong { font-size: 23px; }
    .grid { grid-template-columns: 1fr 1fr; }
    section { overflow: hidden; }
    section h2 {
      margin: 0;
      padding: 14px 16px;
      font-size: 16px;
      border-bottom: 1px solid var(--line);
      background:
        linear-gradient(90deg, #f5fbf9, #ffffff);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    section h2::after {
      content: "";
      width: 34px;
      height: 3px;
      border-radius: 999px;
      background: var(--accent);
    }
    .collapse-btn {
      height: 30px;
      padding: 0 10px;
      font-size: 12px;
      border-radius: 999px;
      background: #ffffff;
      color: var(--accent-dark);
      border-color: #b9eadf;
    }
    .collapsible.collapsed .content {
      display: none;
    }
    .content { padding: 16px; }
    .strategy-advice {
      display: grid;
      grid-template-columns: 1.1fr 1fr;
      gap: 14px;
      align-items: stretch;
    }
    .advice-main {
      border: 1px solid #bfe7df;
      background: linear-gradient(135deg, #f4fbf8, #ffffff);
      border-radius: 10px;
      padding: 16px;
    }
    .advice-main strong {
      display: block;
      color: var(--ink);
      font-size: 22px;
      margin-bottom: 8px;
    }
    .advice-main p {
      margin: 0 0 10px;
      color: var(--muted);
    }
    .advice-list {
      margin: 0;
      padding-left: 18px;
      color: var(--text);
    }
    .advice-list li { margin: 6px 0; }
    .strategy-kpis {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .strategy-kpi {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #ffffff;
    }
    .strategy-kpi span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 5px;
    }
    .strategy-kpi strong {
      color: var(--ink);
      font-size: 20px;
    }
    .strategy-stock-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(220px, 1fr));
      gap: 12px;
    }
    .strategy-stock-card {
      background: linear-gradient(180deg, #101827, #0b1320);
      border: 1px solid #223247;
      border-radius: 10px;
      padding: 14px;
      color: #cbd5e1;
      box-shadow: 0 18px 38px rgba(15,23,42,.16);
    }
    .strategy-stock-card .code {
      color: #7dd3fc;
      font-size: 12px;
      font-weight: 900;
      margin-bottom: 6px;
    }
    .strategy-stock-card strong {
      display: block;
      color: #ffffff;
      font-size: 18px;
      margin-bottom: 8px;
    }
    .strategy-stock-card span {
      display: block;
      color: #94a3b8;
      font-size: 12px;
      line-height: 1.6;
    }
    .strategy-stock-card button {
      margin-top: 10px;
      width: 100%;
      background: #0891b2;
      color: white;
      border-color: #22d3ee;
    }
    .strategy-guide {
      display: grid;
      grid-template-columns: repeat(4, minmax(180px, 1fr));
      gap: 10px;
    }
    .strategy-guide div {
      background: linear-gradient(180deg, #0f1a2a, #08111f);
      border: 1px solid rgba(56,189,248,.2);
      border-radius: 8px;
      padding: 12px;
    }
    .strategy-guide strong {
      display: block;
      color: #ffffff;
      font-size: 15px;
      margin-bottom: 6px;
    }
    .strategy-guide span {
      color: #cbd5e1;
      font-size: 13px;
      line-height: 1.45;
    }
    dl {
      display: grid;
      grid-template-columns: 150px 1fr;
      gap: 8px 12px;
      margin: 0;
    }
    dt { color: var(--muted); font-weight: 700; }
    dd { margin: 0; font-weight: 700; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    thead th {
      background: #eef7f4;
      color: #35524b;
      font-size: 12px;
      font-weight: 800;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 9px 8px;
      text-align: right;
      white-space: nowrap;
    }
    tbody tr:hover {
      background: #f4fbf9;
    }
    th:first-child, td:first-child,
    th:nth-child(2), td:nth-child(2) { text-align: left; }
    .table-wrap { overflow-x: auto; }
    .positive { color: #00d9a6; }
    .negative { color: #ff3b30; }
    .wide { grid-column: 1 / -1; }
    .note { color: var(--muted); }
    .status {
      min-height: 30px;
      color: var(--muted);
      font-size: 14px;
      background: #ffffff;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 12px;
      box-shadow: 0 8px 20px rgba(15, 23, 42, .04);
    }
    .dashboard-note {
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: center;
      gap: 12px;
      background: #ffffff;
      border: 1px solid var(--line);
      border-left: 4px solid var(--gold);
      border-radius: 10px;
      padding: 12px 14px;
      box-shadow: 0 10px 24px rgba(15, 23, 42, .05);
    }
    .dashboard-note strong {
      color: var(--ink);
    }
    .dashboard-note span {
      color: var(--muted);
      font-size: 13px;
    }
    .chart {
      width: 100%;
      height: 260px;
      display: block;
      background: linear-gradient(180deg, #fbfffe, #f7fbfa);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    #priceChart { height: clamp(340px, 48vh, 460px); }
    #rsiChart, #macdChart, #kdChart, #volumeChart, #institutionalChart { height: clamp(200px, 28vh, 270px); }
    #realtimeTrendChart { height: 320px; }
    .chart-line {
      fill: none;
      stroke: var(--accent);
      stroke-width: 2.5;
    }
    .chart-area {
      fill: rgba(0, 168, 132, .10);
    }
    .chart-axis {
      stroke: #cbd5d1;
      stroke-width: 1;
    }
    .chart-label {
      fill: #64748b;
      font-size: 12px;
    }
    .pill {
      border: 1px solid #c6ece4;
      background: var(--accent-soft);
      color: var(--accent-dark);
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
    }
    .hero-panel {
      background:
        linear-gradient(135deg, #111827 0%, #17372f 54%, #0f766e 100%);
      border: 1px solid rgba(255,255,255,.18);
      border-radius: 16px;
      padding: 24px;
      box-shadow: 0 22px 48px rgba(15, 23, 42, .16);
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, .75fr);
      gap: 18px;
      align-items: center;
      color: white;
      position: relative;
      overflow: hidden;
    }
    .hero-panel::after {
      content: "";
      position: absolute;
      inset: auto 24px 24px auto;
      width: 38%;
      height: 1px;
      background: linear-gradient(90deg, transparent, rgba(167,243,208,.8));
      box-shadow:
        0 -44px 0 rgba(255,255,255,.08),
        0 -88px 0 rgba(255,255,255,.05),
        0 -132px 0 rgba(255,255,255,.035);
      pointer-events: none;
    }
    .hero-panel h2 {
      margin: 0 0 8px;
      font-size: 30px;
      color: white;
    }
    .hero-panel p {
      margin: 0;
      color: #cbd5e1;
      max-width: 680px;
    }
    .eyebrow {
      color: #a7f3d0;
      font-size: 12px;
      font-weight: 900;
      margin-bottom: 10px;
    }
    .hero-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 16px;
    }
    .hero-actions button:not(.primary) {
      background: rgba(255,255,255,.10);
      border-color: rgba(255,255,255,.22);
      color: white;
    }
    .hero-actions button:not(.primary):hover {
      background: rgba(255,255,255,.16);
      border-color: rgba(255,255,255,.35);
    }
    .hero-stat-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      position: relative;
      z-index: 1;
    }
    .hero-stat {
      background: rgba(255,255,255,.10);
      border: 1px solid rgba(255,255,255,.18);
      border-radius: 10px;
      padding: 13px;
      backdrop-filter: blur(8px);
    }
    .hero-stat span {
      display: block;
      color: #cbd5e1;
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 7px;
    }
    .hero-stat strong {
      color: white;
      font-size: 20px;
    }
    .market-strip {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }
    .market-tile {
      background: #ffffff;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 15px;
      box-shadow: 0 12px 28px rgba(15,23,42,.055);
    }
    .market-tile span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 8px;
    }
    .market-tile strong {
      color: var(--ink);
      font-size: 18px;
    }
    .market-tile.accent { border-top: 4px solid var(--accent); }
    .market-tile.blue { border-top: 4px solid var(--blue); }
    .market-tile.gold { border-top: 4px solid var(--gold); }
    .market-tile.cyan { border-top: 4px solid var(--cyan); }
    .module-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .module-card {
      background: #ffffff;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 16px;
      box-shadow: 0 10px 24px rgba(15, 23, 42, .045);
    }
    .module-card strong {
      display: block;
      font-size: 16px;
      margin-bottom: 6px;
      color: var(--ink);
    }
    .module-card span {
      color: var(--muted);
      font-size: 13px;
    }
    .explore-hero {
      background: linear-gradient(135deg, #0f172a, #14312d);
      color: #e5edf6;
      border: 1px solid #1f3b4c;
      border-radius: 12px;
      padding: 18px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(280px, auto);
      gap: 16px;
      align-items: center;
      box-shadow: 0 18px 42px rgba(15, 23, 42, .18);
    }
    .explore-hero h2 {
      background: transparent;
      border: 0;
      color: #ffffff;
      padding: 0;
      font-size: 24px;
      margin-bottom: 6px;
    }
    .explore-hero h2::after { display: none; }
    .explore-hero p {
      margin: 0;
      color: #a7f3d0;
      font-weight: 700;
    }
    .explore-actions {
      display: grid;
      grid-template-columns: minmax(160px, 1fr) auto auto;
      gap: 8px;
      align-items: center;
    }
    .explore-actions input {
      background: rgba(255,255,255,.94);
    }
    .explore-actions button:not(.primary) {
      background: rgba(255,255,255,.10);
      border-color: rgba(255,255,255,.20);
      color: #e5edf6;
    }
    .diagnosis {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .diagnosis-card {
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 14px;
    }
    .diagnosis-card span {
      color: var(--muted);
      display: block;
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 8px;
    }
    .diagnosis-card strong {
      font-size: 24px;
      color: var(--ink);
    }
    .watch-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .watch-card {
      background: #ffffff;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 16px;
      box-shadow: 0 10px 24px rgba(15,23,42,.045);
    }
    .watch-card span {
      color: var(--muted);
      font-weight: 800;
      font-size: 12px;
      display: block;
      margin-bottom: 8px;
    }
    .watch-card strong {
      font-size: 26px;
      color: var(--ink);
    }
    .watch-tools {
      display: grid;
      grid-template-columns: minmax(160px, 220px) auto auto auto 1fr;
      gap: 10px;
      align-items: center;
      padding: 14px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      box-shadow: 0 10px 24px rgba(15,23,42,.045);
    }
    .watch-tools .hint {
      color: var(--muted);
      font-size: 13px;
    }
    .stock-hero {
      background:
        linear-gradient(135deg, #10251f, #1e3a34);
      color: white;
      border-radius: 12px;
      padding: 10px 14px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: center;
    }
    .stock-hero h2 {
      background: transparent;
      border: 0;
      color: white;
      padding: 0;
      font-size: 20px;
    }
    .stock-hero h2::after { display: none; }
    .stock-hero .sub {
      color: #a7f3d0;
      font-weight: 700;
      margin-top: 2px;
      font-size: 13px;
    }
    .score-badge {
      min-width: 92px;
      text-align: center;
      background: rgba(255,255,255,.12);
      border: 1px solid rgba(255,255,255,.22);
      border-radius: 12px;
      padding: 8px 10px;
    }
    .score-badge span {
      color: #cbd5e1;
      display: block;
      font-size: 12px;
      font-weight: 800;
    }
    .score-badge strong {
      font-size: 24px;
      color: white;
    }
    .trading-desk {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 14px;
      align-items: start;
    }
    .desk-main, .desk-side {
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    .desk-side {
      grid-template-columns: .85fr 1.15fr 1.2fr 1fr 1fr;
      align-items: stretch;
    }
    .desk-side .desk-panel {
      min-width: 0;
    }
    .desk-side .diagnosis {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    .desk-side .content { padding: 10px; }
    .desk-side dl {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 6px 10px;
      font-size: 12px;
    }
    .desk-side .trade-plan { padding: 10px; }
    .desk-side .trade-callout { padding: 9px; }
    .desk-side .trade-callout strong { font-size: 14px; }
    .desk-side .trade-callout p,
    .desk-side .trade-list {
      font-size: 12px;
      line-height: 1.42;
    }
    .desk-panel {
      background:
        linear-gradient(rgba(125,211,252,.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(125,211,252,.025) 1px, transparent 1px),
        linear-gradient(180deg, #101827, #0b1320);
      background-size: 28px 28px, 28px 28px, auto;
      border: 1px solid rgba(56,189,248,.18);
      border-radius: 10px;
      box-shadow: 0 20px 48px rgba(2, 6, 23, .22), inset 0 1px 0 rgba(255,255,255,.04);
      overflow: hidden;
    }
    .desk-panel h2 {
      background: linear-gradient(90deg, rgba(8,17,31,.96), rgba(17,29,47,.92));
      border-bottom-color: #223247;
      color: #e5edf6;
    }
    .desk-panel h2::after { background: #38bdf8; }
    .desk-panel dl,
    .desk-panel dt,
    .desk-panel dd { color: #cbd5e1; }
    .desk-panel dt { color: #7dd3fc; }
    .desk-panel .content { color: #cbd5e1; }
    .desk-chart-toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      padding: 12px 14px;
      border-bottom: 1px solid #223247;
      background: linear-gradient(90deg, #08111f, #111d2f);
    }
    .chart-tabs {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .compact-tabs .chart-tab {
      min-width: 48px;
      padding: 0 9px;
    }
    .chart-tab {
      height: 30px;
      padding: 0 10px;
      border-radius: 6px;
      background: #111d2f;
      border-color: #2b4058;
      color: #cbd5e1;
      font-size: 12px;
    }
    .chart-tab.active {
      background: #0891b2;
      border-color: #22d3ee;
      color: white;
    }
    .chart-tab:hover {
      background: #17324a;
      border-color: #38bdf8;
    }
    .chart-caption {
      color: #8fb4c7;
      font-size: 12px;
      font-weight: 800;
    }
    .chart-hover-info {
      min-height: 20px;
      padding: 3px 10px;
      color: #dbeafe;
      font-size: 13px;
      font-weight: 800;
      background: linear-gradient(90deg, rgba(14,165,233,.12), rgba(34,197,94,.08));
      border-top: 1px solid rgba(56,189,248,.16);
      border-bottom: 1px solid rgba(56,189,248,.12);
    }
    .chart-shell {
      padding: 0;
      background: #070d16;
    }
    .chart-comparison {
      display: grid;
      grid-template-columns: 72px minmax(0, 1fr);
      background: #070d16;
      min-height: 0;
    }
    .attachment-rail {
      display: flex;
      flex-direction: column;
      gap: 7px;
      padding: 8px 7px;
      background: linear-gradient(180deg, #08111f, #0b1626);
      border-right: 1px solid rgba(56,189,248,.18);
    }
    .attachment-rail button {
      width: 100%;
      height: 34px;
      padding: 0;
      border-radius: 7px;
      background: rgba(14,165,233,.12);
      border-color: rgba(125,211,252,.28);
      color: #dbeafe;
      font-size: 12px;
      font-weight: 900;
    }
    .attachment-rail button:hover {
      background: rgba(14,165,233,.24);
      border-color: #38bdf8;
    }
    .chart-stack {
      min-width: 0;
      display: grid;
      grid-template-rows: auto auto auto;
      overflow: hidden;
      cursor: grab;
    }
    .chart-stack.dragging {
      cursor: grabbing;
      user-select: none;
    }
    .chart-canvas {
      min-width: 0;
    }
    .trading-desk .chart {
      background:
        linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.025) 1px, transparent 1px),
        linear-gradient(180deg, #07101d, #0d1726);
      background-size: 44px 44px, 44px 44px, auto;
      border-color: #213247;
      border-radius: 0;
    }
    .trading-desk .chart-axis { stroke: #334155; }
    .trading-desk .chart-label { fill: #94a3b8; }
    .technical-strip {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 0;
      padding: 0;
      background: #070d16;
    }
    .technical-strip.collapsed {
      display: none;
    }
    .technical-attachment-bar {
      display: none;
    }
    .technical-attachment-bar button {
      height: 28px;
      background: rgba(14,165,233,.12);
      border-color: rgba(125,211,252,.28);
      color: #dbeafe;
      font-size: 13px;
    }
    .mini-chart-panel.hidden {
      display: none;
    }
    .mini-chart-panel {
      background: #070d16;
      border: 0;
      border-radius: 0;
      overflow: hidden;
      box-shadow: none;
    }
    .mini-chart-panel h3 {
      display: none;
    }
    .mini-chart-panel h3 span {
      color: #7dd3fc;
      font-size: 11px;
      font-weight: 900;
    }
    .mini-chart-panel .content {
      padding: 0;
      background: #070d16;
    }
    .mini-chart-panel.expanded {
      grid-column: 1 / -1;
    }
    .mini-chart-panel.expanded .chart {
      height: clamp(170px, 24vh, 230px) !important;
    }
    .sync-cursor {
      pointer-events: none;
      stroke: #facc15;
      stroke-width: 1.4;
      opacity: 0;
    }
    .zoom-btn {
      height: 26px;
      padding: 0 9px;
      font-size: 12px;
      background: #111d2f;
      border-color: #2b4058;
      color: #cbd5e1;
    }
    .trend-summary {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }
    .realtime-terminal {
      display: grid;
      grid-template-columns: minmax(460px, 1.25fr) minmax(260px, .7fr) minmax(320px, .8fr);
      gap: 10px;
      align-items: stretch;
      background: #030506;
      border: 1px solid #1f2933;
      padding: 10px;
    }
    .terminal-chart,
    .terminal-tape,
    .terminal-depth,
    .terminal-watch {
      background: #050708;
      border: 1px solid #1f2933;
      min-width: 0;
      color: #dbeafe;
    }
    .terminal-chart {
      grid-row: span 2;
    }
    .terminal-watch {
      grid-column: 2 / 4;
    }
    .terminal-head {
      min-height: 38px;
      padding: 8px 10px;
      color: #f8fafc;
      font-size: 20px;
      font-weight: 900;
      border-bottom: 1px solid #1f2933;
    }
    .order-ratio {
      display: grid;
      grid-template-columns: 86px 1fr 86px;
      align-items: center;
      gap: 8px;
      padding: 6px 10px;
      color: #00e676;
      background: #020303;
      border-bottom: 1px solid #1f2933;
      font-size: 13px;
      font-weight: 900;
    }
    .order-ratio span:last-child {
      color: #ef4444;
      text-align: right;
    }
    .order-ratio b {
      display: block;
      height: 16px;
      background: #b91c1c;
      border-radius: 999px;
      overflow: hidden;
      position: relative;
    }
    .order-ratio i {
      display: block;
      height: 100%;
      background: #00a000;
      border-radius: 999px 0 0 999px;
    }
    .terminal-chart .chart {
      height: 370px;
      border: 0;
      background: #000;
    }
    .terminal-volume {
      min-height: 88px;
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 1px;
      background: #111827;
      border-top: 1px solid #1f2933;
    }
    .terminal-volume .trend-kpi {
      border: 0;
      border-radius: 0;
      background: #050708;
    }
    .terminal-tape h2,
    .terminal-depth h2,
    .terminal-watch h2 {
      margin: 0;
      padding: 8px 10px;
      background: #11181c;
      border-bottom: 1px solid #25313a;
      color: #dbeafe;
      font-size: 14px;
    }
    .tape-row,
    .depth-row {
      display: grid;
      grid-template-columns: 70px 1fr 1fr 1fr;
      gap: 8px;
      padding: 6px 10px;
      border-bottom: 1px solid #1a242b;
      color: #e5e7eb;
      font-size: 13px;
      font-weight: 800;
    }
    .depth-row {
      grid-template-columns: 72px 1fr 64px;
    }
    .depth-bar {
      background: #111;
      position: relative;
      min-height: 18px;
    }
    .depth-fill {
      position: absolute;
      inset: 0 auto 0 0;
      background: #075985;
    }
    .depth-row.current .depth-fill {
      background: #fbbf24;
    }
    .depth-row.pressure .depth-fill {
      background: #991b1b;
    }
    .trend-kpi {
      background: #0f1a2a;
      border: 1px solid #223247;
      border-radius: 8px;
      padding: 10px;
      color: #cbd5e1;
    }
    .trend-kpi span {
      display: block;
      color: #8fb4c7;
      font-size: 12px;
      font-weight: 900;
      margin-bottom: 5px;
    }
    .trend-kpi strong {
      color: #ffffff;
      font-size: 18px;
    }
    .trade-plan {
      display: grid;
      gap: 8px;
      padding: 10px;
    }
    .trade-callout {
      border: 1px solid #164e63;
      background: linear-gradient(135deg, #082f49, #0f172a);
      border-radius: 10px;
      padding: 10px;
    }
    .trade-callout span {
      color: #7dd3fc;
      display: block;
      font-size: 12px;
      font-weight: 900;
      margin-bottom: 7px;
    }
    .trade-callout strong {
      display: block;
      color: #e0f2fe;
      font-size: 17px;
      margin-bottom: 6px;
    }
    .trade-callout p {
      margin: 0;
      color: #cbd5e1;
      line-height: 1.55;
    }
    .trade-kpis {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 7px;
    }
    .trade-kpi {
      border: 1px solid #223247;
      border-radius: 8px;
      padding: 8px;
      background: #0f1a2a;
    }
    .trade-kpi span {
      display: block;
      color: #8fb4c7;
      font-size: 12px;
      font-weight: 900;
      margin-bottom: 6px;
    }
    .trade-kpi strong {
      color: #e5edf6;
      font-size: 15px;
    }
    .trade-list {
      margin: 0;
      padding-left: 18px;
      color: #cbd5e1;
      line-height: 1.5;
      font-size: 13px;
    }
    .desk-panel .diagnosis-card {
      background: #0f1a2a;
      border-color: #223247;
    }
    .desk-panel .diagnosis-card span { color: #8fb4c7; }
    .desk-panel .diagnosis-card strong { color: #e5edf6; }
    .fundamental-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(250px, 1fr));
      gap: 12px;
      padding: 14px 12px 12px;
    }
    .fundamental-visuals {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
      padding: 12px 12px 0;
    }
    .radar-stage {
      display: grid;
      grid-template-columns: minmax(420px, 1.1fr) minmax(280px, .9fr);
      gap: 12px;
      align-items: stretch;
    }
    .trend-wide .chart {
      height: 320px;
    }
    .research-command {
      display: grid;
      grid-template-columns: minmax(260px, .8fr) minmax(360px, 1.2fr);
      gap: 12px;
      padding: 12px 12px 0;
    }
    .research-verdict {
      background:
        linear-gradient(135deg, rgba(14, 165, 233, .24), rgba(34, 197, 94, .1) 46%, rgba(168, 85, 247, .1)),
        #0f1a2a;
      border: 1px solid rgba(56,189,248,.34);
      border-radius: 10px;
      padding: 14px;
      min-width: 0;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.05), 0 18px 45px rgba(2, 8, 23, .28);
    }
    .research-verdict span,
    .research-bars span {
      display: block;
      color: #8fb4c7;
      font-size: 12px;
      font-weight: 900;
      margin-bottom: 6px;
    }
    .research-verdict strong {
      display: block;
      color: #ffffff;
      font-size: 28px;
      line-height: 1.05;
      margin-bottom: 8px;
    }
    .research-verdict p {
      color: #cbd5e1;
      margin: 0;
      line-height: 1.5;
      font-size: 13px;
    }
    .research-score-row {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      margin-top: 12px;
    }
    .research-pill {
      border: 1px solid rgba(56,189,248,.2);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(15, 23, 42, .8), rgba(8, 17, 31, .8));
      padding: 8px;
    }
    .research-pill b {
      display: block;
      color: #e5edf6;
      font-size: 14px;
      margin-bottom: 3px;
    }
    .research-pill small {
      color: #94a3b8;
      font-size: 11px;
    }
    .research-bars {
      background:
        linear-gradient(rgba(56, 189, 248, .045) 1px, transparent 1px),
        linear-gradient(90deg, rgba(56, 189, 248, .045) 1px, transparent 1px),
        #0f1a2a;
      background-size: 22px 22px;
      border: 1px solid rgba(56,189,248,.24);
      border-radius: 10px;
      padding: 12px;
      min-width: 0;
      align-self: stretch;
    }
    .score-bar {
      display: grid;
      grid-template-columns: 58px 1fr 44px;
      gap: 8px;
      align-items: center;
      margin: 8px 0;
      color: #cbd5e1;
      font-size: 12px;
    }
    .score-track {
      height: 8px;
      border-radius: 999px;
      background: #162337;
      overflow: hidden;
      box-shadow: inset 0 0 0 1px rgba(148, 163, 184, .08);
    }
    .score-fill {
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #38bdf8, #22c55e, #f59e0b);
      width: var(--score);
      box-shadow: 0 0 14px rgba(56, 189, 248, .34);
    }
    .research-list-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-top: 10px;
    }
    .research-list-grid ul {
      margin: 0;
      padding-left: 18px;
      color: #cbd5e1;
      font-size: 12px;
      line-height: 1.45;
    }
    .fundamental-visual {
      background:
        radial-gradient(circle at 20% 0%, rgba(14, 165, 233, .22), transparent 30%),
        radial-gradient(circle at 80% 12%, rgba(34, 197, 94, .12), transparent 26%),
        linear-gradient(rgba(56, 189, 248, .035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(56, 189, 248, .035) 1px, transparent 1px),
        #0f1a2a;
      background-size: auto, auto, 24px 24px, 24px 24px, auto;
      border: 1px solid rgba(56,189,248,.25);
      border-radius: 10px;
      padding: 12px;
      min-width: 0;
      box-shadow: 0 16px 40px rgba(2, 8, 23, .24);
    }
    .fundamental-visual h3 {
      margin: 0 0 8px;
      font-size: 14px;
      color: #e5edf6;
    }
    .fundamental-visual .chart {
      height: 380px;
    }
    .fundamental-mini-table {
      width: 100%;
      border-collapse: collapse;
      color: #cbd5e1;
      font-size: 12px;
      margin-top: 8px;
    }
    .fundamental-mini-table th,
    .fundamental-mini-table td {
      border-bottom: 1px solid rgba(148, 163, 184, .14);
      padding: 6px 4px;
      text-align: right;
    }
    .fundamental-mini-table th:first-child,
    .fundamental-mini-table td:first-child {
      text-align: left;
    }
    .fundamental-card {
      background:
        linear-gradient(180deg, rgba(15, 26, 42, .96), rgba(8, 17, 31, .96));
      border: 1px solid rgba(56,189,248,.2);
      border-radius: 10px;
      padding: 12px;
      color: #cbd5e1;
      min-width: 0;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.035);
    }
    .fundamental-card span {
      display: block;
      color: #7dd3fc;
      font-size: 12px;
      font-weight: 900;
      margin-bottom: 6px;
    }
    .fundamental-card strong {
      display: block;
      color: #ffffff;
      font-size: 16px;
      margin-bottom: 8px;
    }
    .fundamental-card ul {
      margin: 0;
      padding-left: 18px;
      font-size: 13px;
      line-height: 1.55;
    }
    .news-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(240px, 1fr));
      gap: 12px;
      padding: 12px;
    }
    .news-card {
      background: linear-gradient(180deg, #0f1a2a, #08111f);
      border: 1px solid rgba(56,189,248,.2);
      border-radius: 10px;
      padding: 12px;
      color: #cbd5e1;
    }
    .news-card span {
      color: #7dd3fc;
      font-size: 12px;
      font-weight: 900;
    }
    .news-card a {
      display: block;
      color: #ffffff;
      font-weight: 800;
      line-height: 1.45;
      margin: 8px 0;
    }
    .news-card small {
      color: #94a3b8;
      font-size: 12px;
    }
    .inner-grid {
      display: grid;
      grid-template-columns: 1.15fr .85fr;
      gap: 14px;
      align-items: start;
    }
    .research-panel {
      border-top: 3px solid #0ea5e9;
    }
    .realtime-board {
      background:
        linear-gradient(135deg, #0f172a, #0e302d);
      border: 1px solid #1f3b4c;
      color: #e5edf6;
    }
    .realtime-board strong { color: #ffffff; }
    .realtime-board span { color: #a7f3d0; }
    .realtime-row {
      cursor: pointer;
    }
    .realtime-row.selected {
      background: #06283a;
      color: #f8fdff;
      outline: 1px solid rgba(56,189,248,.65);
      box-shadow: inset 3px 0 0 #22d3ee;
    }
    .desk-panel .realtime-row.selected {
      background: #06283a;
    }
    .realtime-row.selected td,
    .desk-panel .realtime-row.selected td {
      color: #f8fdff;
    }
    .realtime-row.selected .positive,
    .desk-panel .realtime-row.selected .positive {
      color: #00ffc6;
    }
    .realtime-row.selected .negative,
    .desk-panel .realtime-row.selected .negative {
      color: #ff5c5c;
    }
    .realtime-row.selected button {
      background: #e6fbff;
      color: #082f49;
      border-color: #7dd3fc;
    }
    .realtime-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .realtime-actions button {
      background: rgba(255,255,255,.10);
      border-color: rgba(255,255,255,.18);
      color: #e5edf6;
    }
    .realtime-row {
      cursor: pointer;
    }
    .realtime-row:hover td:first-child {
      color: var(--accent-dark);
      font-weight: 900;
    }
    @media (max-width: 820px) {
      header { display: block; }
      body { font-size: 14px; }
      .app-shell { grid-template-columns: 1fr; padding: 8px; }
      .sidebar {
        position: sticky;
        top: 0;
        z-index: 20;
        padding: 8px;
        border-radius: 10px;
      }
      .nav-list {
        display: flex;
        overflow-x: auto;
        gap: 8px;
        padding-bottom: 2px;
      }
      .nav-item {
        min-width: 96px;
        grid-template-columns: 24px auto;
        padding: 8px;
        white-space: nowrap;
      }
      .sidebar-title { display: none; }
      .hero-panel, .hero-stat-grid, .market-strip, .module-grid, .diagnosis, .watch-grid, .strategy-advice, .strategy-kpis, .strategy-stock-grid, .strategy-guide, .trading-desk, .desk-side, .trade-kpis, .technical-strip, .fundamental-grid, .fundamental-visuals, .research-command, .research-score-row, .research-list-grid, .news-grid, .inner-grid, .explore-hero, .trend-summary, .realtime-terminal { grid-template-columns: 1fr; }
      .terminal-chart, .terminal-watch { grid-column: auto; grid-row: auto; }
      .desk-side dl { grid-template-columns: 1fr; }
      .desk-side .diagnosis { grid-template-columns: 1fr; }
      .chart-comparison { grid-template-columns: 1fr; }
      .attachment-rail {
        flex-direction: row;
        overflow-x: auto;
        border-right: 0;
        border-bottom: 1px solid rgba(56,189,248,.18);
      }
      .attachment-rail button { min-width: 72px; }
      #priceChart { height: 330px; }
      #rsiChart, #macdChart, #kdChart, #volumeChart, #institutionalChart { height: 190px; }
      .chart-canvas { min-width: 0; }
      .terminal-chart .chart { height: 300px; }
      .radar-stage { grid-template-columns: 1fr; }
      .toolbar, .watch-tools, .grid, .explore-actions { grid-template-columns: 1fr; }
      .desk-chart-toolbar {
        display: grid;
        grid-template-columns: 1fr;
      }
      .chart-tabs {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }
      button { width: 100%; }
      .chart-tabs .chart-tab,
      .realtime-actions button {
        width: auto;
        padding: 0 8px;
      }
      dl { grid-template-columns: 1fr; }
      .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <h1>台股智研 Pro</h1>
      <div class="subtitle">專業台股智能分析平台 · 訊號排行 · AI 實操 · 風控回測</div>
    </div>
    <div class="version"><span id="range">載入中...</span> · UI v26</div>
  </header>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="sidebar-title">功能導覽</div>
      <nav class="nav-list">
        <a class="nav-item active" data-page="overview" href="#" onclick="showPage('overview', this); return false;"><span class="nav-icon">OV</span><span>總覽</span></a>
        <a class="nav-item" data-page="realtimePage" href="#" onclick="showPage('realtimePage', this); return false;"><span class="nav-icon">RT</span><span>即時看盤</span></a>
        <a class="nav-item" data-page="watchPage" href="#" onclick="showPage('watchPage', this); return false;"><span class="nav-icon">MK</span><span>盤後看盤</span></a>
        <a class="nav-item" data-page="signalsPage" href="#" onclick="showPage('signalsPage', this); return false;"><span class="nav-icon">AI</span><span>智能選股</span></a>
        <a class="nav-item" data-page="strategyPage" href="#" onclick="showPage('strategyPage', this); return false;"><span class="nav-icon">OP</span><span>AI 實操</span></a>
        <a class="nav-item" data-page="stockPage" href="#" onclick="showPage('stockPage', this); return false;"><span class="nav-icon">ST</span><span>個股分析</span></a>
        <a class="nav-item" data-page="rankingPage" href="#" onclick="showPage('rankingPage', this); return false;"><span class="nav-icon">RK</span><span>市場排行</span></a>
        <a class="nav-item" data-page="hubPage" href="#" onclick="showPage('hubPage', this); return false;"><span class="nav-icon">HB</span><span>股票探索</span></a>
        <a class="nav-item" data-page="guidePage" href="#" onclick="showPage('guidePage', this); return false;"><span class="nav-icon">GD</span><span>使用手冊</span></a>
        <a class="nav-item" data-page="notifyPage" href="#" onclick="showPage('notifyPage', this); return false;"><span class="nav-icon">TG</span><span>推播設定</span></a>
      </nav>
    </aside>
  <main class="workspace">
    <div class="dashboard-note overview-only">
      <div>
        <strong>智能投資決策中心</strong><br>
        <span>整合上市櫃資料、AI 訊號分數、AI 實操回測與 Telegram 盤後推播，協助快速建立每日觀察名單。</span>
      </div>
      <div class="pill">V1 研究模式</div>
    </div>
    <div class="toolbar overview-only">
      <input id="code" value="2330" aria-label="股票代號">
      <button type="button" class="primary" id="loadStock" onclick="searchStock()">智能搜尋</button>
      <button type="button" id="loadScan" onclick="runAction(fetchScan, '正在載入市場掃描...')">市場排行榜</button>
      <button type="button" id="loadSignals" onclick="runAction(fetchSignals, '正在載入訊號排行...')">AI 訊號</button>
      <button type="button" id="loadStrategy" onclick="runAction(fetchStrategy, '正在執行 AI 實操回測，可能需要約一分鐘...')">AI 實操</button>
      <button type="button" id="refreshStatus" onclick="runAction(fetchStatus, '正在更新狀態...')">更新狀態</button>
    </div>
    <div class="status" id="statusLine">準備就緒。</div>

    <div class="page active" id="overview">
      <div class="hero-panel">
        <div>
          <div class="eyebrow">TAIWAN EQUITY RESEARCH DESK</div>
          <h2>今日台股研究工作台</h2>
          <p>從盤勢廣度、AI 訊號、觀察名單與實操風控切入，先判斷市場環境，再決定要進攻、精選或防守。</p>
          <div class="hero-actions">
            <button type="button" class="primary" onclick="showPage('signalsPage')">查看 AI 訊號</button>
            <button type="button" onclick="showPage('watchPage')">打開觀察名單</button>
            <button type="button" onclick="showPage('realtimePage')">即時看盤</button>
            <button type="button" onclick="showPage('strategyPage')">AI 實操中心</button>
          </div>
        </div>
        <div class="hero-stat-grid">
          <div class="hero-stat"><span>盤面判讀</span><strong>先看廣度</strong></div>
          <div class="hero-stat"><span>交易節奏</span><strong>分批控管</strong></div>
          <div class="hero-stat"><span>核心工具</span><strong>K 線技術圖</strong></div>
          <div class="hero-stat"><span>提醒管道</span><strong>Telegram</strong></div>
        </div>
      </div>
      <div class="market-strip">
        <div class="market-tile accent"><span>AI 實操</span><strong>盤勢 + 回測</strong></div>
        <div class="market-tile blue"><span>即時看盤</span><strong>觀察名單同步</strong></div>
        <div class="market-tile gold"><span>智能選股</span><strong>風險調整分數</strong></div>
        <div class="market-tile cyan"><span>個股分析</span><strong>K 線與技術圖</strong></div>
      </div>
      <div class="grid">
        <section class="wide">
          <h2>今日研究流程</h2>
          <div class="content"><dl>
            <dt>第一步</dt><dd>先看 AI 實操判斷目前盤面是進攻、精選或防守</dd>
            <dt>第二步</dt><dd>用 AI 訊號與觀察名單縮小股票池</dd>
            <dt>第三步</dt><dd>進入個股 K 線、均線、RSI、MACD、KD 和布林通道確認風險</dd>
          </dl></div>
        </section>
        <section class="wide">
          <h2>功能分流</h2>
          <div class="content"><dl>
            <dt>股票探索</dt><dd>集中查看 AI 智選、強勢股票與量能焦點</dd>
            <dt>盤後看盤</dt><dd>只保留觀察名單與盤後摘要，避免資訊重複</dd>
            <dt>即時看盤</dt><dd>盤中報價、即時走勢與 AI 盤中盯盤</dd>
            <dt>AI 實操</dt><dd>查看建倉、了結、未平倉與回測績效</dd>
          </dl></div>
        </section>
      </div>
    </div>

    <div class="page" id="hubPage">
      <div class="explore-hero">
        <div>
          <h2>股票探索工作台</h2>
          <p>從 AI 智選、強勢排行與量能焦點挑股票，點「分析」直接進入 K 線、技術圖與交易計畫。</p>
        </div>
        <div class="explore-actions">
          <input id="hubCode" value="2330" aria-label="股票探索股票代號">
          <button type="button" class="primary" onclick="openHubStock()">分析個股</button>
          <button type="button" onclick="runAction(fetchHub, '正在刷新股票探索...')">刷新</button>
        </div>
      </div>
      <div class="module-grid">
        <div class="module-card"><strong>AI 智選</strong><span>用風險調整分數排序，優先找動能、趨勢與量能較完整的股票。</span></div>
        <div class="module-card"><strong>強勢排行</strong><span>追蹤 20 日漲幅，快速看到市場資金偏好的族群。</span></div>
        <div class="module-card"><strong>量能焦點</strong><span>找出成交量突然放大的股票，再回到個股頁確認型態。</span></div>
      </div>
      <div class="grid">
        <section class="wide">
          <h2>AI 智選名單</h2>
          <div class="table-wrap"><table id="hubSignalsTable"></table></div>
        </section>
        <section>
          <h2>強勢股票</h2>
          <div class="table-wrap"><table id="hubReturnTable"></table></div>
        </section>
        <section>
          <h2>量能焦點</h2>
          <div class="table-wrap"><table id="hubVolumeTable"></table></div>
        </section>
      </div>
    </div>

    <div class="page" id="watchPage">
      <div class="watch-grid">
        <div class="watch-card"><span>60 日新高</span><strong id="watchHighs">-</strong></div>
        <div class="watch-card"><span>站上 SMA20</span><strong id="watchSma20">-</strong></div>
        <div class="watch-card"><span>站上 SMA60</span><strong id="watchSma60">-</strong></div>
      </div>
      <div class="watch-tools">
        <input id="watchlistCode" value="2330" aria-label="觀察名單股票代號">
        <button type="button" class="primary" onclick="runAction(addWatchlistCode, '正在加入觀察名單...')">加入觀察</button>
        <button type="button" onclick="useWatchlistRealtime()">同步到即時看盤</button>
        <button type="button" onclick="runAction(fetchWatch, '正在刷新觀察名單...')">刷新名單</button>
        <div class="hint" id="watchlistHint">觀察名單會保存在本機資料庫。</div>
      </div>
      <div class="grid">
        <section class="wide">
          <h2>我的觀察名單</h2>
          <div class="table-wrap"><table id="watchMajorsTable"></table></div>
        </section>
        <section class="wide">
          <h2>盤後觀察重點</h2>
          <div class="content"><dl id="watchAfterSummary"></dl></div>
        </section>
      </div>
    </div>

    <div class="page" id="realtimePage">
      <div class="dashboard-note realtime-board">
        <div>
          <strong>即時看盤工作台</strong><br>
          <span>輸入觀察代號後刷新報價，點擊任一列查看走勢或進入個股分析。</span>
        </div>
        <div class="realtime-actions">
          <button type="button" onclick="useWatchlistRealtime()">觀察名單</button>
          <button type="button" onclick="startRealtime()">自動刷新</button>
        </div>
      </div>
      <div class="toolbar">
        <input id="realtimeCodes" value="" placeholder="使用觀察名單" aria-label="即時看盤股票代號">
        <button type="button" class="primary" onclick="runAction(fetchRealtime, '正在刷新即時報價...')">刷新報價</button>
        <button type="button" onclick="useWatchlistRealtime()">載入觀察名單</button>
        <button type="button" onclick="startRealtime()">開始自動刷新</button>
        <button type="button" onclick="stopRealtime()">停止自動刷新</button>
      </div>
      <div class="status" id="realtimeNotice">尚未載入報價。</div>
      <div class="realtime-terminal">
        <section class="terminal-chart">
          <div class="terminal-head" id="realtimeTerminalHead">請先載入觀察名單</div>
          <div class="order-ratio" id="orderRatioBar">
            <span>內盤 50.00%</span>
            <b><i style="width:50%"></i></b>
            <span>外盤 50.00%</span>
          </div>
          <svg id="realtimeTrendChart" class="chart" role="img" aria-label="即時走勢圖"></svg>
          <div class="terminal-volume" id="realtimeTrendSummary"></div>
          <div class="note" id="realtimeTrendNotice">點擊股票列查看即時走勢。</div>
        </section>
        <section class="terminal-tape">
          <h2>成交明細</h2>
          <div id="realtimeTape" class="tape-list"></div>
        </section>
        <section class="terminal-depth">
          <h2>五檔與分價</h2>
          <div id="realtimeDepth" class="depth-board"></div>
        </section>
        <section class="terminal-watch">
          <h2>觀察名單報價</h2>
          <div class="table-wrap"><table id="realtimeTable"></table></div>
        </section>
      </div>
      <section class="wide">
        <h2>AI 盤中盯盤</h2>
        <div class="content" id="aiMonitorSummary"></div>
        <div class="table-wrap"><table id="aiMonitorTable"></table></div>
      </section>
    </div>

    <div class="page" id="stockPage">
      <div class="grid">
      <section class="wide">
        <div class="stock-hero">
          <div>
            <h2 id="stockHeroName">個股診斷報告</h2>
            <div class="sub" id="stockHeroSub">輸入股票代號後產生 AI 診斷、K 線與技術分析圖</div>
          </div>
          <div class="score-badge">
            <span>智研評分</span>
            <strong id="heroScore">-</strong>
          </div>
        </div>
      </section>
      <div class="wide trading-desk">
        <div class="desk-main">
          <section class="desk-panel">
            <div class="desk-chart-toolbar">
              <div class="chart-tabs">
                <button type="button" class="chart-tab active" data-chart-mode="all" onclick="setChartMode('all', this)">全覽</button>
                <button type="button" class="chart-tab" data-chart-mode="ma" onclick="setChartMode('ma', this)">均線</button>
                <button type="button" class="chart-tab" data-chart-mode="bollinger" onclick="setChartMode('bollinger', this)">布林</button>
              </div>
              <div class="chart-tabs compact-tabs">
                <button type="button" class="chart-tab active" data-interval="1d" onclick="setStockInterval('1d', this)">日K</button>
                <button type="button" class="chart-tab" data-interval="1wk" onclick="setStockInterval('1wk', this)">週K</button>
                <button type="button" class="chart-tab" data-interval="1mo" onclick="setStockInterval('1mo', this)">月K</button>
                <button type="button" class="chart-tab" data-interval="30m" onclick="setStockInterval('30m', this)">30分</button>
              </div>
              <div class="chart-caption" id="chartCaption">K 線 / 成交量 / MA5 / MA10 / 月線 / 布林通道</div>
            </div>
            <div class="chart-comparison">
              <div class="attachment-rail" aria-label="附圖切換">
                <button type="button" onclick="showTechnicalAttachment('macd')">MACD</button>
                <button type="button" onclick="showTechnicalAttachment('kd')">KD</button>
                <button type="button" onclick="showTechnicalAttachment('rsi')">RSI</button>
                <button type="button" onclick="showTechnicalAttachment('volume')">成交量</button>
                <button type="button" onclick="showChipAttachment('foreign')">外資</button>
                <button type="button" onclick="showChipAttachment('investment')">投信</button>
                <button type="button" onclick="showChipAttachment('retail_proxy')">散戶</button>
              </div>
              <div class="chart-stack" id="chartStack">
                <div class="chart-canvas">
                  <div class="chart-shell"><svg id="priceChart" class="chart" role="img" aria-label="K 線圖"></svg></div>
                  <div class="chart-hover-info" id="chartHoverInfo">滑過 K 棒可查看開高低收、成交量與時間。</div>
                  <div class="technical-strip" id="technicalStrip">
                    <section class="mini-chart-panel hidden" data-tech-panel="volume">
                      <h3><span>成交量</span><button type="button" class="zoom-btn" onclick="toggleChartZoom(this)">放大</button></h3>
                      <div class="content"><svg id="volumeChart" class="chart" role="img" aria-label="成交量圖"></svg></div>
                    </section>
                    <section class="mini-chart-panel hidden" data-tech-panel="rsi">
                      <h3><span>RSI 14</span><button type="button" class="zoom-btn" onclick="toggleChartZoom(this)">放大</button></h3>
                      <div class="content"><svg id="rsiChart" class="chart" role="img" aria-label="RSI 技術圖"></svg></div>
                    </section>
                    <section class="mini-chart-panel expanded" data-tech-panel="macd">
                      <h3><span>MACD 12 / 26 / 9</span><button type="button" class="zoom-btn" onclick="toggleChartZoom(this)">縮小</button></h3>
                      <div class="content"><svg id="macdChart" class="chart" role="img" aria-label="MACD 技術圖"></svg></div>
                    </section>
                    <section class="mini-chart-panel hidden" data-tech-panel="kd">
                      <h3><span>KD 9</span><button type="button" class="zoom-btn" onclick="toggleChartZoom(this)">放大</button></h3>
                      <div class="content"><svg id="kdChart" class="chart" role="img" aria-label="KD 技術圖"></svg></div>
                    </section>
                    <section class="mini-chart-panel hidden" data-tech-panel="chips">
                      <h3><span id="chipChartTitle">外資買賣超</span><button type="button" class="zoom-btn" onclick="toggleChartZoom(this)">放大</button></h3>
                      <div class="content"><svg id="institutionalChart" class="chart" role="img" aria-label="法人買賣超圖"></svg></div>
                    </section>
                  </div>
                </div>
              </div>
            </div>
          </section>
        </div>
        <div class="desk-side">
          <section class="desk-panel">
            <h2>AI 個股診斷</h2>
            <div class="content">
              <div class="diagnosis">
                <div class="diagnosis-card"><span>智研評分</span><strong id="diagScore">-</strong></div>
                <div class="diagnosis-card"><span>市場情緒</span><strong id="diagSentiment">-</strong></div>
                <div class="diagnosis-card"><span>風險分數</span><strong id="diagRiskScore">-</strong></div>
              </div>
            </div>
          </section>
          <section class="desk-panel">
            <h2>交易計畫</h2>
            <div id="tradePlan" class="trade-plan"></div>
          </section>
          <section class="desk-panel">
            <h2>分析構面</h2>
            <div id="analysisFacets" class="trade-plan"></div>
          </section>
          <section class="desk-panel">
            <h2>個股摘要</h2>
            <div class="content"><dl id="stockSummary"></dl></div>
          </section>
          <section class="desk-panel">
            <h2>技術指標</h2>
            <div class="content"><dl id="indicators"></dl></div>
          </section>
        </div>
        <section class="desk-panel">
          <h2>基本面研究</h2>
          <div id="fundamentalResearch" class="fundamental-research"></div>
        </section>
        <section class="desk-panel">
          <h2>最新消息掃描</h2>
          <div id="newsResearch" class="news-grid"></div>
        </section>
      </div>
      </div>
    </div>

    <div class="page" id="strategyPage">
      <div class="grid">
      <section class="wide">
        <h2>AI 實操盤面建議</h2>
        <div class="content" id="strategyAdvice"></div>
      </section>
      <section class="wide">
        <h2>AI 實操怎麼看</h2>
        <div class="content strategy-guide">
          <div><strong>勝率</strong><span>代表過去交易有多少比例賺錢，但不是越高越好，還要看賺賠幅度。</span></div>
          <div><strong>最大回撤</strong><span>代表 AI 實操過程中曾經從高點跌下來多少，是新人最需要先看的風險指標。</span></div>
          <div><strong>總報酬</strong><span>代表回測期間 AI 實操累積成果，需搭配最大回撤一起看。</span></div>
          <div><strong>未平倉部位</strong><span>代表目前 AI 實操仍持有、尚未出場的標的，不等於立刻買進建議。</span></div>
        </div>
      </section>
      <section class="wide">
        <h2>高勝率保守模式</h2>
        <div class="content" id="highWinStrategy"></div>
        <div class="table-wrap"><table id="highWinTradesTable"></table></div>
      </section>
      <section class="wide">
        <h2>AI 實操個股狀況</h2>
        <div class="content" id="strategyStockCards"></div>
      </section>
      <section class="wide">
        <h2>AI 實操摘要</h2>
        <div class="content"><dl id="strategySummary"></dl></div>
      </section>
      <section class="wide">
        <h2>目前可研究標的</h2>
        <div class="table-wrap"><table id="strategyLeadersTable"></table></div>
      </section>
      <section class="wide">
        <h2>AI 實操建倉紀錄</h2>
        <div class="table-wrap"><table id="strategyEntriesTable"></table></div>
      </section>
      <section class="wide">
        <h2>AI 實操了結紀錄</h2>
        <div class="table-wrap"><table id="strategyTradesTable"></table></div>
      </section>
      <section class="wide">
        <h2>AI 實操未平倉</h2>
        <div class="table-wrap"><table id="strategyOpenTable"></table></div>
      </section>
      <section class="wide">
        <h2>資金曲線</h2>
        <div class="content"><svg id="equityChart" class="chart" role="img" aria-label="AI 實操資金曲線"></svg></div>
        <div class="table-wrap"><table id="strategyCurveTable"></table></div>
      </section>
      </div>
    </div>

    <div class="page" id="signalsPage">
      <div class="grid">
      <section class="wide">
        <div class="dashboard-note realtime-board">
          <div>
            <strong>AI 智選分析列隊</strong><br>
            <span>每檔股票都可以直接進入完整技術圖、K 線、交易計畫與風控建議。</span>
          </div>
          <div class="pill">可點擊分析</div>
        </div>
      </section>
      <section class="wide">
        <h2>AI 智選名單</h2>
        <div class="table-wrap"><table id="signalsTable"></table></div>
      </section>
      </div>
    </div>

    <div class="page" id="rankingPage">
      <div class="grid">
      <section class="wide">
        <h2>20 日漲幅排行</h2>
        <div class="table-wrap"><table id="returnTable"></table></div>
      </section>
      <section class="wide">
        <h2>量能放大排行</h2>
        <div class="table-wrap"><table id="volumeTable"></table></div>
      </section>
      </div>
    </div>

    <div class="page" id="notifyPage">
      <div class="grid">
        <section class="wide">
          <h2>個人 Telegram 推播</h2>
          <div class="toolbar">
            <input id="notifyName" placeholder="你的名稱，例如 Kevin" aria-label="推播使用者名稱">
            <button type="button" onclick="runAction(createUserProfile, '正在建立個人推播設定...')">建立個人設定</button>
          </div>
          <div class="toolbar">
            <input id="telegramChatId" placeholder="Telegram chat_id" aria-label="Telegram chat id">
            <button type="button" onclick="runAction(saveUserTelegram, '正在儲存 Telegram...')">儲存 Telegram</button>
            <button type="button" onclick="runAction(sendUserTelegramTest, '正在發送測試推播...')">發送測試推播</button>
          </div>
          <div class="hint" id="notifyHint">朋友可建立自己的個人設定、觀察名單與 Telegram 推播，不會影響主機名單。</div>
          <div class="content"><dl>
            <dt>個人代碼</dt><dd id="notifyUserKey">尚未建立</dd>
            <dt>推播狀態</dt><dd id="notifyTelegramStatus">尚未設定</dd>
            <dt>推播內容</dt><dd>個人觀察名單、AI 分數、買點、賣點、停損與操作提醒</dd>
            <dt>自動排程</dt><dd>正式主機可每天盤後批次發送給所有已啟用 Telegram 的使用者</dd>
          </dl></div>
        </section>
      </div>
    </div>

    <div class="page" id="guidePage">
      <div class="grid">
        <section class="wide">
          <h2>使用手冊</h2>
          <div class="content"><dl>
            <dt>快速入門</dt><dd>先看總覽，再用智能搜尋查個股，最後檢查 AI 訊號與 AI 實操。</dd>
            <dt>AI 智能分析</dt><dd>智研評分綜合技術面、籌碼面、消息面、產業面、三大法人、風險面與資金面。</dd>
            <dt>AI 實操</dt><dd>用歷史資料驗證 Top signals 的表現，包含勝率、最大回撤與交易明細。</dd>
            <dt>推播設定</dt><dd>每天 15:30 盤後更新資料並推播 Telegram 摘要。</dd>
          </dl></div>
        </section>
      </div>
    </div>
  </main>
  </div>
  <script>
    const fmt = (value, digits = 2) => {
      if (value === null || value === undefined) return "無資料";
      if (typeof value === "boolean") return value ? "是" : "否";
      if (typeof value === "number") return value.toLocaleString(undefined, { maximumFractionDigits: digits });
      return value;
    };
    const pctClass = value => value > 0 ? "positive" : value < 0 ? "negative" : "";
    let publicDemoMode = false;
    const publicWatchlistKey = "stock_v1_public_watchlist";
    const userKeyStorageKey = "stock_v1_user_key";
    let currentUserKey = localStorage.getItem(userKeyStorageKey) || "";
    async function getJson(url) {
      const response = await fetch(url);
      const data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || "請求失敗");
      return data;
    }
    async function initPublicConfig() {
      try {
        const config = await getJson("/api/public-config");
        publicDemoMode = !!config.public_demo;
      } catch (error) {
        publicDemoMode = false;
      }
      await loadUserProfile();
    }
    function localWatchlistCodes() {
      try {
        const saved = JSON.parse(localStorage.getItem(publicWatchlistKey) || "[]");
        return Array.isArray(saved) && saved.length ? saved : ["2330", "2367", "2454"];
      } catch (error) {
        return ["2330", "2367", "2454"];
      }
    }
    function saveLocalWatchlist(codes) {
      const clean = [...new Set(codes.map(code => String(code).trim()).filter(Boolean))];
      localStorage.setItem(publicWatchlistKey, JSON.stringify(clean));
      return clean;
    }
    async function getWatchlistData() {
      if (currentUserKey) return getJson(`/api/user/watchlist?user_key=${encodeURIComponent(currentUserKey)}`);
      if (!publicDemoMode) return getJson("/api/watchlist");
      const codes = localWatchlistCodes();
      return getJson(`/api/watchlist?codes=${encodeURIComponent(codes.join(","))}`);
    }
    function renderDl(target, rows) {
      target.innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    }
    function renderTable(target, rows) {
      target.innerHTML = `
        <thead><tr><th>代號</th><th>名稱</th><th>收盤</th><th>20日%</th><th>60日%</th><th>量比</th><th>RSI</th><th>操作</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr>
            <td>${row.code}</td><td title="${row.name}">${row.short_name || row.name}</td><td>${fmt(row.close)}</td>
            <td class="${pctClass(row.return_20d)}">${fmt(row.return_20d)}</td>
            <td class="${pctClass(row.return_60d)}">${fmt(row.return_60d)}</td>
            <td>${fmt(row.volume_ratio)}</td><td>${fmt(row.rsi_14)}</td>
            <td><button type="button" class="compact" onclick="openStock('${row.code}')">分析</button></td>
          </tr>`).join("")}</tbody>`;
    }
    function renderSignals(target, rows) {
      target.innerHTML = `
        <thead><tr><th>代號</th><th>名稱</th><th>分數</th><th>訊號</th><th>收盤</th><th>20日%</th><th>60日%</th><th>回撤</th><th>建倉位</th><th>出倉位</th><th>停損</th><th>量比</th><th>操作</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr>
            <td>${row.code}</td><td title="${row.name}">${row.short_name || row.name}</td><td>${row.score}</td><td>${row.signal}</td>
            <td>${fmt(row.close)}</td><td class="${pctClass(row.return_20d)}">${fmt(row.return_20d)}</td>
            <td class="${pctClass(row.return_60d)}">${fmt(row.return_60d)}</td>
            <td class="${row.drawdown_alert ? "negative" : ""}" title="${row.drawdown_alert || "未達提醒線"}">${fmt(row.drawdown_pct)}%</td>
            <td>${row.entry_zone || "-"}</td><td>${row.exit_zone || "-"}</td><td class="negative">${row.stop || "-"}</td>
            <td>${fmt(row.volume_ratio)}</td>
            <td><button type="button" class="compact" onclick="openStock('${row.code}')">分析</button></td>
          </tr>`).join("")}</tbody>`;
    }
    function renderMajors(target, rows) {
      if (!rows.length) {
        target.innerHTML = `
          <thead><tr><th>狀態</th></tr></thead>
          <tbody><tr><td>尚未加入觀察股票，請在上方輸入股票代號。</td></tr></tbody>`;
        return;
      }
      target.innerHTML = `
        <thead><tr><th>代號</th><th>名稱</th><th>收盤</th><th>5日%</th><th>20日%</th><th>60日%</th><th>RSI</th><th>量比</th><th>訊號</th><th>買點</th><th>賣點</th><th>停損</th><th>操作</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr>
            <td>${row.code}</td><td>${row.name}</td><td>${fmt(row.close)}</td>
            <td class="${pctClass(row.return_5d)}">${fmt(row.return_5d)}</td>
            <td class="${pctClass(row.return_20d)}">${fmt(row.return_20d)}</td>
            <td class="${pctClass(row.return_60d)}">${fmt(row.return_60d)}</td>
            <td>${fmt(row.rsi_14)}</td><td>${fmt(row.volume_ratio)}</td><td>${row.signal || "資料不足"}</td>
            <td>${row.buy_zone || "-"}</td><td>${row.sell_zone || "-"}</td><td class="negative">${row.stop || "-"}</td>
            <td>
              <button type="button" class="compact" onclick="openStock('${row.code}')">分析</button>
              <button type="button" class="compact" onclick="removeWatchlistCode('${row.code}')">移除</button>
            </td>
          </tr>`).join("")}</tbody>`;
    }
    function renderRealtime(target, rows) {
      if (!rows.length) {
        target.innerHTML = `
          <thead><tr><th>狀態</th></tr></thead>
          <tbody><tr><td>目前沒有可顯示的報價，請確認股票代號是否正確。</td></tr></tbody>`;
        return;
      }
      target.innerHTML = `
        <thead><tr><th>代號</th><th>名稱</th><th>市場</th><th>現價</th><th>漲跌</th><th>漲跌%</th><th>成交量</th><th>操作</th></tr></thead>
        <tbody>${rows.map(row => {
          return `
          <tr class="realtime-row" data-code="${row.code}" onclick="selectRealtimeTrend('${row.code}')" title="點擊查看 ${row.code} 即時走勢">
            <td>${row.code}</td><td>${row.name}</td><td>${row.market}</td><td>${fmt(row.price)}</td>
            <td class="${pctClass(row.change)}">${fmt(row.change)}</td>
            <td class="${pctClass(row.change_percent)}">${fmt(row.change_percent)}</td>
            <td>${fmt(row.volume, 0)}</td>
            <td>
              <button type="button" class="compact" onclick="event.stopPropagation(); selectRealtimeTrend('${row.code}')">走勢</button>
              <button type="button" class="compact" onclick="event.stopPropagation(); openStock('${row.code}')">分析</button>
            </td>
          </tr>`;
        }).join("")}</tbody>`;
      renderRealtimeBoard(rows[0]);
    }
    function renderRealtimeBoard(row) {
      if (!row) return;
      const head = document.getElementById("realtimeTerminalHead");
      if (head) {
        head.innerHTML = `${row.code} ${row.name} <span class="${pctClass(row.change)}" style="margin-left:18px">${fmt(row.price)} ${fmt(row.change)}(${fmt(row.change_percent)}%)</span> <span style="float:right;color:#94a3b8">${row.date || ""}</span>`;
      }
      renderOrderRatio(row);
      const tape = document.getElementById("realtimeTape");
      const depth = document.getElementById("realtimeDepth");
      if (!tape || !depth) return;
      const base = Number(row.price || 0);
      const vol = Math.max(1, Number(row.volume || 0));
      const times = ["14:30:00", "13:30:00", "13:24:59", "13:24:58", "13:24:56", "13:24:55", "13:24:54"];
      tape.innerHTML = times.map((time, index) => {
        const price = base + ((index % 3) - 1) * 0.1;
        const qty = Math.max(1, Math.round(vol / (index + 20) / 100));
        return `<div class="tape-row"><span>${time}</span><span class="${pctClass(price - base)}">${fmt(price)}</span><span>${fmt(price + 0.1)}</span><span class="positive">${qty}</span></div>`;
      }).join("");
      const levels = Array.from({ length: 10 }, (_, index) => {
        const offset = 5 - index;
        const price = base + offset * 0.1;
        const qty = Math.max(100, Math.round((vol / 1000) * (1 + Math.abs(offset) / 4)));
        return { price, qty, current: Math.abs(offset) < 0.1, pressure: offset === 0 };
      });
      const maxQty = Math.max(...levels.map(item => item.qty), 1);
      depth.innerHTML = levels.map(item => `
        <div class="depth-row ${item.current ? "current" : ""} ${item.pressure ? "pressure" : ""}">
          <span class="${pctClass(item.price - base)}">${fmt(item.price)}</span>
          <span class="depth-bar"><i class="depth-fill" style="width:${Math.max(8, item.qty / maxQty * 100)}%"></i></span>
          <span>${fmt(item.qty, 0)}</span>
        </div>
      `).join("");
    }
    function renderOrderRatio(row) {
      const target = document.getElementById("orderRatioBar");
      if (!target || !row) return;
      const pct = Math.max(-8, Math.min(8, Number(row.change_percent || 0)));
      const outer = Math.max(15, Math.min(85, 50 + pct * 4));
      const inner = 100 - outer;
      target.innerHTML = `
        <span>${fmt(inner)}%</span>
        <b><i style="width:${inner}%"></i></b>
        <span>${fmt(outer)}%</span>
      `;
      target.title = "內外盤比例目前為代理估算值；接入逐筆成交 API 後會改為真實內外盤。";
    }
    function renderAiMonitor(target, rows) {
      if (!rows.length) {
        target.innerHTML = `
          <thead><tr><th>狀態</th></tr></thead>
          <tbody><tr><td>目前沒有可盯盤的觀察名單。</td></tr></tbody>`;
        return;
      }
      target.innerHTML = `
        <thead><tr><th>代號</th><th>名稱</th><th>動作</th><th>AI</th><th>風險</th><th>技術</th><th>籌碼</th><th>消息</th><th>產業</th><th>法人</th><th>買點</th><th>賣點</th><th>停損</th><th>操作</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr>
            <td>${row.code}</td><td>${row.name}</td><td>${row.action}</td>
            <td>${fmt(row.score, 0)}</td><td>${fmt(row.risk, 0)}</td>
            <td title="${facetDetail(row, '技術面')}">${facetStance(row, '技術面')}</td>
            <td title="${facetDetail(row, '籌碼面')}">${facetStance(row, '籌碼面')}</td>
            <td title="${facetDetail(row, '消息面')}">${facetStance(row, '消息面')}</td>
            <td title="${facetDetail(row, '產業面')}">${facetStance(row, '產業面')}</td>
            <td title="${facetDetail(row, '三大法人')}">${facetStance(row, '三大法人')}</td>
            <td>${row.buy_zone}</td><td>${row.sell_zone}</td><td class="negative">${row.stop}</td>
            <td><button type="button" class="compact" onclick="openStock('${row.code}')">分析</button></td>
          </tr>`).join("")}</tbody>`;
    }
    function facetStance(row, name) {
      const item = (row.facets || []).find(facet => facet.name === name);
      return item ? item.stance : "無資料";
    }
    function facetDetail(row, name) {
      const item = (row.facets || []).find(facet => facet.name === name);
      return item ? item.detail : "";
    }
    function renderAnalysisFacets(target, monitor) {
      const facets = monitor.facets || [];
      if (!facets.length) {
        target.innerHTML = `<div class="trade-callout"><strong>資料不足</strong><p>尚無分析構面資料。</p></div>`;
        return;
      }
      target.innerHTML = `
        <div class="trade-kpis">
          ${facets.map(item => `
            <div class="trade-kpi" title="${item.detail}">
              <span>${item.name}</span>
              <strong>${item.stance}</strong>
            </div>
          `).join("")}
        </div>
        <ul class="trade-list">${facets.map(item => `<li><b>${item.name}：</b>${item.detail}</li>`).join("")}</ul>
      `;
    }
    function renderFundamentalResearch(target, data) {
      if (!data || data.error) {
        target.innerHTML = `<div class="fundamental-card"><strong>資料不足</strong><ul><li>${data ? data.error : "無資料"}</li></ul></div>`;
        return;
      }
      const yearlyRows = (data.yearly_stats || []).map(row => `
        <tr><td>${row.year}</td><td>${fmt(row.return)}%</td><td>${fmt(row.low)}-${fmt(row.high)}</td><td>${fmt(row.avg_volume, 0)}</td></tr>
      `).join("");
      const peerRows = (data.peer_cards || []).map(row => `
        <tr><td>${row.code} ${row.name}</td><td>${fmt(row.return_20d)}%</td></tr>
      `).join("");
      const summary = data.summary || {};
      const profile = data.market_profile || {};
      const strongest = summary.strongest || {};
      const weakest = summary.weakest || {};
      const grouped = groupResearchSections(data.sections || []);
      target.innerHTML = `
        <div class="research-command">
          <div class="research-verdict">
            <span>研究總結</span>
            <strong>${summary.action || data.verdict || "觀察"}</strong>
            <p>${data.code || ""} ${data.name || ""}｜綜合分數 ${fmt(summary.overall_score, 1)}｜結論 ${summary.verdict || data.verdict || "資料有限"}</p>
            <div class="research-score-row">
              <div class="research-pill"><b>${fmt(summary.overall_score, 1)}</b><small>總分</small></div>
              <div class="research-pill"><b>${strongest.label || "-"}</b><small>最強 ${fmt(strongest.score, 1)}</small></div>
              <div class="research-pill"><b>${weakest.label || "-"}</b><small>待補 ${fmt(weakest.score, 1)}</small></div>
            </div>
          </div>
        </div>
        <div class="fundamental-visuals">
          <div class="fundamental-visual">
            <h3>分析雷達圖</h3>
            <div class="radar-stage">
              <svg id="fundamentalRadarChart" class="chart" role="img" aria-label="分析雷達圖"></svg>
              <div class="research-bars">
                <span>雷達圖重點摘要</span>
                <div class="research-pill"><b>${strongest.label || "-"}</b><small>最強構面 ${fmt(strongest.score, 1)} 分</small></div>
                <div class="research-pill"><b>${weakest.label || "-"}</b><small>需追蹤構面 ${fmt(weakest.score, 1)} 分</small></div>
                <div class="research-pill"><b>${fmt(summary.data_quality, 0)}</b><small>資料覆蓋分數</small></div>
                <div class="research-list-grid">
                  <ul>${(summary.positives || []).map(item => `<li>${item}</li>`).join("")}</ul>
                  <ul>${(summary.risk_flags || []).map(item => `<li>${item}</li>`).join("")}</ul>
                </div>
              </div>
            </div>
          </div>
          <div class="fundamental-visual trend-wide">
            <h3>近一年指數化走勢</h3>
            <svg id="fundamentalTrendChart" class="chart" role="img" aria-label="近一年指數化走勢"></svg>
            <table class="fundamental-mini-table">
              <thead><tr><th>年度</th><th>報酬</th><th>高低區間</th><th>均量</th></tr></thead>
              <tbody>${yearlyRows || "<tr><td colspan='4'>年度資料不足</td></tr>"}</tbody>
            </table>
          </div>
        </div>
        <div class="fundamental-grid">
          <div class="fundamental-card">
            <span>同業前段班</span>
            <strong>${data.industry || "未分類"}</strong>
            <table class="fundamental-mini-table">
              <thead><tr><th>個股</th><th>20日%</th></tr></thead>
              <tbody>${peerRows || "<tr><td colspan='2'>同業資料不足</td></tr>"}</tbody>
            </table>
          </div>
          <div class="fundamental-card">
            <span>市場代理資料</span>
            <strong>${profile.chip_label || "籌碼中性"}</strong>
            <ul>
              <li>流動性：${profile.liquidity_label || "-"}｜20日均量 ${fmt(profile.avg_volume_20, 0)}</li>
              <li>上漲量占比：${fmt(profile.up_volume_ratio)}%</li>
              <li>OBV 斜率：${profile.obv_slope_label || "-"}</li>
              <li>估值熱度：${fmt(profile.valuation_heat, 1)} / 100</li>
            </ul>
          </div>
          <div class="fundamental-card">
            <span>下一步追蹤</span>
            <strong>條件確認</strong>
            <ul>${(summary.checkpoints || []).map(item => `<li>${item}</li>`).join("")}</ul>
          </div>
          <div class="fundamental-card">
            <span>資料覆蓋</span>
            <strong>完整度 ${fmt(summary.data_quality, 0)}</strong>
            <ul>${(summary.data_gaps || []).map(item => `<li>${item}</li>`).join("")}</ul>
          </div>
          ${grouped.map(group => `
            <div class="fundamental-card">
              <span>${group.label}</span>
              <strong>${group.title}</strong>
              <ul>${group.points.slice(0, 5).map(point => `<li>${point}</li>`).join("")}</ul>
            </div>
          `).join("")}
        </div>
      `;
      renderRadarChart(document.getElementById("fundamentalRadarChart"), data.radar || []);
      renderLineChart(document.getElementById("fundamentalTrendChart"), data.trend_series || [], "indexed");
    }
    function groupResearchSections(sections) {
      const buckets = [
        { label: "公司與產業", title: "商業模式 / 產業 / 護城河", names: ["商業模式與收入來源", "競爭護城河", "產業趨勢"], points: [] },
        { label: "市場與籌碼", title: "價格品質 / 同業 / 籌碼", names: ["價格品質與波動", "同業比較", "籌碼與資金代理"], points: [] },
        { label: "估值與財務", title: "估值熱度 / 財務代理", names: ["五年財務健康", "估值分析"], points: [] },
        { label: "事件與情境", title: "消息 / 多空 / 成長 / 結論", names: ["消息事件偵測", "多空與基本情境", "未來 12-24 個月與 5-10 年成長", "是否應該投資"], points: [] },
      ];
      sections.forEach(section => {
        const bucket = buckets.find(item => item.names.includes(section.title));
        if (!bucket) return;
        bucket.points.push(`${section.title}：${section.stance}`);
        (section.points || []).slice(0, 2).forEach(point => bucket.points.push(point));
      });
      return buckets.filter(item => item.points.length);
    }
    function renderRadarChart(target, rows) {
      const width = 620;
      const height = 460;
      const cx = width / 2;
      const cy = 222;
      const radius = 158;
      target.setAttribute("viewBox", `0 0 ${width} ${height}`);
      if (!rows || rows.length < 3) {
        target.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">資料不足</text>`;
        return;
      }
      const pointAt = (index, value) => {
        const angle = -Math.PI / 2 + index * Math.PI * 2 / rows.length;
        const r = radius * (Number(value) / 100);
        return { x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r };
      };
      const ring = level => rows.map((_, index) => {
        const point = pointAt(index, level);
        return `${point.x},${point.y}`;
      }).join(" ");
      const avg = rows.reduce((sum, row) => sum + Number(row.score || 0), 0) / rows.length;
      const axes = rows.map((row, index) => {
        const end = pointAt(index, 100);
        const label = pointAt(index, 122);
        return `
          <line x1="${cx}" y1="${cy}" x2="${end.x}" y2="${end.y}" stroke="#1e3a5f" stroke-width="1"></line>
          <text x="${label.x}" y="${label.y}" text-anchor="middle" fill="#c7e7ff" font-size="18" font-weight="800">${row.label}</text>
        `;
      }).join("");
      const area = rows.map((row, index) => {
        const point = pointAt(index, row.score);
        return `${point.x},${point.y}`;
      }).join(" ");
      const dots = rows.map((row, index) => {
        const point = pointAt(index, row.score);
        const label = pointAt(index, Math.max(24, Number(row.score || 0) - 12));
        return `
          <circle cx="${point.x}" cy="${point.y}" r="5" fill="#7dd3fc" stroke="#0f172a" stroke-width="2">
            <title>${row.label} ${fmt(row.score, 1)}</title>
          </circle>
          <text x="${label.x}" y="${label.y + 5}" text-anchor="middle" fill="#ffffff" font-size="15" font-weight="900">${fmt(row.score, 0)}</text>
        `;
      }).join("");
      target.innerHTML = `
        <defs>
          <radialGradient id="radarGlow" cx="50%" cy="50%" r="55%">
            <stop offset="0%" stop-color="#38bdf8" stop-opacity=".38"></stop>
            <stop offset="62%" stop-color="#22c55e" stop-opacity=".18"></stop>
            <stop offset="100%" stop-color="#a855f7" stop-opacity=".08"></stop>
          </radialGradient>
          <filter id="softGlow">
            <feGaussianBlur stdDeviation="3" result="blur"></feGaussianBlur>
            <feMerge><feMergeNode in="blur"></feMergeNode><feMergeNode in="SourceGraphic"></feMergeNode></feMerge>
          </filter>
        </defs>
        <circle cx="${cx}" cy="${cy}" r="${radius + 18}" fill="#020617" opacity=".18"></circle>
        <polygon points="${ring(100)}" fill="none" stroke="#38bdf8" stroke-width="1.2" opacity=".55"></polygon>
        <polygon points="${ring(75)}" fill="none" stroke="#1e3a5f" stroke-width="1"></polygon>
        <polygon points="${ring(50)}" fill="none" stroke="#1e3a5f" stroke-width="1" opacity=".8"></polygon>
        <polygon points="${ring(25)}" fill="none" stroke="#1e3a5f" stroke-width="1" opacity=".55"></polygon>
        ${axes}
        <polygon points="${area}" fill="url(#radarGlow)" stroke="#7dd3fc" stroke-width="2.4" filter="url(#softGlow)"></polygon>
        ${dots}
        <circle cx="${cx}" cy="${cy}" r="34" fill="#08111f" stroke="#38bdf8" stroke-width="1.4" opacity=".96"></circle>
        <text x="${cx}" y="${cy - 4}" text-anchor="middle" fill="#ffffff" font-size="30" font-weight="900">${fmt(avg, 0)}</text>
        <text x="${cx}" y="${cy + 22}" text-anchor="middle" fill="#94a3b8" font-size="14" font-weight="800">平均</text>
        <text x="${cx}" y="${cy + radius + 58}" text-anchor="middle" fill="#94a3b8" font-size="15" font-weight="800">0-100 分，越外圈越強</text>
      `;
    }
    function renderNews(target, data) {
      if (!data || data.error || !(data.items || []).length) {
        target.innerHTML = `<div class="news-card"><span>新聞掃描</span><a href="#">${data ? (data.message || data.error || "目前沒有新聞") : "目前沒有新聞"}</a><small>若來源暫時無法連線，系統會保留量價事件偵測作為消息代理。</small></div>`;
        return;
      }
      target.innerHTML = (data.items || []).map(item => `
        <div class="news-card">
          <span>${item.sentiment || "中性"}｜${item.source || "新聞"}</span>
          <a href="${item.link}" target="_blank" rel="noopener">${item.title}</a>
          <small>${item.published || ""}</small>
        </div>
      `).join("");
    }
    function renderStrategyTrades(target, rows) {
      target.innerHTML = `
        <thead><tr><th>建倉</th><th>了結</th><th>代號</th><th>名稱</th><th>建倉價</th><th>出倉價</th><th>分數</th><th>報酬%</th><th>資金</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr>
            <td>${row.entry_date}</td><td>${row.exit_date}</td><td>${row.code}</td><td>${row.name}</td>
            <td>${fmt(row.entry_price)}</td><td>${fmt(row.exit_price)}</td>
            <td>${row.score}</td><td class="${pctClass(row.return)}">${fmt(row.return)}</td><td>${fmt(row.capital)}</td>
          </tr>`).join("")}</tbody>`;
    }
    function renderStrategyEntries(target, rows) {
      target.innerHTML = `
        <thead><tr><th>建倉日</th><th>預計出場</th><th>代號</th><th>名稱</th><th>建倉位</th><th>預估出倉位</th><th>分數</th><th>配置資金</th></tr></thead>
        <tbody>${(rows || []).map(row => `
          <tr>
            <td>${row.entry_date}</td><td>${row.exit_date}</td><td>${row.code}</td><td>${row.name}</td>
            <td>${fmt(row.entry_price)}</td><td>${fmt(row.exit_price)}</td><td>${row.score}</td><td>${fmt(row.entry_value)}</td>
          </tr>`).join("") || "<tr><td colspan='8'>目前沒有建倉紀錄。</td></tr>"}</tbody>`;
    }
    function renderStrategyOpen(target, rows) {
      target.innerHTML = `
        <thead><tr><th>建倉日</th><th>預計出場</th><th>代號</th><th>名稱</th><th>建倉位</th><th>預估出倉位</th><th>目前預估%</th><th>資金</th></tr></thead>
        <tbody>${(rows || []).map(row => `
          <tr>
            <td>${row.entry_date}</td><td>${row.exit_date}</td><td>${row.code}</td><td>${row.name}</td>
            <td>${fmt(row.entry_price)}</td><td>${fmt(row.exit_price)}</td><td class="${pctClass(row.return)}">${fmt(row.return)}</td><td>${fmt(row.capital)}</td>
          </tr>`).join("") || "<tr><td colspan='8'>目前沒有未平倉部位。</td></tr>"}</tbody>`;
    }
    function renderHighWinStrategy(panel, table, data) {
      const win = Number(data.win_rate || 0);
      const verdict = win >= 80 ? "已達 80% 勝率門檻" : "尚未達 80%，不硬做漂亮數字";
      const next = win >= 80
        ? "目前條件可列為高勝率候選，但仍需看最大虧損與交易次數。"
        : "要接近 80%，下一步需要加入大盤濾網、產業強弱、三大法人與新聞事件，並接受交易次數下降。";
      panel.innerHTML = `
        <div class="strategy-advice">
          <div class="advice-main">
            <strong>${data.name || "高勝率保守模式"}</strong>
            <p>${verdict}</p>
            <p>${next}</p>
            <p><b>交易次數：</b>${fmt(data.trades, 0)} 筆｜<b>勝率：</b>${fmt(data.win_rate)}%｜<b>平均報酬：</b>${fmt(data.avg_return)}%</p>
            <ul class="advice-list">${(data.rules || []).map(item => `<li>${item}</li>`).join("")}</ul>
          </div>
          <div class="strategy-kpis">
            <div class="strategy-kpi"><span>勝率</span><strong>${fmt(data.win_rate)}%</strong></div>
            <div class="strategy-kpi"><span>平均報酬</span><strong>${fmt(data.avg_return)}%</strong></div>
            <div class="strategy-kpi"><span>中位數</span><strong>${fmt(data.median_return)}%</strong></div>
            <div class="strategy-kpi"><span>最差單筆</span><strong>${fmt(data.worst_return)}%</strong></div>
          </div>
        </div>`;
      renderStrategyTrades(table, data.recent_trades || []);
    }
    function renderStrategyLeaders(target, rows) {
      target.innerHTML = `
        <thead><tr><th>代號</th><th>名稱</th><th>分數</th><th>訊號</th><th>收盤</th><th>20日%</th><th>回撤</th><th>建倉位</th><th>出倉位</th><th>停損</th><th>操作</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr>
            <td>${row.code}</td><td>${row.name}</td><td>${fmt(row.score, 0)}</td><td>${row.signal}</td>
            <td>${fmt(row.close)}</td><td class="${pctClass(row.return_20d)}">${fmt(row.return_20d)}</td>
            <td class="${row.drawdown_alert ? "negative" : ""}" title="${row.drawdown_alert || "未達提醒線"}">${fmt(row.drawdown_pct)}%</td>
            <td>${row.entry_zone || "-"}</td><td>${row.exit_zone || "-"}</td><td class="negative">${row.stop || "-"}</td>
            <td><button type="button" class="compact" onclick="openStock('${row.code}')">分析</button></td>
          </tr>`).join("")}</tbody>`;
    }
    function renderStrategyStockCards(target, rows) {
      const cards = (rows || []).slice(0, 8).map(row => {
        const heat = row.rsi_14 >= 70 ? "偏熱，避免追高" : row.rsi_14 >= 55 ? "動能健康" : "等待轉強";
        const trend = row.above_sma20 && row.above_sma60 ? "月線與季線之上" : row.above_sma20 ? "站上月線" : "趨勢待確認";
        return `
          <div class="strategy-stock-card">
            <div class="code">${row.code} · ${row.signal}</div>
            <strong>${row.name}</strong>
            <span>收盤 ${fmt(row.close)}｜20日 ${fmt(row.return_20d)}%｜60日 ${fmt(row.return_60d)}%</span>
            <span>建倉 ${row.entry_zone || "-"}｜出倉 ${row.exit_zone || "-"}｜停損 ${row.stop || "-"}</span>
            <span>RSI ${fmt(row.rsi_14)}｜量比 ${fmt(row.volume_ratio)}｜${trend}</span>
            <span>${heat}${row.new_high_60 ? "｜60日新高" : ""}</span>
            <button type="button" class="compact" onclick="openStock('${row.code}')">進入個股分析</button>
          </div>
        `;
      }).join("");
      target.innerHTML = `<div class="strategy-stock-grid">${cards || "<div class='note'>目前沒有可顯示的 AI 實操個股。</div>"}</div>`;
    }
    function renderStrategyAdvice(target, context) {
      const m = context.metrics || {};
      target.innerHTML = `
        <div class="strategy-advice">
          <div class="advice-main">
            <strong>${context.stance || "資料不足"}</strong>
            <p>${context.headline || ""}</p>
            <p><b>部位建議：</b>${context.position_suggestion || "等待資料更新"}</p>
            <ul class="advice-list">${(context.actions || []).map(item => `<li>${item}</li>`).join("")}</ul>
          </div>
          <div class="strategy-kpis">
            <div class="strategy-kpi"><span>站上月線</span><strong>${fmt(m.above_sma20_pct)}%</strong></div>
            <div class="strategy-kpi"><span>站上季線</span><strong>${fmt(m.above_sma60_pct)}%</strong></div>
            <div class="strategy-kpi"><span>60日新高</span><strong>${fmt(m.new_high_60, 0)} 檔</strong></div>
            <div class="strategy-kpi"><span>可行訊號</span><strong>${fmt(m.actionable_pct)}%</strong></div>
            <div class="strategy-kpi"><span>平均 RSI</span><strong>${fmt(m.avg_rsi)}</strong></div>
            <div class="strategy-kpi"><span>20日平均</span><strong>${fmt(m.avg_return_20d)}%</strong></div>
          </div>
        </div>
        <div class="content" style="padding:14px 0 0">
          <b>風險提示</b>
          <ul class="advice-list">${(context.risks || []).map(item => `<li>${item}</li>`).join("")}</ul>
        </div>`;
    }
    function renderCurve(target, rows) {
      const recent = rows.slice(-12);
      target.innerHTML = `
        <thead><tr><th>日期</th><th>資金</th></tr></thead>
        <tbody>${recent.map(row => `
          <tr><td>${row.date}</td><td>${fmt(row.capital)}</td></tr>`).join("")}</tbody>`;
    }
    function renderLineChart(target, rows, valueKey) {
      const width = 1000;
      const height = 260;
      const pad = { left: 54, right: 72, top: 18, bottom: 34 };
      target.setAttribute("viewBox", `0 0 ${width} ${height}`);
      if (!rows || rows.length < 2) {
        target.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">資料不足</text>`;
        return;
      }
      const values = rows.map(row => Number(row[valueKey])).filter(value => Number.isFinite(value));
      const min = Math.min(...values);
      const max = Math.max(...values);
      const span = max - min || 1;
      const x = index => pad.left + index * ((width - pad.left - pad.right) / (rows.length - 1));
      const y = value => pad.top + (max - value) * ((height - pad.top - pad.bottom) / span);
      const points = rows.map((row, index) => `${x(index)},${y(Number(row[valueKey]))}`).join(" ");
      const area = `${pad.left},${height - pad.bottom} ${points} ${width - pad.right},${height - pad.bottom}`;
      target.innerHTML = `
        <line class="chart-axis" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"></line>
        <line class="chart-axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>
        <polygon class="chart-area" points="${area}"></polygon>
        <polyline class="chart-line" points="${points}"></polyline>
        <text class="chart-label" x="${pad.left}" y="${height - 10}">${rows[0].date || ""}</text>
        <text class="chart-label" x="${width - pad.right}" y="${height - 10}" text-anchor="end">${rows[rows.length - 1].date || ""}</text>
        <text class="chart-label" x="8" y="${pad.top + 8}">${fmt(max)}</text>
        <text class="chart-label" x="8" y="${height - pad.bottom}">${fmt(min)}</text>
      `;
    }
    function renderRealtimeTrend(data) {
      const target = document.getElementById("realtimeTrendChart");
      const summary = document.getElementById("realtimeTrendSummary");
      const notice = document.getElementById("realtimeTrendNotice");
      const rows = (data.rows || []).filter(row => Number.isFinite(Number(row.close)));
      if (!rows.length) {
        target.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">資料不足</text>`;
        summary.innerHTML = "";
        notice.textContent = data.error || "目前沒有可用走勢資料。";
        return;
      }
      const first = rows[0];
      const latest = rows[rows.length - 1];
      const change = Number(latest.close) - Number(first.close);
      const changePct = Number(first.close) ? change / Number(first.close) * 100 : null;
      const high = Math.max(...rows.map(row => Number(row.high ?? row.close)).filter(Number.isFinite));
      const low = Math.min(...rows.map(row => Number(row.low ?? row.close)).filter(Number.isFinite));
      summary.innerHTML = `
        <div class="trend-kpi"><span>最新</span><strong>${fmt(latest.close)}</strong></div>
        <div class="trend-kpi"><span>區間漲跌</span><strong class="${pctClass(changePct)}">${fmt(change)} / ${fmt(changePct)}%</strong></div>
        <div class="trend-kpi"><span>高低區間</span><strong>${fmt(low)} - ${fmt(high)}</strong></div>
      `;
      renderRealtimeTrendChart(target, rows);
      const head = document.getElementById("realtimeTerminalHead");
      if (head) {
        head.innerHTML = `${data.code || ""} ${data.name || ""} <span class="${pctClass(changePct)}" style="margin-left:18px">${fmt(latest.close)} ${fmt(change)}(${fmt(changePct)}%)</span> <span style="float:right;color:#94a3b8">${trendLabel(latest)}</span>`;
      }
      renderOrderRatio({ change_percent: changePct });
      notice.textContent = `${data.code || ""} ${data.name || ""}｜${data.message || "走勢已更新"}`;
    }
    function renderRealtimeTrendChart(target, rows) {
      const width = 1000;
      const height = 320;
      const pad = { left: 58, right: 74, top: 20, bottom: 38 };
      const volumeTop = 244;
      const volumeBottom = height - pad.bottom;
      target.setAttribute("viewBox", `0 0 ${width} ${height}`);
      if (!rows || rows.length < 2) {
        target.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">資料不足</text>`;
        return;
      }
      const closes = rows.map(row => Number(row.close));
      const highs = rows.map(row => Number(row.high ?? row.close));
      const lows = rows.map(row => Number(row.low ?? row.close));
      const volumes = rows.map(row => Number(row.volume || 0));
      const max = Math.max(...highs);
      const min = Math.min(...lows);
      const span = max - min || 1;
      const plotW = width - pad.left - pad.right;
      const plotH = volumeTop - pad.top - 12;
      const x = index => pad.left + index * (plotW / (rows.length - 1));
      const y = value => pad.top + (max - value) * (plotH / span);
      const points = closes.map((value, index) => `${x(index)},${y(value)}`).join(" ");
      const area = `${pad.left},${volumeTop - 12} ${points} ${width - pad.right},${volumeTop - 12}`;
      const volumeMax = Math.max(...volumes, 1);
      const barW = Math.max(2, Math.min(10, plotW / rows.length * .55));
      const volumeBars = volumes.map((value, index) => {
        const h = Math.max(1, value / volumeMax * (volumeBottom - volumeTop));
        return `<rect x="${x(index) - barW / 2}" y="${volumeBottom - h}" width="${barW}" height="${h}" fill="#38bdf8" opacity=".26"></rect>`;
      }).join("");
      const latest = closes[closes.length - 1];
      const latestY = y(latest);
      target.innerHTML = `
        <line class="chart-axis" x1="${pad.left}" y1="${volumeTop - 12}" x2="${width - pad.right}" y2="${volumeTop - 12}"></line>
        <line class="chart-axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${volumeBottom}"></line>
        <line class="chart-axis" x1="${pad.left}" y1="${volumeBottom}" x2="${width - pad.right}" y2="${volumeBottom}"></line>
        <polygon class="chart-area" points="${area}"></polygon>
        <polyline class="chart-line" points="${points}"></polyline>
        <line x1="${pad.left}" y1="${latestY}" x2="${width - pad.right}" y2="${latestY}" stroke="#38bdf8" stroke-dasharray="4 5" opacity=".62"></line>
        ${volumeBars}
        <text class="chart-label" x="8" y="${pad.top + 8}">${fmt(max)}</text>
        <text class="chart-label" x="8" y="${volumeTop - 12}">${fmt(min)}</text>
        <text class="chart-label" x="${width - pad.right + 8}" y="${latestY + 4}">${fmt(latest)}</text>
        <text class="chart-label" x="${pad.left}" y="${height - 10}">${trendLabel(rows[0])}</text>
        <text class="chart-label" x="${width - pad.right}" y="${height - 10}" text-anchor="end">${trendLabel(rows[rows.length - 1])}</text>
        <text class="chart-label" x="${pad.left}" y="${volumeTop + 12}">量</text>
      `;
    }
    function trendLabel(row) {
      return (row && (row.label || row.time || row.date)) || "";
    }
    function smaValues(values, window) {
      return values.map((_, index) => {
        if (index + 1 < window) return null;
        const slice = values.slice(index + 1 - window, index + 1);
        return slice.reduce((a, b) => a + b, 0) / window;
      });
    }
    function stdValues(values, window) {
      return values.map((_, index) => {
        if (index + 1 < window) return null;
        const slice = values.slice(index + 1 - window, index + 1);
        const avg = slice.reduce((a, b) => a + b, 0) / window;
        const variance = slice.reduce((sum, value) => sum + Math.pow(value - avg, 2), 0) / window;
        return Math.sqrt(variance);
      });
    }
    function emaValues(values, window) {
      if (!values.length) return [];
      const multiplier = 2 / (window + 1);
      const out = [values[0]];
      for (let i = 1; i < values.length; i++) {
        out.push((values[i] - out[i - 1]) * multiplier + out[i - 1]);
      }
      return out;
    }
    function macdValues(values) {
      const ema12 = emaValues(values, 12);
      const ema26 = emaValues(values, 26);
      const macd = values.map((_, index) => ema12[index] - ema26[index]);
      const signal = emaValues(macd, 9);
      const hist = macd.map((value, index) => value - signal[index]);
      return { macd, signal, hist };
    }
    function kdValues(rows, window = 9) {
      const out = [];
      let k = 50;
      let d = 50;
      rows.forEach((row, index) => {
        if (index + 1 < window) {
          out.push({ k: null, d: null });
          return;
        }
        const slice = rows.slice(index + 1 - window, index + 1);
        const high = Math.max(...slice.map(item => Number(item.high)));
        const low = Math.min(...slice.map(item => Number(item.low)));
        const close = Number(row.close);
        const rsv = high === low ? 50 : ((close - low) / (high - low)) * 100;
        k = k * 2 / 3 + rsv / 3;
        d = d * 2 / 3 + k / 3;
        out.push({ k, d });
      });
      return out;
    }
    function rsiValues(values, window = 14) {
      return values.map((_, index) => {
        if (index < window) return null;
        let gains = 0;
        let losses = 0;
        for (let i = index - window + 1; i <= index; i++) {
          const diff = values[i] - values[i - 1];
          if (diff >= 0) gains += diff;
          else losses += Math.abs(diff);
        }
        if (losses === 0) return 100;
        const rs = gains / losses;
        return 100 - 100 / (1 + rs);
      });
    }
    let currentPriceRows = [];
    let chartWindowStart = 0;
    let chartWindowSize = 80;
    let currentChartMode = "all";
    let currentStockCode = "2330";
    let currentStockInterval = "1d";
    const chartModeCaptions = {
      all: "K 線 / 成交量 / MA5 / MA10 / 月線 / 布林通道",
      ma: "均線模式：K 線 / MA5 / MA10 / 月線",
      bollinger: "布林模式：K 線 / 布林上軌 / 布林下軌",
    };
    let currentInstitutional = null;
    let currentChipMode = "foreign";
    function visibleChartRows() {
      if (!currentPriceRows.length) return [];
      const size = Math.min(chartWindowSize, currentPriceRows.length);
      const maxStart = Math.max(0, currentPriceRows.length - size);
      chartWindowStart = Math.max(0, Math.min(chartWindowStart, maxStart));
      return currentPriceRows.slice(chartWindowStart, chartWindowStart + size);
    }
    function resetChartWindow() {
      const size = Math.min(chartWindowSize, currentPriceRows.length);
      chartWindowStart = Math.max(0, currentPriceRows.length - size);
    }
    function renderChartSuite() {
      const rows = visibleChartRows();
      renderCandlestickChart(document.getElementById("priceChart"), rows, currentChartMode);
      renderVolumeChart(document.getElementById("volumeChart"), rows);
      renderRsiChart(document.getElementById("rsiChart"), rows, currentPriceRows, chartWindowStart);
      renderMacdChart(document.getElementById("macdChart"), rows, currentPriceRows, chartWindowStart);
      renderKdChart(document.getElementById("kdChart"), rows, currentPriceRows, chartWindowStart);
      renderInstitutionalChart(document.getElementById("institutionalChart"), currentInstitutional || { rows: [] }, currentChipMode);
      setSyncedCursor(rows.length - 1, rows.length);
    }
    function setChartMode(mode, button) {
      currentChartMode = mode;
      document.querySelectorAll("[data-chart-mode]").forEach(tab => tab.classList.remove("active"));
      if (button) button.classList.add("active");
      const caption = document.getElementById("chartCaption");
      if (caption) caption.textContent = chartModeCaptions[mode] || chartModeCaptions.all;
      renderChartSuite();
    }
    async function setStockInterval(interval, button) {
      currentStockInterval = interval;
      document.querySelectorAll("[data-interval]").forEach(tab => tab.classList.remove("active"));
      if (button) button.classList.add("active");
      await loadStockChartInterval(currentStockCode, interval);
    }
    function intervalLabel(interval) {
      return { "1d": "日K", "1wk": "週K", "1mo": "月K", "30m": "30分" }[interval] || interval;
    }
    async function loadStockChartInterval(code, interval) {
      if (interval === "1d" || interval === "1wk" || interval === "1mo") {
        const prices = await getJson(`/api/prices?code=${encodeURIComponent(code)}&limit=160`);
        const dailyRows = prices.prices || [];
        currentPriceRows = interval === "1wk" ? aggregateDailyRows(dailyRows, "week") : interval === "1mo" ? aggregateDailyRows(dailyRows, "month") : dailyRows;
      } else {
        const trend = await getJson(`/api/realtime-trend?code=${encodeURIComponent(code)}&interval=${encodeURIComponent(interval)}`);
        currentPriceRows = trend.rows || [];
      }
      resetChartWindow();
      renderChartSuite();
      const info = document.getElementById("chartHoverInfo");
      if (info) info.textContent = `${intervalLabel(interval)}｜共 ${currentPriceRows.length} 根 K 棒，目前顯示 ${visibleChartRows().length} 根。`;
    }
    function aggregateDailyRows(rows, mode) {
      const groups = new Map();
      rows.forEach(row => {
        const d = new Date(`${row.date}T00:00:00`);
        const key = mode === "month"
          ? row.date.slice(0, 7)
          : `${d.getFullYear()}-W${String(Math.ceil((((d - new Date(d.getFullYear(),0,1)) / 86400000) + new Date(d.getFullYear(),0,1).getDay() + 1) / 7)).padStart(2, "0")}`;
        if (!groups.has(key)) {
          groups.set(key, { date: row.date, label: key, open: row.open, high: row.high, low: row.low, close: row.close, volume: row.volume || 0 });
        } else {
          const item = groups.get(key);
          item.high = Math.max(Number(item.high), Number(row.high));
          item.low = Math.min(Number(item.low), Number(row.low));
          item.close = row.close;
          item.volume = (item.volume || 0) + (row.volume || 0);
          item.date = row.date;
        }
      });
      return [...groups.values()];
    }
    function toggleChartZoom(button) {
      const panel = button.closest(".mini-chart-panel");
      if (!panel) return;
      panel.classList.toggle("expanded");
      button.textContent = panel.classList.contains("expanded") ? "縮小" : "放大";
    }
    function showTechnicalAttachment(mode) {
      const strip = document.getElementById("technicalStrip");
      const panels = document.querySelectorAll("[data-tech-panel]");
      if (!strip) return;
      if (mode === "none") {
        strip.classList.add("collapsed");
        panels.forEach(panel => {
          panel.classList.remove("hidden", "expanded");
          const btn = panel.querySelector(".zoom-btn");
          if (btn) btn.textContent = "放大";
        });
        return;
      }
      strip.classList.remove("collapsed");
      panels.forEach(panel => {
        const shouldShow = mode === "all" || panel.dataset.techPanel === mode;
        panel.classList.toggle("hidden", !shouldShow);
        const isSingle = mode !== "all" && shouldShow;
        panel.classList.toggle("expanded", isSingle);
        const btn = panel.querySelector(".zoom-btn");
        if (btn) btn.textContent = isSingle ? "縮小" : "放大";
      });
    }
    function showChipAttachment(mode) {
      currentChipMode = mode;
      showTechnicalAttachment("chips");
      renderInstitutionalChart(document.getElementById("institutionalChart"), currentInstitutional || { rows: [] }, mode);
    }
    function alignInstitutionalRows(data, priceRows) {
      const source = data.rows || [];
      if (!source.length || !priceRows.length) return [];
      const byDate = new Map(source.map(row => [row.date, row]));
      return priceRows.map(row => {
        const date = row.date || (row.time || "").slice(0, 10);
        const matched = byDate.get(date);
        return {
          date,
          label: row.label || row.time || row.date,
          foreign: matched ? Number(matched.foreign || 0) : null,
          investment: matched ? Number(matched.investment || 0) : null,
          retail_proxy: matched ? Number(matched.retail_proxy || 0) : null,
          hasInstitutional: Boolean(matched),
        };
      });
    }
    function installChartDrag() {
      const stack = document.getElementById("chartStack");
      if (!stack || stack.dataset.dragReady) return;
      stack.dataset.dragReady = "1";
      let dragging = false;
      let startX = 0;
      let startWindow = 0;
      let lastStep = 0;
      stack.addEventListener("pointerdown", event => {
        dragging = true;
        startX = event.clientX;
        startWindow = chartWindowStart;
        lastStep = 0;
        stack.classList.add("dragging");
        stack.setPointerCapture(event.pointerId);
      });
      stack.addEventListener("pointermove", event => {
        if (!dragging) return;
        const step = Math.round((startX - event.clientX) / 12);
        if (step === lastStep) return;
        lastStep = step;
        const size = Math.min(chartWindowSize, currentPriceRows.length);
        const maxStart = Math.max(0, currentPriceRows.length - size);
        chartWindowStart = Math.max(0, Math.min(startWindow + step, maxStart));
        renderChartSuite();
      });
      stack.addEventListener("wheel", event => {
        if (!currentPriceRows.length) return;
        event.preventDefault();
        const size = Math.min(chartWindowSize, currentPriceRows.length);
        const maxStart = Math.max(0, currentPriceRows.length - size);
        const delta = event.deltaY || event.deltaX;
        chartWindowStart = Math.max(0, Math.min(chartWindowStart + Math.sign(delta) * 4, maxStart));
        renderChartSuite();
      }, { passive: false });
      const stop = event => {
        dragging = false;
        stack.classList.remove("dragging");
        if (event.pointerId !== undefined && stack.hasPointerCapture(event.pointerId)) stack.releasePointerCapture(event.pointerId);
      };
      stack.addEventListener("pointerup", stop);
      stack.addEventListener("pointercancel", stop);
      stack.addEventListener("pointerleave", stop);
    }
    function buildTradePlan(stock, ind, signal, rows) {
      if (!rows || !rows.length) return null;
      const latest = rows[rows.length - 1];
      const close = Number(latest.close);
      const recent = rows.slice(-20);
      const recentHigh = Math.max(...recent.map(row => Number(row.high)));
      const recentLow = Math.min(...recent.map(row => Number(row.low)));
      const supports = [recentLow, ind.sma_20, ind.sma_60].filter(value => Number.isFinite(Number(value)) && Number(value) <= close).map(Number);
      const resistances = [recentHigh, ind.sma_20, ind.sma_60].filter(value => Number.isFinite(Number(value)) && Number(value) >= close).map(Number);
      const support = supports.length ? Math.max(...supports) : recentLow;
      const resistance = resistances.length ? Math.min(...resistances) : recentHigh;
      const stop = support * 0.97;
      const buyLow = support;
      const buyHigh = support * 1.03;
      const breakout = resistance * 1.01;
      const rsi = Number(ind.rsi_14);
      const trendUp = Number(ind.sma_20) > Number(ind.sma_60) && close > Number(ind.sma_20);
      const momentum = Number(ind.return_20d);
      let action = "觀察等待";
      let tone = "價格需要更靠近支撐或放量突破壓力後，再提高進場把握。";
      if (trendUp && rsi < 68 && close <= buyHigh) {
        action = "分批偏多";
        tone = "趨勢站在月線之上且尚未明顯過熱，可用小部位靠近支撐分批研究。";
      } else if (trendUp && close > resistance && rsi < 75) {
        action = "突破追蹤";
        tone = "價格正在挑戰近期壓力，適合等收盤確認與量能配合，不宜一次追滿。";
      } else if (rsi >= 72) {
        action = "高檔控管";
        tone = "RSI 偏熱，優先保護獲利與等待拉回，追價風險較高。";
      } else if (close < Number(ind.sma_20) || momentum < -5) {
        action = "防守觀察";
        tone = "短線動能偏弱，先看是否守住支撐，跌破時不要硬攤平。";
      }
      return {
        action,
        tone,
        close,
        support,
        resistance,
        buyLow,
        buyHigh,
        stop,
        breakout,
        latestDate: latest.date,
        checks: [
          `買點區：${fmt(buyLow)} - ${fmt(buyHigh)}，越靠近支撐越有風險報酬比。`,
          `突破確認：收盤站上 ${fmt(breakout)} 且量能放大，再視為強勢續航。`,
          `風控線：跌破 ${fmt(stop)} 代表結構轉弱，需降低部位或退出觀察。`,
        ],
      };
    }
    function renderTradePlan(target, plan) {
      if (!plan) {
        target.innerHTML = `<div class="trade-callout"><strong>資料不足</strong><p>需要更多 K 線資料才能產生交易計畫。</p></div>`;
        return;
      }
      target.innerHTML = `
        <div class="trade-callout">
          <span>${plan.latestDate || "最新資料"}</span>
          <strong>${plan.action}</strong>
          <p>${plan.tone}</p>
        </div>
        <div class="trade-kpis">
          <div class="trade-kpi"><span>最新價</span><strong>${fmt(plan.close)}</strong></div>
          <div class="trade-kpi"><span>支撐</span><strong>${fmt(plan.support)}</strong></div>
          <div class="trade-kpi"><span>壓力</span><strong>${fmt(plan.resistance)}</strong></div>
          <div class="trade-kpi"><span>停損</span><strong class="negative">${fmt(plan.stop)}</strong></div>
        </div>
        <ul class="trade-list">${plan.checks.map(item => `<li>${item}</li>`).join("")}</ul>
      `;
    }
    function showCandleInfo(index) {
      const row = visibleChartRows()[index];
      const target = document.getElementById("chartHoverInfo");
      if (!row || !target) return;
      const change = Number(row.close) - Number(row.open);
      const changePct = Number(row.open) ? change / Number(row.open) * 100 : null;
      target.textContent = `${trendLabel(row)}｜開 ${fmt(row.open)} 高 ${fmt(row.high)} 低 ${fmt(row.low)} 收 ${fmt(row.close)}｜量 ${fmt(row.volume, 0)}｜單棒 ${fmt(change)} / ${fmt(changePct)}%`;
    }
    function setSyncedCursor(index, rowCount = currentPriceRows.length) {
      const ratio = rowCount > 1 ? index / (rowCount - 1) : 1;
      const visibleRows = visibleChartRows();
      const infoIndex = visibleRows.length > 1 ? Math.round(ratio * (visibleRows.length - 1)) : index;
      showCandleInfo(infoIndex);
      document.querySelectorAll(".sync-cursor").forEach(line => {
        const width = Number(line.dataset.width || 1000);
        const left = Number(line.dataset.left || 54);
        const right = Number(line.dataset.right || 18);
        const x = left + ratio * (width - left - right);
        line.setAttribute("x1", x);
        line.setAttribute("x2", x);
        line.style.opacity = "1";
      });
    }
    function syncCursorLayer(rows, width, height, pad, bottom = height - pad.bottom) {
      if (!rows || rows.length < 2) return "";
      const plotW = width - pad.left - pad.right;
      const step = plotW / (rows.length - 1);
      const zoneW = Math.max(5, Math.min(18, plotW / rows.length));
      const zones = rows.map((row, index) => {
        const cx = pad.left + index * step;
        const x = Math.max(pad.left, cx - zoneW / 2);
        const w = Math.min(zoneW, width - pad.right - x);
        const title = row.open === undefined ? trendLabel(row) : `${trendLabel(row)} O:${fmt(row.open)} H:${fmt(row.high)} L:${fmt(row.low)} C:${fmt(row.close)} V:${fmt(row.volume, 0)}`;
        return `<rect x="${x}" y="${pad.top}" width="${w}" height="${bottom - pad.top}" fill="transparent" onmousemove="setSyncedCursor(${index}, ${rows.length})"><title>${title}</title></rect>`;
      }).join("");
      return `<line class="sync-cursor" data-width="${width}" data-left="${pad.left}" data-right="${pad.right}" y1="${pad.top}" y2="${bottom}"></line>${zones}`;
    }
    function renderCandlestickChart(target, rows, mode = "all") {
      const width = 1000;
      const height = 420;
      const pad = { left: 54, right: 72, top: 20, bottom: 34 };
      const volumeBottom = height - pad.bottom;
      target.setAttribute("viewBox", `0 0 ${width} ${height}`);
      if (!rows || rows.length < 2) {
        target.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">資料不足</text>`;
        return;
      }
      const highs = rows.map(row => Number(row.high));
      const lows = rows.map(row => Number(row.low));
      const closes = rows.map(row => Number(row.close));
      const max = Math.max(...highs);
      const min = Math.min(...lows);
      const span = max - min || 1;
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      const step = plotW / (rows.length - 1);
      const x = index => pad.left + index * step;
      const y = value => pad.top + (max - value) * (plotH / span);
      const candleW = Math.max(3, Math.min(9, step * .55));
      const candles = rows.map((row, index) => {
        const open = Number(row.open);
        const close = Number(row.close);
        const high = Number(row.high);
        const low = Number(row.low);
        const cx = x(index);
        const up = close >= open;
        const color = up ? "#ff2d2d" : "#00ff5a";
        const bodyY = Math.min(y(open), y(close));
        const bodyH = Math.max(1, Math.abs(y(open) - y(close)));
        return `
          <line x1="${cx}" y1="${y(high)}" x2="${cx}" y2="${y(low)}" stroke="${color}" stroke-width="1.4"></line>
          <rect x="${cx - candleW / 2}" y="${bodyY}" width="${candleW}" height="${bodyH}" fill="${color}" opacity="1"></rect>
        `;
      }).join("");
      const ma5 = smaValues(closes, 5);
      const ma10 = smaValues(closes, 10);
      const ma20 = smaValues(closes, 20);
      const std20 = stdValues(closes, 20);
      const upper = ma20.map((value, index) => value === null ? null : value + 2 * std20[index]);
      const lower = ma20.map((value, index) => value === null ? null : value - 2 * std20[index]);
      const linePath = values => values
        .map((value, index) => value === null ? null : `${x(index)},${y(value)}`)
        .filter(Boolean)
        .join(" ");
      const latest = closes[closes.length - 1];
      const latestY = y(latest);
      const recent = rows.slice(-20);
      const recentHigh = Math.max(...recent.map(row => Number(row.high)));
      const recentLow = Math.min(...recent.map(row => Number(row.low)));
      const showMa = mode === "all" || mode === "ma";
      const showBollinger = mode === "all" || mode === "bollinger";
      const showLevels = true;
      target.innerHTML = `
        <line class="chart-axis" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"></line>
        <line class="chart-axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>
        <line class="chart-axis" x1="${pad.left}" y1="${volumeBottom}" x2="${width - pad.right}" y2="${volumeBottom}"></line>
        ${candles}
        ${showLevels ? `<line x1="${pad.left}" y1="${y(recentHigh)}" x2="${width - pad.right}" y2="${y(recentHigh)}" stroke="#d9a441" stroke-dasharray="6 6" opacity=".75"></line>` : ""}
        ${showLevels ? `<line x1="${pad.left}" y1="${y(recentLow)}" x2="${width - pad.right}" y2="${y(recentLow)}" stroke="#64748b" stroke-dasharray="6 6" opacity=".65"></line>` : ""}
        <line x1="${pad.left}" y1="${latestY}" x2="${width - pad.right}" y2="${latestY}" stroke="#38bdf8" stroke-dasharray="4 5" opacity=".62"></line>
        ${showBollinger ? `<polyline fill="none" stroke="#a78bfa" stroke-width="1.6" points="${linePath(upper)}"></polyline>` : ""}
        ${showBollinger ? `<polyline fill="none" stroke="#a78bfa" stroke-width="1.6" points="${linePath(lower)}"></polyline>` : ""}
        ${showMa ? `<polyline fill="none" stroke="#38bdf8" stroke-width="2" points="${linePath(ma5)}"></polyline>` : ""}
        ${showMa ? `<polyline fill="none" stroke="#f59e0b" stroke-width="2" points="${linePath(ma10)}"></polyline>` : ""}
        ${showMa ? `<polyline fill="none" stroke="#22c55e" stroke-width="2" points="${linePath(ma20)}"></polyline>` : ""}
        <text class="chart-label" x="${pad.left}" y="${height - 10}">${rows[0].date || ""}</text>
        <text class="chart-label" x="${width - pad.right}" y="${height - 10}" text-anchor="end">${rows[rows.length - 1].date || ""}</text>
        <text class="chart-label" x="8" y="${pad.top + 8}">${fmt(max)}</text>
        <text class="chart-label" x="8" y="${height - pad.bottom}">${fmt(min)}</text>
        <text class="chart-label" x="${width - pad.right + 8}" y="${latestY + 4}">${fmt(latest)}</text>
        ${showLevels ? `<text class="chart-label" x="${width - pad.right + 8}" y="${y(recentHigh) + 4}">壓力</text>` : ""}
        ${showLevels ? `<text class="chart-label" x="${width - pad.right + 8}" y="${y(recentLow) + 4}">支撐</text>` : ""}
        ${showMa ? `<text class="chart-label" x="${pad.left + 8}" y="22" style="fill:#38bdf8">SMA(5) ${fmt(ma5.at(-1))}</text>` : ""}
        ${showMa ? `<text class="chart-label" x="${pad.left + 166}" y="22" style="fill:#f59e0b">SMA(10) ${fmt(ma10.at(-1))}</text>` : ""}
        ${showMa ? `<text class="chart-label" x="${pad.left + 340}" y="22" style="fill:#a855f7">SMA(20) ${fmt(ma20.at(-1))}</text>` : ""}
        ${showBollinger ? `<text class="chart-label" x="${width - 190}" y="22" style="fill:#a78bfa">布林 ${fmt(upper.at(-1))}/${fmt(lower.at(-1))}</text>` : ""}
        ${syncCursorLayer(rows, width, height, pad, height - pad.bottom)}
      `;
    }
    function renderVolumeChart(target, rows) {
      const width = 1000;
      const height = 220;
      const pad = { left: 54, right: 72, top: 18, bottom: 34 };
      target.setAttribute("viewBox", `0 0 ${width} ${height}`);
      if (!rows || rows.length < 2) {
        target.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">資料不足</text>`;
        return;
      }
      const max = Math.max(...rows.map(row => Number(row.volume || 0)), 1);
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      const step = plotW / (rows.length - 1);
      const x = index => pad.left + index * step;
      const barW = Math.max(2, Math.min(9, step * .55));
      const bars = rows.map((row, index) => {
        const color = Number(row.close) >= Number(row.open) ? "#ff2d2d" : "#00ff5a";
        const h = Math.max(1, Number(row.volume || 0) / max * plotH);
        return `<rect x="${x(index) - barW / 2}" y="${height - pad.bottom - h}" width="${barW}" height="${h}" fill="${color}" opacity=".76"><title>${trendLabel(row)} 量 ${fmt(row.volume, 0)}</title></rect>`;
      }).join("");
      target.innerHTML = `
        <line class="chart-axis" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"></line>
        <line class="chart-axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>
        ${bars}
        <text class="chart-label" x="8" y="${pad.top + 8}">${fmt(max, 0)}</text>
        <text class="chart-label" x="${pad.left}" y="${height - 10}">${trendLabel(rows[0])}</text>
        <text class="chart-label" x="${width - pad.right}" y="${height - 10}" text-anchor="end">${trendLabel(rows[rows.length - 1])}</text>
        ${syncCursorLayer(rows, width, height, pad)}
      `;
    }
    function renderRsiChart(target, rows, sourceRows = rows, startIndex = 0) {
      const sourceValues = sourceRows.map(row => Number(row.close));
      const visibleValues = rsiValues(sourceValues, 14).slice(startIndex, startIndex + rows.length);
      const rsi = rows.map((row, index) => ({ date: row.date, label: trendLabel(row), value: visibleValues[index] }));
      const width = 1000;
      const height = 220;
      const pad = { left: 54, right: 72, top: 18, bottom: 34 };
      target.setAttribute("viewBox", `0 0 ${width} ${height}`);
      const valid = rsi.filter(row => row.value !== null);
      if (valid.length < 2) {
        target.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">資料不足</text>`;
        return;
      }
      const x = index => pad.left + index * ((width - pad.left - pad.right) / (rows.length - 1));
      const y = value => pad.top + (100 - value) * ((height - pad.top - pad.bottom) / 100);
      const points = rsi.map((row, index) => row.value === null ? null : `${x(index)},${y(row.value)}`).filter(Boolean).join(" ");
      target.innerHTML = `
        <line class="chart-axis" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"></line>
        <line class="chart-axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>
        <line x1="${pad.left}" y1="${y(70)}" x2="${width - pad.right}" y2="${y(70)}" stroke="#d9a441" stroke-dasharray="4 4"></line>
        <line x1="${pad.left}" y1="${y(30)}" x2="${width - pad.right}" y2="${y(30)}" stroke="#94a3b8" stroke-dasharray="4 4"></line>
        <polyline class="chart-line" points="${points}"></polyline>
        <text class="chart-label" x="8" y="${y(70) + 4}">70</text>
        <text class="chart-label" x="8" y="${y(30) + 4}">30</text>
        <text class="chart-label" x="${pad.left}" y="${height - 10}">${trendLabel(rows[0])}</text>
        <text class="chart-label" x="${width - pad.right}" y="${height - 10}" text-anchor="end">${trendLabel(rows[rows.length - 1])}</text>
        ${syncCursorLayer(rows, width, height, pad)}
      `;
    }
    function renderMacdChart(target, rows, sourceRows = rows, startIndex = 0) {
      const sourceValues = sourceRows.map(row => Number(row.close));
      const fullData = macdValues(sourceValues);
      const data = {
        macd: fullData.macd.slice(startIndex, startIndex + rows.length),
        signal: fullData.signal.slice(startIndex, startIndex + rows.length),
        hist: fullData.hist.slice(startIndex, startIndex + rows.length),
      };
      const width = 1000;
      const height = 240;
      const pad = { left: 54, right: 72, top: 18, bottom: 34 };
      target.setAttribute("viewBox", `0 0 ${width} ${height}`);
      if (sourceValues.length < 35 || rows.length < 2) {
        target.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">資料不足</text>`;
        return;
      }
      const all = [...data.macd, ...data.signal, ...data.hist].filter(Number.isFinite);
      const max = Math.max(...all);
      const min = Math.min(...all);
      const span = max - min || 1;
      const step = (width - pad.left - pad.right) / (rows.length - 1);
      const x = index => pad.left + index * step;
      const y = value => pad.top + (max - value) * ((height - pad.top - pad.bottom) / span);
      const zeroY = y(0);
      const barW = Math.max(2, Math.min(7, step * .55));
      const bars = data.hist.map((value, index) => {
        const up = value >= 0;
        const color = up ? "#ff2d2d" : "#00ff5a";
        const top = Math.min(y(value), zeroY);
        const h = Math.max(1, Math.abs(y(value) - zeroY));
        return `<rect x="${x(index) - barW / 2}" y="${top}" width="${barW}" height="${h}" fill="${color}" opacity=".75"></rect>`;
      }).join("");
      const linePath = values => values.map((value, index) => `${x(index)},${y(value)}`).join(" ");
      target.innerHTML = `
        <line class="chart-axis" x1="${pad.left}" y1="${zeroY}" x2="${width - pad.right}" y2="${zeroY}"></line>
        <line class="chart-axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>
        ${bars}
        <polyline fill="none" stroke="#2563eb" stroke-width="2" points="${linePath(data.macd)}"></polyline>
        <polyline fill="none" stroke="#f59e0b" stroke-width="2" points="${linePath(data.signal)}"></polyline>
        <text class="chart-label" x="${pad.left}" y="${height - 10}">${trendLabel(rows[0])}</text>
        <text class="chart-label" x="${width - pad.right}" y="${height - 10}" text-anchor="end">${trendLabel(rows[rows.length - 1])}</text>
        <text class="chart-label" x="${width - 128}" y="22">MACD</text>
        <text class="chart-label" x="${width - 70}" y="22">Signal</text>
        ${syncCursorLayer(rows, width, height, pad)}
      `;
    }
    function renderKdChart(target, rows, sourceRows = rows, startIndex = 0) {
      const data = kdValues(sourceRows, 9).slice(startIndex, startIndex + rows.length);
      const width = 1000;
      const height = 220;
      const pad = { left: 54, right: 72, top: 18, bottom: 34 };
      target.setAttribute("viewBox", `0 0 ${width} ${height}`);
      const valid = data.filter(row => row.k !== null);
      if (valid.length < 2) {
        target.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">資料不足</text>`;
        return;
      }
      const x = index => pad.left + index * ((width - pad.left - pad.right) / (rows.length - 1));
      const y = value => pad.top + (100 - value) * ((height - pad.top - pad.bottom) / 100);
      const linePath = key => data.map((row, index) => row[key] === null ? null : `${x(index)},${y(row[key])}`).filter(Boolean).join(" ");
      target.innerHTML = `
        <line class="chart-axis" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"></line>
        <line class="chart-axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>
        <line x1="${pad.left}" y1="${y(80)}" x2="${width - pad.right}" y2="${y(80)}" stroke="#d9a441" stroke-dasharray="4 4"></line>
        <line x1="${pad.left}" y1="${y(20)}" x2="${width - pad.right}" y2="${y(20)}" stroke="#94a3b8" stroke-dasharray="4 4"></line>
        <polyline fill="none" stroke="#2563eb" stroke-width="2" points="${linePath("k")}"></polyline>
        <polyline fill="none" stroke="#f59e0b" stroke-width="2" points="${linePath("d")}"></polyline>
        <text class="chart-label" x="8" y="${y(80) + 4}">80</text>
        <text class="chart-label" x="8" y="${y(20) + 4}">20</text>
        <text class="chart-label" x="${pad.left}" y="${height - 10}">${trendLabel(rows[0])}</text>
        <text class="chart-label" x="${width - pad.right}" y="${height - 10}" text-anchor="end">${trendLabel(rows[rows.length - 1])}</text>
        <text class="chart-label" x="${width - 80}" y="22">K / D</text>
        ${syncCursorLayer(rows, width, height, pad)}
      `;
    }
    function renderInstitutionalChart(target, data, mode = "foreign") {
      const rows = alignInstitutionalRows(data, visibleChartRows());
      const width = 1000;
      const height = 240;
      const pad = { left: 54, right: 72, top: 18, bottom: 36 };
      target.setAttribute("viewBox", `0 0 ${width} ${height}`);
      if (!rows.length) {
        target.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">${data.message || "法人資料不足"}</text>`;
        return;
      }
      const colors = { foreign: "#38bdf8", investment: "#f59e0b", retail_proxy: "#a78bfa" };
      const labels = { foreign: "外資", investment: "投信", retail_proxy: "散戶代理" };
      const key = colors[mode] ? mode : "foreign";
      const title = document.getElementById("chipChartTitle");
      if (title) title.textContent = `${labels[key]}買賣超`;
      const all = rows.map(row => row.hasInstitutional ? Number(row[key] || 0) : null).filter(Number.isFinite);
      const maxAbs = Math.max(...all.map(Math.abs), 1);
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      const zeroY = pad.top + plotH / 2;
      const step = rows.length > 1 ? plotW / (rows.length - 1) : plotW;
      const barW = Math.max(3, Math.min(11, step * .48));
      const y = value => zeroY - (Number(value) / maxAbs) * (plotH / 2 - 8);
      const bars = rows.map((row, index) => {
        if (!row.hasInstitutional) return "";
        const value = Number(row[key] || 0);
        const yy = y(value);
        const top = Math.min(yy, zeroY);
        const h = Math.max(1, Math.abs(yy - zeroY));
        const color = value >= 0 ? "#ef4444" : "#00e676";
        const x = pad.left + index * step - barW / 2;
        return `<rect x="${x}" y="${top}" width="${barW}" height="${h}" fill="${color}" opacity=".82"><title>${row.date} ${labels[key]} ${fmt(value, 0)}</title></rect>`;
      }).join("");
      target.innerHTML = `
        <line class="chart-axis" x1="${pad.left}" y1="${zeroY}" x2="${width - pad.right}" y2="${zeroY}"></line>
        <line class="chart-axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>
        ${bars}
        <text class="chart-label" x="${width - 158}" y="22" fill="${colors[key]}">${labels[key]}｜紅買超 綠賣超</text>
        <text class="chart-label" x="8" y="${pad.top + 8}">${fmt(maxAbs, 0)}</text>
        <text class="chart-label" x="8" y="${height - pad.bottom}">-${fmt(maxAbs, 0)}</text>
        <text class="chart-label" x="${pad.left}" y="${height - 10}">${trendLabel(rows[0])}</text>
        <text class="chart-label" x="${width - pad.right}" y="${height - 10}" text-anchor="end">${trendLabel(rows[rows.length - 1])}</text>
        ${syncCursorLayer(rows, width, height, pad)}
      `;
    }
    async function fetchStatus() {
      const data = await getJson("/api/status");
      document.getElementById("range").textContent = `${data.last_date} 官方 ${data.official_count || 0} 檔 / 最新 ${data.latest_count || 0} 檔`;
    }
    async function fetchStock() {
      const codeValue = document.getElementById("code").value.trim() || "2330";
      currentStockCode = codeValue;
      currentStockInterval = "1d";
      document.querySelectorAll("[data-interval]").forEach(tab => tab.classList.toggle("active", tab.dataset.interval === "1d"));
      const encoded = encodeURIComponent(codeValue);
      const [stock, ind, prices, signal, monitor, institutional] = await Promise.all([
        getJson(`/api/stock?code=${encoded}`),
        getJson(`/api/indicators?code=${encoded}`),
        getJson(`/api/prices?code=${encoded}&limit=160`),
        getJson(`/api/stock-signal?code=${encoded}`),
        getJson(`/api/ai-monitor-stock?code=${encoded}`),
        getJson(`/api/institutional?code=${encoded}`),
      ]);
      currentPriceRows = prices.prices || [];
      resetChartWindow();
      renderDl(document.getElementById("stockSummary"), [
        ["代號", `${stock.code} ${stock.short_name || stock.name}`],
        ["市場", stock.market],
        ["產業分類", stock.industry || "無資料"],
        ["資料筆數", fmt(stock.rows, 0)],
        ["資料範圍", `${stock.first_date} 至 ${stock.last_date}`],
        ["最新收盤", stock.latest ? fmt(stock.latest.close) : "無資料"],
        ["最新成交量", stock.latest ? fmt(stock.latest.volume, 0) : "無資料"],
      ]);
      renderDl(document.getElementById("indicators"), [
        ["最新日期", ind.latest_date],
        ["收盤", fmt(ind.close)],
        ["5日報酬", `${fmt(ind.return_5d)}%`],
        ["20日報酬", `${fmt(ind.return_20d)}%`],
        ["SMA 20", fmt(ind.sma_20)],
        ["SMA 60", fmt(ind.sma_60)],
        ["RSI 14", fmt(ind.rsi_14)],
        ["MACD histogram", fmt(ind.macd_histogram)],
        ["5日/20日量比", fmt(ind.volume_ratio)],
        ["60日新高", fmt(ind.new_high_60)],
      ]);
      currentInstitutional = institutional;
      renderChartSuite();
      installChartDrag();
      renderTradePlan(document.getElementById("tradePlan"), buildTradePlan(stock, ind, signal, prices.prices));
      renderAnalysisFacets(document.getElementById("analysisFacets"), monitor);
      const fundamentalTarget = document.getElementById("fundamentalResearch");
      fundamentalTarget.innerHTML = `<div class="fundamental-card"><span>基本面研究</span><strong>背景載入中</strong><ul><li>先顯示 K 線與交易計畫，研究報告稍後補上。</li></ul></div>`;
      getJson(`/api/fundamental?code=${encoded}`)
        .then(fundamental => renderFundamentalResearch(fundamentalTarget, fundamental))
        .catch(error => {
          fundamentalTarget.innerHTML = `<div class="fundamental-card"><span>基本面研究</span><strong>暫時不可用</strong><ul><li>${error.message}</li></ul></div>`;
        });
      const newsTarget = document.getElementById("newsResearch");
      newsTarget.innerHTML = `<div class="news-card"><span>新聞掃描</span><a href="#">正在背景掃描最新新聞...</a><small>核心分析已先完成，不等待新聞來源。</small></div>`;
      getJson(`/api/news?code=${encoded}`)
        .then(news => renderNews(newsTarget, news))
        .catch(error => renderNews(newsTarget, { message: `新聞掃描暫時失敗：${error.message}`, items: [] }));
      document.getElementById("diagScore").textContent = fmt(signal.intelli_score, 1);
      document.getElementById("diagSentiment").textContent = signal.sentiment;
      document.getElementById("diagRiskScore").textContent = fmt(signal.risk_adjusted_score, 0);
      document.getElementById("heroScore").textContent = fmt(signal.intelli_score, 1);
      document.getElementById("stockHeroName").textContent = `${stock.code} ${stock.short_name || stock.name}`;
      document.getElementById("stockHeroSub").textContent = `${stock.market} · ${signal.signal} · ${signal.sentiment} · 建議搭配 K 線與風險提示觀察`;
    }
    function activatePage(pageId) {
      document.querySelectorAll(".page").forEach(page => page.classList.remove("active"));
      document.getElementById(pageId).classList.add("active");
      document.body.classList.toggle("stock-focus", pageId === "stockPage");
      document.querySelectorAll(".overview-only").forEach(item => item.classList.toggle("hidden", pageId !== "overview"));
      document.querySelectorAll(".nav-item").forEach(item => item.classList.remove("active"));
      const nav = document.querySelector(`[data-page="${pageId}"]`);
      if (nav) nav.classList.add("active");
      if (pageId === "stockPage") installChartDrag();
    }
    function searchStock() {
      activatePage("stockPage");
      runAction(fetchStock, "正在載入個股摘要、技術分析與走勢圖...");
    }
    async function fetchScan() {
      const data = await getJson("/api/scan?limit=20");
      renderTable(document.getElementById("returnTable"), data.top_return_20d);
      renderTable(document.getElementById("volumeTable"), data.top_volume_expansion);
    }
    async function fetchSignals() {
      const data = await getJson("/api/signals?limit=20");
      renderSignals(document.getElementById("signalsTable"), data.top_signals);
      const overview = document.getElementById("overviewSignalsTable");
      if (overview) renderSignals(overview, data.top_signals.slice(0, 5));
    }
    async function fetchHub() {
      const [signals, scan] = await Promise.all([
        getJson("/api/signals?limit=12"),
        getJson("/api/scan?limit=10"),
      ]);
      renderSignals(document.getElementById("hubSignalsTable"), signals.top_signals || []);
      renderTable(document.getElementById("hubReturnTable"), scan.top_return_20d || []);
      renderTable(document.getElementById("hubVolumeTable"), scan.top_volume_expansion || []);
    }
    async function fetchStrategy() {
      const data = await getJson("/api/strategy");
      const s = data.summary;
      const summaryRows = [
        ["開始操盤", s.start_date || "2026-05-01"],
        ["起始資金", fmt(s.initial_capital)],
        ["最大持股數", fmt(s.max_positions, 0)],
        ["持有期間", `${fmt(s.horizon, 0)} 個交易日`],
        ["交易筆數", fmt(s.trades, 0)],
        ["未平倉部位", fmt(s.open_positions, 0)],
        ["最終資金", fmt(s.final_capital)],
        ["總報酬", `${fmt(s.total_return)}%`],
        ["勝率", `${fmt(s.win_rate)}%`],
        ["中位數交易", `${fmt(s.median_trade_return)}%`],
        ["最大回撤", `${fmt(s.max_drawdown)}%`],
      ];
      renderDl(document.getElementById("strategySummary"), summaryRows);
      const overviewStrategy = document.getElementById("overviewStrategySummary");
      if (overviewStrategy) renderDl(overviewStrategy, summaryRows);
      renderStrategyAdvice(document.getElementById("strategyAdvice"), data.market_context || {});
      renderHighWinStrategy(document.getElementById("highWinStrategy"), document.getElementById("highWinTradesTable"), data.high_win_strategy || {});
      renderStrategyLeaders(document.getElementById("strategyLeadersTable"), (data.market_context || {}).leaders || []);
      renderStrategyStockCards(document.getElementById("strategyStockCards"), (data.market_context || {}).leaders || []);
      renderStrategyEntries(document.getElementById("strategyEntriesTable"), data.recent_entries || []);
      renderStrategyTrades(document.getElementById("strategyTradesTable"), data.closed_trades || data.recent_trades || []);
      renderStrategyOpen(document.getElementById("strategyOpenTable"), data.open_positions || []);
      renderCurve(document.getElementById("strategyCurveTable"), data.curve);
      renderLineChart(document.getElementById("equityChart"), data.curve, "capital");
    }
    async function fetchWatch() {
      const data = await getJson("/api/watch");
      document.getElementById("watchHighs").textContent = fmt(data.summary.new_high_60, 0);
      document.getElementById("watchSma20").textContent = fmt(data.summary.above_sma20, 0);
      document.getElementById("watchSma60").textContent = fmt(data.summary.above_sma60, 0);
      const watchData = await getWatchlistData();
      const majors = watchData.watchlist || data.majors || [];
      renderMajors(document.getElementById("watchMajorsTable"), majors);
      const modeText = currentUserKey
        ? "個人模式：觀察名單會綁定你的使用者代碼，推播只送到你的 Telegram。"
        : publicDemoMode ? "公開展示模式：觀察名單只存在此瀏覽器，不會影響主機與推播。" : "觀察名單會保存在本機資料庫，可同步到即時看盤與推播。";
      document.getElementById("watchlistHint").textContent = `目前觀察 ${majors.length} 檔。${modeText}`;
      renderDl(document.getElementById("watchAfterSummary"), [
        ["盤後定位", "這裡只保留觀察名單與盤後摘要，AI 智選、強勢排行與量能焦點集中到股票探索。"],
        ["60 日新高", `${fmt(data.summary.new_high_60, 0)} 檔`],
        ["站上月線", `${fmt(data.summary.above_sma20, 0)} 檔`],
        ["站上季線", `${fmt(data.summary.above_sma60, 0)} 檔`],
      ]);
    }
    async function addWatchlistCode() {
      const code = document.getElementById("watchlistCode").value.trim();
      if (currentUserKey) {
        const data = await getJson(`/api/user/watchlist/add?user_key=${encodeURIComponent(currentUserKey)}&code=${encodeURIComponent(code)}`);
        renderMajors(document.getElementById("watchMajorsTable"), data.watchlist || []);
        document.getElementById("watchlistHint").textContent = `已加入 ${code}。這是你的個人觀察名單。`;
        return;
      }
      if (publicDemoMode) {
        const codes = saveLocalWatchlist([...localWatchlistCodes(), code]);
        const data = await getJson(`/api/watchlist?codes=${encodeURIComponent(codes.join(","))}`);
        renderMajors(document.getElementById("watchMajorsTable"), data.watchlist || []);
        document.getElementById("watchlistHint").textContent = `已加入 ${code}。公開展示模式只會更新此瀏覽器，不會影響你的主機觀察名單。`;
        return;
      }
      const data = await getJson(`/api/watchlist/add?code=${encodeURIComponent(code)}`);
      renderMajors(document.getElementById("watchMajorsTable"), data.watchlist || []);
      document.getElementById("watchlistHint").textContent = `已加入 ${code}，目前觀察 ${(data.watchlist || []).length} 檔。`;
    }
    async function removeWatchlistCode(code) {
      if (currentUserKey) {
        const data = await getJson(`/api/user/watchlist/remove?user_key=${encodeURIComponent(currentUserKey)}&code=${encodeURIComponent(code)}`);
        renderMajors(document.getElementById("watchMajorsTable"), data.watchlist || []);
        document.getElementById("watchlistHint").textContent = `已移除 ${code}。這是你的個人觀察名單。`;
        return;
      }
      if (publicDemoMode) {
        const codes = saveLocalWatchlist(localWatchlistCodes().filter(item => item !== code));
        const data = await getJson(`/api/watchlist?codes=${encodeURIComponent(codes.join(","))}`);
        renderMajors(document.getElementById("watchMajorsTable"), data.watchlist || []);
        document.getElementById("watchlistHint").textContent = `已移除 ${code}。公開展示模式只會更新此瀏覽器。`;
        return;
      }
      const data = await getJson(`/api/watchlist/remove?code=${encodeURIComponent(code)}`);
      renderMajors(document.getElementById("watchMajorsTable"), data.watchlist || []);
      document.getElementById("watchlistHint").textContent = `已移除 ${code}，目前觀察 ${(data.watchlist || []).length} 檔。`;
    }
    async function useWatchlistRealtime() {
      const data = await getWatchlistData();
      const codes = (data.codes || []).join(",");
      if (!codes) {
        setStatus("觀察名單是空的，請先加入股票。");
        return;
      }
      document.getElementById("realtimeCodes").value = codes;
      showPage("realtimePage");
    }
    function openStock(code) {
      document.getElementById("code").value = code;
      searchStock();
    }
    function openHubStock() {
      const code = document.getElementById("hubCode").value.trim() || "2330";
      openStock(code);
    }
    async function loadUserProfile() {
      if (!currentUserKey) {
        updateNotifyUi(null);
        return;
      }
      try {
        const data = await getJson(`/api/user/profile?user_key=${encodeURIComponent(currentUserKey)}`);
        updateNotifyUi(data.user);
      } catch (error) {
        localStorage.removeItem(userKeyStorageKey);
        currentUserKey = "";
        updateNotifyUi(null);
      }
    }
    function updateNotifyUi(user) {
      const keyEl = document.getElementById("notifyUserKey");
      const statusEl = document.getElementById("notifyTelegramStatus");
      const hintEl = document.getElementById("notifyHint");
      if (!keyEl || !statusEl || !hintEl) return;
      if (!user) {
        keyEl.textContent = "尚未建立";
        statusEl.textContent = "尚未設定";
        hintEl.textContent = "請先建立個人設定，再儲存 Telegram chat_id。";
        return;
      }
      keyEl.textContent = user.user_key;
      document.getElementById("telegramChatId").value = user.telegram_chat_id || "";
      statusEl.textContent = user.telegram_enabled ? `已啟用：${user.telegram_chat_id}` : "尚未啟用";
      hintEl.textContent = `${user.display_name} 的個人設定已啟用。請妥善保存個人代碼；同一個瀏覽器會自動記住。`;
    }
    async function createUserProfile() {
      const name = document.getElementById("notifyName").value.trim() || "朋友";
      const data = await getJson(`/api/user/create?name=${encodeURIComponent(name)}`);
      currentUserKey = data.user.user_key;
      localStorage.setItem(userKeyStorageKey, currentUserKey);
      updateNotifyUi(data.user);
      await fetchWatch();
    }
    async function saveUserTelegram() {
      if (!currentUserKey) throw new Error("請先建立個人設定。");
      const chatId = document.getElementById("telegramChatId").value.trim();
      const data = await getJson(`/api/user/telegram/save?user_key=${encodeURIComponent(currentUserKey)}&chat_id=${encodeURIComponent(chatId)}`);
      updateNotifyUi(data.user);
    }
    async function sendUserTelegramTest() {
      if (!currentUserKey) throw new Error("請先建立個人設定。");
      const data = await getJson(`/api/user/telegram/test?user_key=${encodeURIComponent(currentUserKey)}`);
      document.getElementById("notifyHint").textContent = data.message || "測試推播已送出。";
    }
    let realtimeTimer = null;
    let selectedRealtimeCode = "";
    async function realtimeCodesFromWatchlist() {
      const manual = document.getElementById("realtimeCodes").value.trim();
      if (manual) return manual;
      const data = await getWatchlistData();
      const codes = (data.codes || []).join(",");
      if (codes) {
        document.getElementById("realtimeCodes").value = codes;
      }
      return codes;
    }
    async function fetchRealtime() {
      const codes = await realtimeCodesFromWatchlist();
      if (!codes) {
        renderRealtime(document.getElementById("realtimeTable"), []);
        renderRealtimeMonitor(await getJson("/api/ai-monitor"));
        document.getElementById("realtimeNotice").textContent = "觀察名單是空的，請先到盤後看盤加入股票。";
        document.getElementById("realtimeTrendSummary").innerHTML = "";
        document.getElementById("realtimeTrendChart").innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">請先加入觀察名單</text>`;
        document.getElementById("realtimeTrendNotice").textContent = "即時看盤會直接使用你的觀察名單，不再載入預設股票。";
        return;
      }
      const [data, monitor] = await Promise.all([
        getJson(`/api/realtime?codes=${encodeURIComponent(codes)}`),
        getJson("/api/ai-monitor"),
      ]);
      const quotes = data.quotes || [];
      renderRealtime(document.getElementById("realtimeTable"), quotes);
      renderRealtimeMonitor(monitor);
      document.getElementById("realtimeNotice").textContent = data.message || "即時看盤資料已更新。";
      if (quotes.length) {
        const nextCode = quotes.some(row => row.code === selectedRealtimeCode) ? selectedRealtimeCode : quotes[0].code;
        await selectRealtimeTrend(nextCode);
      }
    }
    function renderRealtimeMonitor(monitor) {
      const m = (monitor && monitor.summary) || {};
      const isIntraday = !!m.is_intraday;
      document.getElementById("aiMonitorSummary").innerHTML = isIntraday ? `
        <dl>
          <dt>模式</dt><dd>${m.session_label || "盤中盯盤"}</dd>
          <dt>盯盤狀態</dt><dd>${m.stance || "資料不足"}</dd>
          <dt>風控</dt><dd>${fmt(m.urgent, 0)} 檔</dd>
          <dt>偏多</dt><dd>${fmt(m.positive, 0)} 檔</dd>
          <dt>觀察</dt><dd>${fmt(m.watch, 0)} 檔</dd>
        </dl>` : `<div class="trade-callout"><strong>${m.session_label || "盤後整理"}</strong><p>${m.message || "AI 盯盤只在台股盤中顯示。盤後請看盤後觀察與 AI 實操。"}</p><p>目前時間：${m.now || ""}</p></div>`;
      if (isIntraday) {
        renderAiMonitor(document.getElementById("aiMonitorTable"), monitor.items || []);
      } else {
        document.getElementById("aiMonitorTable").innerHTML = `<thead><tr><th>盤中盯盤暫停</th></tr></thead><tbody><tr><td>現在不是台股盤中時段，系統不顯示盤中盯盤指令。</td></tr></tbody>`;
      }
    }
    async function selectRealtimeTrend(code) {
      selectedRealtimeCode = code;
      document.querySelectorAll(".realtime-row").forEach(row => {
        row.classList.toggle("selected", row.dataset.code === code);
      });
      const data = await getJson(`/api/realtime-trend?code=${encodeURIComponent(code)}`);
      renderRealtimeTrend(data);
    }
    function startRealtime() {
      stopRealtime();
      runAction(fetchRealtime, "正在刷新即時報價...");
      realtimeTimer = setInterval(() => runAction(fetchRealtime, "自動刷新即時報價..."), 30000);
      setStatus("已啟動 30 秒自動刷新。");
    }
    function stopRealtime() {
      if (realtimeTimer) clearInterval(realtimeTimer);
      realtimeTimer = null;
      setStatus("已停止自動刷新。");
    }
    function showPage(pageId, navItem) {
      activatePage(pageId);
      if (navItem) navItem.classList.add("active");
      if (pageId === "strategyPage") runAction(fetchStrategy, "正在執行 AI 實操回測，可能需要約一分鐘...");
      if (pageId === "realtimePage") runAction(fetchRealtime, "正在載入即時看盤...");
      if (pageId === "watchPage") runAction(fetchWatch, "正在載入盤後看盤資料...");
      if (pageId === "signalsPage") runAction(fetchSignals, "正在載入訊號排行...");
      if (pageId === "rankingPage") runAction(fetchScan, "正在載入市場排行榜...");
      if (pageId === "hubPage") runAction(fetchHub, "正在載入股票探索...");
      if (pageId === "stockPage") runAction(fetchStock, "正在載入個股資料...");
    }
    function toggleSection(sectionId, button) {
      const section = document.getElementById(sectionId);
      section.classList.toggle("collapsed");
      button.textContent = section.classList.contains("collapsed") ? "展開" : "收起";
    }
    function setStatus(message) {
      document.getElementById("statusLine").textContent = message;
    }
    function runAction(action, message = "載入中...") {
      setStatus(message);
      action()
        .then(() => setStatus("完成。"))
        .catch(error => {
          setStatus(`錯誤：${error.message}`);
          alert(error.message);
        });
    }
    document.getElementById("code").addEventListener("keydown", event => {
      if (event.key === "Enter") searchStock();
    });
    document.getElementById("watchlistCode").addEventListener("keydown", event => {
      if (event.key === "Enter") runAction(addWatchlistCode, "正在加入觀察名單...");
    });
    document.getElementById("hubCode").addEventListener("keydown", event => {
      if (event.key === "Enter") openHubStock();
    });
    initPublicConfig()
      .then(() => fetchStatus())
      .then(() => fetchStock())
      .then(() => setStatus("準備就緒。排行與 AI 實操會在切換頁面時載入。"))
      .catch(error => {
        setStatus(`錯誤：${error.message}`);
        alert(error.message);
      });
  </script>
</body>
</html>
"""
