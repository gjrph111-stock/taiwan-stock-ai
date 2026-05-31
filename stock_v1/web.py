import csv
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
from .ai_ops import INITIAL_CAPITAL, run_daily_ai_ops
from .backtest import realistic_strategy_backtest
from . import db
from .config import DEFAULT_DB_PATH
from .finmind import fetch_finmind_institutional, fetch_finmind_kbar, fetch_recent_finmind_prices
from .fundamental import build_fundamental_analysis
from .indicators import is_new_high, macd, pct_change, rsi, sma, volume_ratio
from .industry import industry_profile
from .names import short_name
from .news import fetch_market_news, fetch_stock_news, headline_id
from .signals import load_signal_rows, rank_signals, risk_adjusted_score, score_stock


_FUNDAMENTAL_CACHE: dict[tuple[str, str | None], dict] = {}
_STRATEGY_CACHE: dict[str | None, dict] = {}
_AI_MONITOR_CACHE: dict[str, object] = {"expires_at": datetime.min, "data": None}
_PREMARKET_CACHE: dict[str, object] = {"expires_at": datetime.min, "data": None}
_LARGE_ORDER_CACHE: dict[str, object] = {"expires_at": datetime.min, "observed": {}}
_PUBLIC_FINANCIAL_CACHE: dict[str, object] = {}
_FUND_HOLDINGS_CSV = DEFAULT_DB_PATH.parent / "fund_holdings.csv"
_FINANCIAL_KPIS_CSV = DEFAULT_DB_PATH.parent / "financial_kpis.csv"


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
                elif parsed.path == "/api/industries":
                    self._send_json(api_industries(db_path, _param(params, "industry", ""), int(_param(params, "limit", "40"))))
                elif parsed.path == "/api/signals":
                    limit = int(_param(params, "limit", "20"))
                    self._send_json(api_signals(db_path, limit))
                elif parsed.path == "/api/strategy":
                    self._send_json(api_strategy(db_path))
                elif parsed.path == "/api/watch":
                    self._send_json(api_watch(db_path))
                elif parsed.path == "/api/premarket":
                    self._send_json(api_premarket(db_path, int(_param(params, "limit", "8")), _param(params, "force", "") == "1"))
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
                elif parsed.path == "/api/watchlist/reorder":
                    self._send_json(api_watchlist_reorder(db_path, _param(params, "codes", "")))
                elif parsed.path == "/api/watchlist/sync":
                    self._send_json(api_watchlist_sync(db_path, _param(params, "codes", "")))
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
                elif parsed.path == "/api/user/watchlist/reorder":
                    self._send_json(api_user_watchlist_reorder(db_path, _param(params, "user_key", ""), _param(params, "codes", "")))
                elif parsed.path == "/api/user/watchlist/sync":
                    self._send_json(api_user_watchlist_sync(db_path, _param(params, "user_key", ""), _param(params, "codes", "")))
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
                elif parsed.path == "/api/fund-holdings":
                    self._send_json(api_fund_holdings(db_path, _param(params, "code", "2330")))
                elif parsed.path == "/api/financial-kpis":
                    self._send_json(api_financial_kpis(db_path, _param(params, "code", "2330")))
                elif parsed.path == "/api/jobs/notify-users":
                    self._send_json(api_job_notify_users(db_path, _param(params, "token", ""), int(_param(params, "limit", "5"))))
                elif parsed.path == "/api/jobs/notify-users-intraday":
                    self._send_json(api_job_notify_users_intraday(db_path, _param(params, "token", ""), int(_param(params, "limit", "5"))))
                elif parsed.path == "/api/jobs/premarket-prepare":
                    self._send_json(api_job_premarket_prepare(db_path, _param(params, "token", ""), int(_param(params, "limit", "5"))))
                elif parsed.path == "/api/jobs/notify-users-premarket":
                    self._send_json(api_job_notify_users_premarket(db_path, _param(params, "token", ""), int(_param(params, "limit", "5"))))
                elif parsed.path == "/api/jobs/news-watch":
                    self._send_json(api_job_news_watch(db_path, _param(params, "token", ""), int(_param(params, "limit", "8"))))
                elif parsed.path == "/api/admin/users":
                    self._send_json(api_admin_users(db_path, _param(params, "token", "")))
                elif parsed.path == "/api/admin/telegram-webhook":
                    self._send_json(api_admin_telegram_webhook(_param(params, "token", ""), _param(params, "url", "")))
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


def _cloud_web_mode() -> bool:
    explicit = os.environ.get("STOCK_V1_CLOUD_WEB", "").strip().lower()
    if explicit:
        return explicit in {"1", "true", "yes", "on"}
    return DEFAULT_DB_PATH.name == "tw_stocks_deploy.sqlite"


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
                    "sparkline": _sparkline_values(closes),
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


def _market_cap_proxy(close: float | None, volume: float | None) -> float:
    return float(close or 0) * float(volume or 0)


def api_industries(db_path: Path, industry: str = "", limit: int = 40) -> dict:
    with _connect(db_path) as conn:
        meta_rows = conn.execute("SELECT code, industry FROM stocks").fetchall()
        industry_raw_by_code = {row["code"]: row["industry"] for row in meta_rows}
        rows_by_industry: dict[str, list[dict]] = {}
        for stock, rows in load_signal_rows(conn):
            if len(rows) < 2:
                continue
            closes = [row["close"] for row in rows if row["close"] is not None]
            volumes = [row["volume"] or 0 for row in rows]
            if len(closes) < 2:
                continue
            profile = industry_profile(stock["code"], stock["name"], industry_raw_by_code.get(stock["code"]))
            category = profile["category"] or "未分類"
            prev_close = closes[-2] if len(closes) >= 2 else None
            close = closes[-1]
            change = close - prev_close if prev_close not in (None, 0) else None
            change_percent = change / prev_close * 100 if change is not None and prev_close else None
            ma20 = sma(closes, 20)
            ma60 = sma(closes, 60)
            item = {
                "code": stock["code"],
                "name": stock["name"],
                "short_name": short_name(stock["name"]),
                "market": stock["market"],
                "industry": category,
                "date": rows[-1]["date"],
                "close": close,
                "price": close,
                "change": change,
                "change_percent": change_percent,
                "return_20d": pct_change(closes, 20),
                "return_60d": pct_change(closes, 60),
                "volume": volumes[-1] if volumes else None,
                "volume_ratio": volume_ratio(volumes),
                "rsi_14": rsi(closes, 14),
                "sparkline": _sparkline_values(closes),
                "new_high_60": is_new_high(closes, 60),
                "above_sma20": ma20 is not None and close > ma20,
                "above_sma60": ma60 is not None and close > ma60,
                "leader_score": _market_cap_proxy(close, volumes[-1] if volumes else None),
            }
            groups = [category, *(profile.get("themes") or [])]
            for group in dict.fromkeys(groups):
                rows_by_industry.setdefault(group, []).append({**item, "industry": group})
    industries = []
    for name, items in rows_by_industry.items():
        items.sort(key=lambda item: item["leader_score"], reverse=True)
        leader = items[0] if items else None
        avg_change = sum(float(item.get("change_percent") or 0) for item in items) / len(items) if items else None
        industries.append({
            "name": name,
            "count": len(items),
            "avg_change_percent": avg_change,
            "leader": leader,
        })
    industries.sort(key=lambda item: item["count"], reverse=True)
    selected = industry or (industries[0]["name"] if industries else "")
    selected_items = rows_by_industry.get(selected, [])
    if selected_items:
        leader_code = selected_items[0]["code"]
        selected_items = [selected_items[0]] + sorted(
            [item for item in selected_items[1:] if item["code"] != leader_code],
            key=lambda item: item.get("change_percent") if item.get("change_percent") is not None else -9999,
            reverse=True,
        )
    return {
        "industries": industries,
        "selected": selected,
        "items": selected_items[:limit],
        "summary": {
            "count": len(selected_items),
            "avg_change_percent": sum(float(item.get("change_percent") or 0) for item in selected_items) / len(selected_items) if selected_items else None,
            "leader": selected_items[0] if selected_items else None,
        },
    }


def api_signals(db_path: Path, limit: int = 20) -> dict:
    with _connect(db_path) as conn:
        payload = rank_signals(conn, limit)
        _attach_sparklines(conn, payload.get("top_signals", []))
        return payload


def api_strategy(db_path: Path) -> dict:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT MAX(date) AS latest FROM prices").fetchone()
        latest = row["latest"] if row else None
    strategy_mode = "cloud-backtest-v1" if _cloud_web_mode() else "daily-ai-ops-v1"
    cache_key = f"{latest}|{strategy_mode}|{INITIAL_CAPITAL}"
    if cache_key in _STRATEGY_CACHE:
        return _STRATEGY_CACHE[cache_key]
    with _connect(db_path) as conn:
        if _cloud_web_mode():
            result = _cloud_strategy_backtest(conn)
        else:
            result = run_daily_ai_ops(conn, INITIAL_CAPITAL)
        context = _strategy_market_context(conn, {"max_drawdown": result["summary"].get("max_drawdown")})
    is_cloud = _cloud_web_mode()
    payload = {
        "summary": result["summary"],
        "strategy": result.get("strategy", {}),
        "market_context": context,
        "curve": result["curve"],
        "recent_entries": [] if is_cloud else result.get("recent_entries", [])[-10:],
        "closed_trades": [] if is_cloud else result.get("closed_trades", [])[-10:],
        "recent_trades": [] if is_cloud else result.get("recent_trades", [])[-10:],
        "open_positions": [] if is_cloud else result.get("open_positions", []),
        "today_actions": [] if is_cloud else result.get("today_actions", []),
        "high_win_strategy": {},
    }
    if len(_STRATEGY_CACHE) > 8:
        _STRATEGY_CACHE.clear()
    _STRATEGY_CACHE[cache_key] = payload
    return payload


def _cloud_strategy_backtest(conn: sqlite3.Connection) -> dict:
    result = realistic_strategy_backtest(
        conn,
        max_positions=5,
        horizon=10,
        step=5,
        max_days=None,
        initial_capital=100.0,
        cost_bps=20,
        start_date=None,
    )
    summary = {
        "max_positions": result["max_positions"],
        "horizon": result["horizon"],
        "step": result["step"],
        "cost_bps": result["cost_bps"],
        "start_date": result["curve"][0]["date"] if result.get("curve") else result.get("start_date"),
        "tested_dates": result.get("tested_dates"),
        "trades": result["trades"],
        "initial_capital": result["initial_capital"],
        "final_capital": result["final_capital"],
        "total_return": result["total_return"],
        "win_rate": result["win_rate"],
        "avg_trade_return": result.get("avg_trade_return"),
        "median_trade_return": result["median_trade_return"],
        "max_drawdown": result["max_drawdown"],
        "best_trade": result.get("best_trade"),
        "worst_trade": result.get("worst_trade"),
    }
    return {
        "summary": summary,
        "strategy": {
            "name": "AI 風險調整動能策略",
            "description": "每日依 AI 訊號分數、趨勢、量能、RSI 與風險濾網排序，挑選分數最高且通過風控的股票建立等權組合。",
            "rules": [
                "股票池：上市櫃且至少具備 80 筆日線資料。",
                "進場：每 5 個交易日重新排序，最多持有 5 檔。",
                "排名：使用風險調整後 AI 分數，並參考原始分數與 20 日動能。",
                "風控：排除未通過風險濾網的標的，交易成本以單邊 20 bps 估算。",
                "出場：持有 10 個交易日後依回測價格出場，再重新分配資金。",
            ],
            "benchmark_note": "資金以 100 為基準指數化，不代表固定投入 20 萬。",
        },
        "curve": result["curve"],
        "recent_entries": result.get("recent_entries", []),
        "closed_trades": result.get("recent_trades", []),
        "recent_trades": result.get("recent_trades", []),
        "open_positions": result.get("open_position_details", []),
        "today_actions": [],
    }


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
                "sparkline": _sparkline_values(closes),
                "rsi_14": rsi(closes, 14),
                "volume_ratio": volume_ratio(volumes),
                "new_high_60": is_new_high(closes, 60),
                "above_sma20": ma20 is not None and closes[-1] > ma20,
                "above_sma60": ma60 is not None and closes[-1] > ma60,
                "signal": signal["signal"],
                "score": signal["risk_adjusted_score"],
                "entry_zone": signal.get("entry_zone"),
                "exit_zone": signal.get("exit_zone"),
                "stop": signal.get("stop"),
                "drawdown_pct": signal.get("drawdown_pct"),
                "reasons": signal.get("reasons", []),
                "cautions": signal.get("cautions", []),
            }
        )
    if not rows:
        return {
            "date": None,
            "stance": "資料不足",
            "position_suggestion": "暫不建立 AI 經理人部位",
            "headline": "目前資料不足，請先更新市場資料。",
            "actions": ["先執行資料更新，再重新載入 AI 經理人。"],
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
        headline = "市場廣度偏強，AI 經理人可以主動找多頭續航股。"
    elif breadth20 >= 0.45 and breadth60 >= 0.35:
        stance = "中性偏多"
        position = "建議 40% - 60% 研究資金，保留現金等待回測後的高勝率切入點"
        headline = "盤面仍有多方支撐，但不宜追高過度擴張部位。"
    elif breadth20 < 0.35 or breadth60 < 0.30:
        stance = "防守觀望"
        position = "建議 20% - 35% 研究資金，以觀察名單和停損控管為主"
        headline = "市場廣度偏弱，AI 經理人重點應放在防守與等待訊號轉強。"
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
        risks.append("AI 經理人歷史最大回撤偏大，建議把停損與持股上限放在第一優先。")
    if weak / total > 0.45:
        risks.append("弱勢訊號占比偏高，避免把資金平均分散到落後股。")

    actions = [
        "優先從 AI 訊號與觀察名單交集找標的，避免只看單日漲幅。",
        "進場分兩到三批，不用一次買滿，並以 20 日線或最近低點作為風控參考。",
        "若盤面跌破月線家數快速增加，AI 經理人自動降到防守模式。",
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


def _after_hours_industry_report(db_path: Path) -> dict:
    with _connect(db_path) as conn:
        raw_industry = {row["code"]: row["industry"] for row in conn.execute("SELECT code, industry FROM stocks").fetchall()}
        groups: dict[str, dict] = {}
        latest_date = None
        for stock, rows in load_signal_rows(conn):
            clean = [row for row in rows if row["close"] is not None]
            if len(clean) < 2:
                continue
            latest = clean[-1]
            previous = clean[-2]
            latest_date = max(latest_date or latest["date"], latest["date"])
            close = float(latest["close"])
            prev_close = float(previous["close"]) if previous["close"] not in (None, 0) else None
            change_percent = (close - prev_close) / prev_close * 100 if prev_close else 0
            closes = [float(row["close"]) for row in clean]
            volumes = [row["volume"] or 0 for row in clean]
            ret20 = pct_change(closes, 20) or 0
            volx = volume_ratio(volumes) or 0
            profile = industry_profile(stock["code"], stock["name"], raw_industry.get(stock["code"]))
            group_names = [profile["category"], *profile.get("themes", [])]
            for group_name in dict.fromkeys(group_names):
                group = groups.setdefault(
                    group_name,
                    {
                        "name": group_name,
                        "count": 0,
                        "advancers": 0,
                        "decliners": 0,
                        "sum_change": 0.0,
                        "sum_return_20d": 0.0,
                        "sum_volume_ratio": 0.0,
                        "items": [],
                    },
                )
                item = {
                    "code": stock["code"],
                    "name": short_name(stock["name"]),
                    "change_percent": change_percent,
                    "return_20d": ret20,
                    "volume_ratio": volx,
                    "close": close,
                    "leader_score": close * float(latest["volume"] or 0),
                }
                group["count"] += 1
                group["advancers"] += 1 if change_percent > 0 else 0
                group["decliners"] += 1 if change_percent < 0 else 0
                group["sum_change"] += change_percent
                group["sum_return_20d"] += ret20
                group["sum_volume_ratio"] += volx
                group["items"].append(item)

    reports = []
    for group in groups.values():
        count = max(1, group["count"])
        items = group["items"]
        leader = max(items, key=lambda row: row["leader_score"]) if items else {}
        strongest = max(items, key=lambda row: row["change_percent"]) if items else {}
        weakest = min(items, key=lambda row: row["change_percent"]) if items else {}
        avg_change = group["sum_change"] / count
        avg_return_20d = group["sum_return_20d"] / count
        avg_volume_ratio = group["sum_volume_ratio"] / count
        breadth = group["advancers"] / count * 100
        if avg_change >= 1.2 and breadth >= 60:
            stance = "類股強勢"
            action = "盤後列入明日優先觀察，開盤仍等量價確認。"
        elif avg_change <= -1.0 and breadth <= 40:
            stance = "類股轉弱"
            action = "降低追價，先檢查支撐、停損與是否有新聞利空。"
        elif avg_volume_ratio >= 1.8:
            stance = "量能異動"
            action = "資金正在集中，挑龍頭與最強個股，不追落後補漲。"
        else:
            stance = "中性整理"
            action = "觀察族群內強弱分化，等待隔日量能方向。"
        reports.append(
            {
                "name": group["name"],
                "count": group["count"],
                "advancers": group["advancers"],
                "decliners": group["decliners"],
                "breadth": breadth,
                "avg_change_percent": avg_change,
                "avg_return_20d": avg_return_20d,
                "avg_volume_ratio": avg_volume_ratio,
                "leader": leader,
                "strongest": strongest,
                "weakest": weakest,
                "stance": stance,
                "action": action,
            }
        )
    reports.sort(key=lambda row: (row["avg_change_percent"], row["avg_volume_ratio"], row["count"]), reverse=True)
    return {
        "date": latest_date,
        "top_groups": reports[:10],
        "volume_focus": sorted(reports, key=lambda row: row["avg_volume_ratio"], reverse=True)[:8],
        "weak_groups": sorted(reports, key=lambda row: row["avg_change_percent"])[:6],
        "summary": {
            "groups": len(reports),
            "strong_groups": sum(1 for row in reports if row["avg_change_percent"] >= 1),
            "weak_groups": sum(1 for row in reports if row["avg_change_percent"] <= -1),
        },
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
        "industry_after_report": _after_hours_industry_report(db_path),
    }


def api_premarket(db_path: Path, limit: int = 8, force: bool = False) -> dict:
    snapshot = _premarket_snapshot(force=force)
    codes = _watchlist_codes(db_path)
    rows = api_watchlist(db_path, ",".join(codes)).get("watchlist", [])[:limit]
    news = fetch_market_news(max_items=12)
    return {
        "snapshot": snapshot,
        "watchlist": [
            {
                **row,
                "premarket_bias": _premarket_watch_bias(row, snapshot),
                "premarket_action": _premarket_watch_action(row, snapshot),
            }
            for row in rows
        ],
        "news": news,
        "report": _build_premarket_report_text(db_path, "本機使用者", limit=limit),
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
                "message": "AI 盯盤只在台股盤中顯示。",
                **session,
            },
        }
    cached_until = _AI_MONITOR_CACHE.get("expires_at")
    if isinstance(cached_until, datetime) and datetime.now() < cached_until and _AI_MONITOR_CACHE.get("data"):
        result = dict(_AI_MONITOR_CACHE["data"])  # type: ignore[arg-type]
        result["summary"] = {**result.get("summary", {}), **session, "cached": True}
        return result
    result = build_ai_monitor(db_path)
    result["summary"] = {**result.get("summary", {}), **session}
    _AI_MONITOR_CACHE["data"] = result
    _AI_MONITOR_CACHE["expires_at"] = datetime.now() + timedelta(seconds=10)
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
        _ensure_financial_kpi_tables(conn)
        stock = conn.execute("SELECT code, name, market FROM stocks WHERE code = ?", (code,)).fetchone()
        if stock:
            _fetch_and_store_public_financial_kpis(conn, stock)
        row = conn.execute("SELECT MAX(date) AS latest FROM prices WHERE stock_code = ?", (code,)).fetchone()
        latest = row["latest"] if row else None
        kpi_row = conn.execute("SELECT MAX(updated_at) AS latest FROM financial_kpis WHERE stock_code = ?", (code,)).fetchone()
        kpi_latest = kpi_row["latest"] if kpi_row else None
    cache_key = (code, f"{latest}|{kpi_latest}")
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
    return {"public_demo": _public_demo_mode(), "cloud_web": _cloud_web_mode()}


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
        next_order = _next_watch_order(conn, "watchlist")
        conn.execute(
            """
            INSERT INTO watchlist (code, created_at, sort_order)
            VALUES (?, datetime('now'), ?)
            ON CONFLICT(code) DO UPDATE SET created_at = watchlist.created_at
            """,
            (code, next_order),
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


def api_watchlist_reorder(db_path: Path, raw_codes: str) -> dict:
    if _public_demo_mode():
        return {"error": "公開展示模式不會修改主機觀察名單，請使用瀏覽器本機排序。"}
    codes = _parse_code_list(raw_codes)
    if not codes:
        return {"error": "沒有可排序的股票代號。"}
    with _connect(db_path) as conn:
        _ensure_watchlist(conn)
        _apply_watchlist_order(conn, "watchlist", codes)
        conn.commit()
    return api_watchlist(db_path)


def api_watchlist_sync(db_path: Path, raw_codes: str) -> dict:
    if _public_demo_mode():
        return {"error": "公開展示模式不會修改主機推播名單。"}
    codes = _parse_code_list(raw_codes)
    if not codes:
        return {"error": "沒有可同步的股票代號。"}
    with _connect(db_path) as conn:
        _ensure_watchlist(conn)
        placeholders = ",".join("?" for _ in codes)
        valid_rows = conn.execute(f"SELECT code FROM stocks WHERE code IN ({placeholders})", codes).fetchall()
        valid_codes = {row["code"] for row in valid_rows}
        ordered_codes = [code for code in codes if code in valid_codes]
        if not ordered_codes:
            return {"error": "同步失敗：找不到有效股票代號。"}
        conn.execute("DELETE FROM watchlist")
        conn.executemany(
            "INSERT INTO watchlist (code, created_at, sort_order) VALUES (?, datetime('now'), ?)",
            [(code, index + 1) for index, code in enumerate(ordered_codes)],
        )
        conn.commit()
    data = api_watchlist(db_path)
    data["message"] = f"已同步 {len(data.get('watchlist', []))} 檔到本機推播關注名單。"
    return data


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
            "INSERT OR IGNORE INTO user_watchlist (user_key, code, created_at, sort_order) VALUES (?, ?, datetime('now'), ?)",
            [(user_key, code, index + 1) for index, code in enumerate(["2330", "2367", "2454"])],
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
        next_order = _next_watch_order(conn, "user_watchlist", user_key)
        conn.execute(
            "INSERT OR IGNORE INTO user_watchlist (user_key, code, created_at, sort_order) VALUES (?, ?, datetime('now'), ?)",
            (user_key, code, next_order),
        )
        conn.commit()
    return api_user_watchlist(db_path, user_key)


def api_user_watchlist_remove(db_path: Path, user_key: str, code: str) -> dict:
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        conn.execute("DELETE FROM user_watchlist WHERE user_key = ? AND code = ?", (user_key, code.strip()))
        conn.commit()
    return api_user_watchlist(db_path, user_key)


def api_user_watchlist_reorder(db_path: Path, user_key: str, raw_codes: str) -> dict:
    if not _user_row(db_path, user_key):
        return {"error": "找不到使用者，請先建立個人推播設定。"}
    codes = _parse_code_list(raw_codes)
    if not codes:
        return {"error": "沒有可排序的股票代號。"}
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        _apply_watchlist_order(conn, "user_watchlist", codes, user_key)
        conn.commit()
    return api_user_watchlist(db_path, user_key)


def api_user_watchlist_sync(db_path: Path, user_key: str, raw_codes: str) -> dict:
    codes = _parse_code_list(raw_codes)
    if not codes:
        return {"error": "沒有可同步的股票代號。"}
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        user = conn.execute("SELECT user_key FROM app_users WHERE user_key = ?", (user_key,)).fetchone()
        if not user:
            return {"error": "找不到使用者，請先建立個人推播設定。"}
        placeholders = ",".join("?" for _ in codes)
        valid_rows = conn.execute(f"SELECT code FROM stocks WHERE code IN ({placeholders})", codes).fetchall()
        valid_codes = {row["code"] for row in valid_rows}
        ordered_codes = [code for code in codes if code in valid_codes]
        if not ordered_codes:
            return {"error": "同步失敗：找不到有效股票代號。"}
        conn.execute("DELETE FROM user_watchlist WHERE user_key = ?", (user_key,))
        conn.executemany(
            "INSERT INTO user_watchlist (user_key, code, created_at, sort_order) VALUES (?, ?, datetime('now'), ?)",
            [(user_key, code, index + 1) for index, code in enumerate(ordered_codes)],
        )
        conn.execute("UPDATE app_users SET updated_at = datetime('now') WHERE user_key = ?", (user_key,))
        conn.commit()
    data = api_user_watchlist(db_path, user_key)
    data["message"] = f"已同步 {len(data.get('watchlist', []))} 檔到個人推播名單。"
    return data


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


def api_job_notify_users(db_path: Path, token: str, limit: int = 5) -> dict:
    ok, error = _job_authorized(token)
    if not ok:
        return {"error": error}
    return send_enabled_user_telegrams(db_path, limit=limit)


def api_job_notify_users_intraday(db_path: Path, token: str, limit: int = 5) -> dict:
    ok, error = _job_authorized(token)
    if not ok:
        return {"error": error}
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    allowed = now.weekday() < 5 and time(9, 0) <= now.time() <= time(14, 0)
    if not allowed:
        return {"skipped": True, "message": f"Not market time. Now={now:%Y-%m-%d %H:%M:%S} Asia/Taipei"}
    return send_enabled_user_intraday_telegrams(db_path, limit=limit)


def api_job_premarket_prepare(db_path: Path, token: str, limit: int = 5) -> dict:
    ok, error = _job_authorized(token)
    if not ok:
        return {"error": error}
    snapshot = _premarket_snapshot(force=True)
    return {
        "ok": True,
        "prepared_at": snapshot.get("prepared_at"),
        "stance": snapshot.get("stance"),
        "score": snapshot.get("score"),
        "items": len(snapshot.get("items") or []),
        "limit": limit,
    }


def api_job_notify_users_premarket(db_path: Path, token: str, limit: int = 5) -> dict:
    ok, error = _job_authorized(token)
    if not ok:
        return {"error": error}
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    allowed = now.weekday() < 5 and time(8, 20) <= now.time() <= time(8, 50)
    if not allowed:
        return {"skipped": True, "message": f"Premarket push is only allowed Mon-Fri 08:20-08:50 Asia/Taipei. Now={now:%Y-%m-%d %H:%M:%S}"}
    return send_enabled_user_premarket_telegrams(db_path, limit=limit)


def api_job_news_watch(db_path: Path, token: str, limit: int = 8) -> dict:
    ok, error = _job_authorized(token)
    if not ok:
        return {"error": error}
    return scan_breaking_news(db_path, limit=limit)


def api_admin_users(db_path: Path, token: str) -> dict:
    ok, error = _admin_authorized(token)
    if not ok:
        return {"error": error}
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        users = conn.execute(
            """
            SELECT user_key, display_name, telegram_chat_id, telegram_enabled, created_at, updated_at
            FROM app_users
            ORDER BY created_at DESC
            """
        ).fetchall()
        rows = []
        for user in users:
            codes = [
                row["code"]
                for row in conn.execute(
                    """
                    SELECT code
                    FROM user_watchlist
                    WHERE user_key = ?
                    ORDER BY COALESCE(sort_order, 999999), created_at, code
                    """,
                    (user["user_key"],),
                ).fetchall()
            ]
            rows.append(
                {
                    "user_key": user["user_key"],
                    "display_name": user["display_name"],
                    "telegram_enabled": bool(user["telegram_enabled"]),
                    "telegram_chat_id": _mask_secret(user["telegram_chat_id"] or ""),
                    "watchlist_count": len(codes),
                    "watchlist_codes": codes,
                    "created_at": user["created_at"],
                    "updated_at": user["updated_at"],
                }
            )
    return {
        "users": rows,
        "summary": {
            "total": len(rows),
            "telegram_enabled": sum(1 for row in rows if row["telegram_enabled"]),
            "with_watchlist": sum(1 for row in rows if row["watchlist_count"] > 0),
        },
    }


def api_admin_telegram_webhook(token: str, url: str) -> dict:
    ok, error = _admin_authorized(token)
    if not ok:
        return {"error": error}
    webhook_url = url.strip() or os.environ.get("STOCK_V1_TELEGRAM_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return {"error": "請輸入 webhook URL。"}
    result = set_telegram_webhook(webhook_url)
    info = _telegram_api("getWebhookInfo", {})
    return {"set_webhook": result, "webhook_info": info}


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


def send_enabled_user_intraday_telegrams(db_path: Path, limit: int = 5) -> dict:
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
            message = _build_user_intraday_message(db_path, user["user_key"], user["display_name"], limit=limit)
            _send_telegram_to_chat(user["telegram_chat_id"], message)
            sent += 1
        except Exception as exc:
            failures.append({"user_key": user["user_key"], "name": user["display_name"], "error": str(exc)})
    return {"users": len(users), "sent": sent, "failures": failures}


def send_enabled_user_premarket_telegrams(db_path: Path, limit: int = 5) -> dict:
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
            message = _build_user_premarket_message(db_path, user["user_key"], user["display_name"], limit=limit)
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
            f"你的個人代碼：{user['user_key']}\n"
            "你可以直接輸入股票代號，例如 2330。\n\n"
            + _telegram_help_text()
        )
    if lowered in {"/help", "help", "幫助", "說明"}:
        return _telegram_help_text()
    if lowered.startswith(("綁定", "bind", "/bind")):
        key = normalized.split(maxsplit=1)[1].strip() if len(normalized.split(maxsplit=1)) > 1 else ""
        result = _bind_telegram_to_user(db_path, key, chat_id, display_name)
        return result.get("message") or result.get("error") or "綁定完成。"

    user = _ensure_telegram_user(db_path, chat_id, display_name)
    code = _resolve_telegram_stock_code(db_path, normalized)

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
    order = {code: index for index, code in enumerate(codes)}
    with _connect(db_path) as conn:
        stocks = conn.execute(
            f"SELECT code, name, market FROM stocks WHERE code IN ({','.join('?' for _ in codes)})",
            codes,
        ).fetchall() if codes else []
    stocks = sorted(stocks, key=lambda row: order.get(row["code"], 999999))
    if not stocks:
        return {"quotes": [], "message": "沒有可查詢的股票代號"}

    twse_result = _fetch_twse_mis_quotes(stocks)
    twse_quotes = twse_result["quotes"]
    twse_by_code = {row["code"]: row for row in twse_quotes}
    if len(twse_quotes) == len(stocks):
        with _connect(db_path) as conn:
            quotes = [twse_by_code[stock["code"]] for stock in stocks if stock["code"] in twse_by_code]
            _attach_sparklines(conn, quotes, append_live=True)
        large_order_alerts = _detect_and_push_large_order_alerts(db_path, quotes)
        return {
            "quotes": quotes,
            "message": twse_result["message"],
            "source": "TWSE MIS",
            "large_order_alerts": large_order_alerts,
        }

    quotes = []
    failures = []
    with _connect(db_path) as conn:
        for stock in stocks:
            twse_quote = twse_by_code.get(stock["code"])
            if twse_quote:
                quotes.append(twse_quote)
                continue
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
                            "sparkline": _sparkline_for_code(conn, stock["code"], append_value=price),
                        }
                    )
                    continue
            except Exception as exc:
                failures.append(f"{stock['code']}: {exc}")

            local = _local_quote(conn, stock)
            if local:
                quotes.append(local)
        _attach_sparklines(conn, quotes, append_live=True)

    large_order_alerts = _detect_and_push_large_order_alerts(db_path, quotes)
    message = twse_result["message"] if twse_quotes else "證交所 MIS 即時報價暫不可用，已改用 FinMind TaiwanStockPrice。"
    if failures:
        message += " 部分股票已切回本機資料：" + "；".join(failures[:3])
    return {"quotes": quotes, "message": message, "source": "TWSE MIS + FinMind", "large_order_alerts": large_order_alerts}


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


def _fetch_twse_mis_quotes(stocks: list[sqlite3.Row]) -> dict:
    channels = []
    stock_by_code = {}
    for stock in stocks:
        market = str(stock["market"] or "").upper()
        exchange = "otc" if market in {"TPEX", "OTC"} else "tse"
        channels.append(f"{exchange}_{stock['code']}.tw")
        stock_by_code[stock["code"]] = stock
    if not channels:
        return {"quotes": [], "message": "沒有可查詢的股票代號"}
    url = (
        "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
        f"?ex_ch={'|'.join(channels)}&json=1&delay=0&_={int(datetime.now().timestamp() * 1000)}"
    )
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://mis.twse.com.tw/stock/fibest.jsp",
        },
    )
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    except Exception as exc:
        return {"quotes": [], "message": f"證交所 MIS 即時報價暫不可用：{exc}"}
    rows = payload.get("msgArray") or []
    quotes = []
    for item in rows:
        code = str(item.get("c") or "").strip()
        stock = stock_by_code.get(code)
        if not stock:
            continue
        quote = _parse_twse_mis_quote(item, stock)
        if quote:
            quotes.append(quote)
    query = payload.get("queryTime") or {}
    sys_date = query.get("sysDate") or ""
    sys_time = query.get("sysTime") or ""
    stamp = f"{sys_date} {sys_time}".strip()
    return {
        "quotes": quotes,
        "message": f"已使用證交所 MIS 即時報價更新。{stamp}",
    }


def _parse_twse_mis_quote(item: dict, stock: sqlite3.Row) -> dict | None:
    previous = _num(item.get("y"))
    bids = _price_levels(item.get("b"), item.get("g"))
    asks = _price_levels(item.get("a"), item.get("f"))
    last = _first_number(item.get("z"), item.get("pz"), item.get("b"), item.get("a"), item.get("o"), item.get("y"))
    if last is None:
        return None
    change = last - previous if previous not in (None, 0) else None
    change_percent = (change / previous * 100) if change is not None and previous else None
    date_text = str(item.get("d") or "")
    date_fmt = f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:]}" if len(date_text) == 8 else date_text
    return {
        "code": stock["code"],
        "name": short_name(stock["name"]),
        "market": stock["market"],
        "price": last,
        "change": change,
        "change_percent": change_percent,
        "volume": _num(item.get("v")),
        "date": date_fmt,
        "time": item.get("t") or item.get("%"),
        "open": _num(item.get("o")),
        "high": _num(item.get("h")),
        "low": _num(item.get("l")),
        "previous_close": previous,
        "last_qty": _num(item.get("tv")) or _num(item.get("q")),
        "bids": bids,
        "asks": asks,
        "source": "證交所 MIS",
    }


def _price_levels(price_text: str | None, qty_text: str | None) -> list[dict]:
    prices = _split_numbers(price_text)
    qtys = _split_numbers(qty_text)
    return [
        {"price": price, "qty": qtys[index] if index < len(qtys) else None}
        for index, price in enumerate(prices)
        if price is not None and price > 0
    ]


def _split_numbers(text: str | None) -> list[float]:
    return [_num(part) for part in str(text or "").split("_") if _num(part) is not None]


def _first_number(*values) -> float | None:
    for value in values:
        if isinstance(value, str) and "_" in value:
            for part in value.split("_"):
                parsed = _num(part)
                if parsed is not None and parsed > 0:
                    return parsed
            continue
        parsed = _num(value)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _num(value) -> float | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


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
    official_dates = sorted(row["date"] for row in price_dates) if price_dates else []
    try:
        raw = fetch_finmind_institutional(code, start, end)
        rows = _group_finmind_institutional(raw)
        if len(rows) < 5 and stock.get("market") == "TWSE":
            official_rows = _fetch_twse_t86_institutional(code, official_dates[-45:])
            if official_rows:
                rows = official_rows
                source = "TWSE T86 官方三大法人買賣超"
            else:
                source = "FinMind TaiwanStockInstitutionalInvestorsBuySell"
        else:
            source = "FinMind TaiwanStockInstitutionalInvestorsBuySell"
        return {
            "code": code,
            "name": stock.get("short_name") or stock.get("name"),
            "source": source,
            "message": "外資、投信、自營商為公開法人買賣超；散戶為法人反向代理值。",
            "rows": rows,
            "summary": _institutional_summary(rows),
        }
    except Exception as exc:
        official_rows = _fetch_twse_t86_institutional(code, official_dates[-45:]) if stock.get("market") == "TWSE" else []
        if official_rows:
            return {
                "code": code,
                "name": stock.get("short_name") or stock.get("name"),
                "source": "TWSE T86 官方三大法人買賣超",
                "message": f"FinMind 法人資料不可用，已改用證交所 T86 官方資料。原因：{exc}",
                "rows": official_rows,
                "summary": _institutional_summary(official_rows),
            }
        return {"code": code, "name": stock.get("short_name") or stock.get("name"), "rows": [], "summary": {}, "message": f"法人資料暫不可用：{exc}"}


def _group_finmind_institutional(raw: list[dict]) -> list[dict]:
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
        item["total"] = institutional_net
        item["retail_proxy"] = -institutional_net
        rows.append(item)
    return rows


def _fetch_twse_t86_institutional(code: str, dates: list[str]) -> list[dict]:
    rows = []
    for day in dates:
        ymd = day.replace("-", "")
        url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={ymd}&selectType=ALLBUT0999&response=json"
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8", errors="ignore"))
        except Exception:
            continue
        if payload.get("stat") != "OK":
            continue
        for item in payload.get("data") or []:
            if not item or str(item[0]).strip() != code:
                continue
            foreign = _int_text(item[4]) + _int_text(item[7])
            investment = _int_text(item[10])
            dealer = _int_text(item[11])
            total = _int_text(item[18]) if len(item) > 18 else foreign + investment + dealer
            rows.append(
                {
                    "date": day,
                    "foreign": foreign,
                    "investment": investment,
                    "dealer": dealer,
                    "total": total,
                    "retail_proxy": -total,
                }
            )
            break
    return rows


def _institutional_summary(rows: list[dict]) -> dict:
    recent = rows[-20:]
    last = rows[-1] if rows else {}
    totals = {
        key: sum(float(row.get(key) or 0) for row in recent)
        for key in ["foreign", "investment", "dealer", "total", "retail_proxy"]
    }
    streak = 0
    for row in reversed(rows):
        value = float(row.get("total") or 0)
        if value > 0 and streak >= 0:
            streak += 1
        elif value < 0 and streak <= 0:
            streak -= 1
        else:
            break
    stance = "法人偏買" if totals["total"] > 0 else "法人偏賣" if totals["total"] < 0 else "法人中性"
    return {"last": last, "twenty_day": totals, "streak": streak, "stance": stance}


def _int_text(value) -> int:
    if value in (None, "", "-", "--"):
        return 0
    try:
        return int(str(value).replace(",", "").strip())
    except ValueError:
        return 0


def api_fund_holdings(db_path: Path, code: str) -> dict:
    code = code.strip()
    with _connect(db_path) as conn:
        _ensure_fund_holding_tables(conn)
        imported = _import_fund_holdings_csv(conn, _FUND_HOLDINGS_CSV)
        stock = conn.execute("SELECT code, name FROM stocks WHERE code = ?", (code,)).fetchone()
        rows = conn.execute(
            """
            SELECT fund_name, fund_type, manager, report_date, stock_code, shares,
                   market_value, weight, source, summary
            FROM fund_holdings
            WHERE stock_code = ?
            ORDER BY fund_name, report_date
            """,
            (code,),
        ).fetchall()
    by_fund: dict[str, list[dict]] = {}
    for row in rows:
        by_fund.setdefault(row["fund_name"], []).append(dict(row))
    items = []
    for fund_name, fund_rows in by_fund.items():
        fund_rows.sort(key=lambda item: item["report_date"] or "")
        latest = fund_rows[-1]
        previous = fund_rows[-2] if len(fund_rows) > 1 else None
        latest_shares = float(latest.get("shares") or 0)
        previous_shares = float(previous.get("shares") or 0) if previous else None
        change = latest_shares - previous_shares if previous is not None else None
        action = "新增揭露" if previous is None else "加碼" if change and change > 0 else "減碼" if change and change < 0 else "持平"
        items.append(
            {
                **latest,
                "previous_date": previous.get("report_date") if previous else None,
                "previous_shares": previous_shares,
                "share_change": change,
                "action": action,
            }
        )
    items.sort(key=lambda item: abs(float(item.get("share_change") or item.get("shares") or 0)), reverse=True)
    total_shares = sum(float(item.get("shares") or 0) for item in items)
    total_change = sum(float(item.get("share_change") or 0) for item in items if item.get("share_change") is not None)
    return {
        "code": code,
        "name": short_name(stock["name"]) if stock else code,
        "csv_path": str(_FUND_HOLDINGS_CSV),
        "imported": imported,
        "items": items,
        "summary": {
            "funds": len(items),
            "total_shares": total_shares,
            "total_change": total_change,
            "latest_date": max((item.get("report_date") or "" for item in items), default=None),
        },
        "message": (
            "已依單一基金持股明細計算各基金持有與前期增減。"
            if items
            else "官方免費公開端點目前沒有單一基金逐檔持股明細；系統已先用外資、投信、自營商買賣超補齊資金面，若取得基金月報或持股揭露檔可再匯入。"
        ),
    }


def _ensure_fund_holding_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fund_holdings (
            fund_name TEXT NOT NULL,
            fund_type TEXT NOT NULL,
            manager TEXT,
            report_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            shares REAL,
            market_value REAL,
            weight REAL,
            source TEXT,
            summary TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (fund_name, report_date, stock_code)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fund_holdings_stock ON fund_holdings(stock_code, report_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fund_holdings_fund ON fund_holdings(fund_name, report_date)")
    conn.commit()


def _import_fund_holdings_csv(conn: sqlite3.Connection, path: Path) -> dict:
    if not path.exists():
        return {"loaded": False, "rows": 0, "path": str(path)}
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            report_date = (raw.get("report_date") or raw.get("日期") or "").strip()
            fund_name = (raw.get("fund_name") or raw.get("基金名稱") or "").strip()
            stock_code = (raw.get("stock_code") or raw.get("股票代號") or "").strip()
            if not report_date or not fund_name or not stock_code:
                continue
            rows.append(
                {
                    "fund_name": fund_name,
                    "fund_type": (raw.get("fund_type") or raw.get("基金類型") or "未分類").strip(),
                    "manager": (raw.get("manager") or raw.get("經理公司") or "").strip(),
                    "report_date": report_date,
                    "stock_code": stock_code,
                    "shares": _num(raw.get("shares") or raw.get("持股股數")),
                    "market_value": _num(raw.get("market_value") or raw.get("持股市值")),
                    "weight": _num(raw.get("weight") or raw.get("權重")),
                    "source": (raw.get("source") or raw.get("資料來源") or "").strip(),
                    "summary": (raw.get("summary") or raw.get("分析摘要") or "").strip(),
                }
            )
    if rows:
        conn.executemany(
            """
            INSERT INTO fund_holdings (
                fund_name, fund_type, manager, report_date, stock_code, shares,
                market_value, weight, source, summary, updated_at
            )
            VALUES (
                :fund_name, :fund_type, :manager, :report_date, :stock_code, :shares,
                :market_value, :weight, :source, :summary, datetime('now')
            )
            ON CONFLICT(fund_name, report_date, stock_code) DO UPDATE SET
                fund_type = excluded.fund_type,
                manager = excluded.manager,
                shares = excluded.shares,
                market_value = excluded.market_value,
                weight = excluded.weight,
                source = excluded.source,
                summary = excluded.summary,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        conn.commit()
    return {"loaded": True, "rows": len(rows), "path": str(path)}


def api_financial_kpis(db_path: Path, code: str) -> dict:
    code = code.strip()
    with _connect(db_path) as conn:
        _ensure_financial_kpi_tables(conn)
        imported = _import_financial_kpis_csv(conn, _FINANCIAL_KPIS_CSV)
        stock = conn.execute("SELECT code, name, market FROM stocks WHERE code = ?", (code,)).fetchone()
        auto = _fetch_and_store_public_financial_kpis(conn, stock) if stock else {"loaded": False, "rows": 0, "message": "找不到股票基本資料。"}
        company_context = _fetch_public_company_context(stock) if stock else {}
        rows = conn.execute(
            """
            SELECT stock_code, period, revenue, revenue_yoy, eps, gross_margin,
                   operating_margin, roe, pe, pb, dividend_yield, source, summary
            FROM financial_kpis
            WHERE stock_code = ?
            ORDER BY period
            """,
            (code,),
        ).fetchall()
    items = [dict(row) for row in rows]
    latest = items[-1] if items else None
    return {
        "code": code,
        "csv_path": str(_FINANCIAL_KPIS_CSV),
        "imported": imported,
        "auto_fetch": auto,
        "company_context": company_context,
        "items": items,
        "latest": latest,
        "message": (
            auto.get("message") or "已依公開資料與匯入資料補齊財報與估值 KPI。"
            if latest
            else "公開財報端點暫時沒有回傳資料；若仍缺特定欄位，可再補 data/financial_kpis.csv。"
        ),
    }


def _ensure_financial_kpi_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS financial_kpis (
            stock_code TEXT NOT NULL,
            period TEXT NOT NULL,
            revenue REAL,
            revenue_yoy REAL,
            eps REAL,
            gross_margin REAL,
            operating_margin REAL,
            roe REAL,
            pe REAL,
            pb REAL,
            dividend_yield REAL,
            source TEXT,
            summary TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (stock_code, period)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_financial_kpis_stock ON financial_kpis(stock_code, period)")
    conn.commit()


def _import_financial_kpis_csv(conn: sqlite3.Connection, path: Path) -> dict:
    if not path.exists():
        return {"loaded": False, "rows": 0, "path": str(path)}
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            period = (raw.get("period") or raw.get("期間") or raw.get("年月") or "").strip()
            stock_code = (raw.get("stock_code") or raw.get("股票代號") or "").strip()
            if not period or not stock_code:
                continue
            rows.append(
                {
                    "stock_code": stock_code,
                    "period": period,
                    "revenue": _num(raw.get("revenue") or raw.get("營收")),
                    "revenue_yoy": _num(raw.get("revenue_yoy") or raw.get("營收年增率")),
                    "eps": _num(raw.get("eps") or raw.get("EPS")),
                    "gross_margin": _num(raw.get("gross_margin") or raw.get("毛利率")),
                    "operating_margin": _num(raw.get("operating_margin") or raw.get("營業利益率")),
                    "roe": _num(raw.get("roe") or raw.get("ROE")),
                    "pe": _num(raw.get("pe") or raw.get("本益比")),
                    "pb": _num(raw.get("pb") or raw.get("股價淨值比")),
                    "dividend_yield": _num(raw.get("dividend_yield") or raw.get("殖利率")),
                    "source": (raw.get("source") or raw.get("資料來源") or "").strip(),
                    "summary": (raw.get("summary") or raw.get("分析摘要") or "").strip(),
                }
            )
    if rows:
        conn.executemany(
            """
            INSERT INTO financial_kpis (
                stock_code, period, revenue, revenue_yoy, eps, gross_margin,
                operating_margin, roe, pe, pb, dividend_yield, source, summary, updated_at
            )
            VALUES (
                :stock_code, :period, :revenue, :revenue_yoy, :eps, :gross_margin,
                :operating_margin, :roe, :pe, :pb, :dividend_yield, :source, :summary, datetime('now')
            )
            ON CONFLICT(stock_code, period) DO UPDATE SET
                revenue = excluded.revenue,
                revenue_yoy = excluded.revenue_yoy,
                eps = excluded.eps,
                gross_margin = excluded.gross_margin,
                operating_margin = excluded.operating_margin,
                roe = excluded.roe,
                pe = excluded.pe,
                pb = excluded.pb,
                dividend_yield = excluded.dividend_yield,
                source = excluded.source,
                summary = excluded.summary,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        conn.commit()
    return {"loaded": True, "rows": len(rows), "path": str(path)}


def _public_financial_endpoints(market: str | None) -> dict[str, str]:
    market_text = str(market or "").upper()
    if market_text in {"TPEX", "OTC"}:
        return {
            "valuation": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis",
            "revenue": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
            "income": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci",
            "balance": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_ci",
            "profitability": "https://www.tpex.org.tw/openapi/v1/mopsfin_187ap17_O",
            "governance": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap11_O",
            "major_shareholders": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap02_O",
            "dividend": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap39_O",
            "forecast": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap15_O",
            "control_change": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap24_O",
            "source": "TPEx OpenAPI",
        }
    return {
        "valuation": "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL",
        "revenue": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
        "income": "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci",
        "balance": "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_ci",
        "profitability": "https://openapi.twse.com.tw/v1/opendata/t187ap17_L",
        "governance": "https://openapi.twse.com.tw/v1/opendata/t187ap11_L",
        "major_shareholders": "https://openapi.twse.com.tw/v1/opendata/t187ap02_L",
        "dividend": "https://openapi.twse.com.tw/v1/opendata/t187ap45_L",
        "forecast": "https://openapi.twse.com.tw/v1/opendata/t187ap15_L",
        "control_change": "https://openapi.twse.com.tw/v1/opendata/t187ap24_L",
        "source": "TWSE OpenAPI",
    }


def _cached_public_json(url: str) -> list[dict]:
    cached = _PUBLIC_FINANCIAL_CACHE.get(url)
    now = datetime.now()
    if isinstance(cached, dict) and cached.get("expires_at") and cached["expires_at"] > now:
        return cached.get("rows") or []
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    rows = payload if isinstance(payload, list) else []
    _PUBLIC_FINANCIAL_CACHE[url] = {"expires_at": now + timedelta(hours=6), "rows": rows}
    return rows


def _find_public_stock_row(rows: list[dict], code: str) -> dict:
    code = str(code).strip()
    for row in rows:
        if str(row.get("公司代號") or row.get("Code") or row.get("SecuritiesCompanyCode") or "").strip() == code:
            return row
    return {}


def _public_stock_rows(rows: list[dict], code: str) -> list[dict]:
    code = str(code).strip()
    return [
        row for row in rows
        if str(row.get("公司代號") or row.get("Code") or row.get("SecuritiesCompanyCode") or "").strip() == code
    ]


def _roc_year_text(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        year = int(text[:3] if len(text) >= 3 else text)
    except ValueError:
        return text
    if year < 1911:
        year += 1911
    return str(year)


def _public_period(income_row: dict, revenue_row: dict) -> str:
    year = _roc_year_text(income_row.get("年度") or income_row.get("Year"))
    season = str(income_row.get("季別") or income_row.get("Season") or "").strip()
    if year and season:
        return f"{year}Q{season}"
    ym = str(revenue_row.get("資料年月") or "").strip()
    if len(ym) >= 5:
        year = _roc_year_text(ym[:3])
        return f"{year}-{ym[3:]}"
    return date.today().strftime("%Y-%m")


def _ratio(numerator, denominator, multiplier: float = 100.0) -> float | None:
    num = _num(numerator)
    den = _num(denominator)
    if num is None or den in (None, 0):
        return None
    return num / den * multiplier


def _fetch_public_company_context(stock: sqlite3.Row) -> dict:
    endpoints = _public_financial_endpoints(stock["market"])
    code = stock["code"]

    def rows_for(key: str) -> list[dict]:
        url = endpoints.get(key)
        if not url:
            return []
        try:
            return _public_stock_rows(_cached_public_json(url), code)
        except Exception:
            return []

    governance_rows = rows_for("governance")
    major_rows = rows_for("major_shareholders")
    dividend_rows = rows_for("dividend")
    forecast_rows = rows_for("forecast")
    control_rows = rows_for("control_change")
    profitability_rows = rows_for("profitability")

    pledged_ratios = [_num(row.get("設質股數佔持股比例")) for row in governance_rows]
    pledged_ratios = [value for value in pledged_ratios if value is not None]
    holding_values = [_num(row.get("目前持股")) for row in governance_rows]
    holding_total = sum(value for value in holding_values if value is not None)
    dividend_latest = sorted(dividend_rows, key=lambda row: str(row.get("股利年度") or row.get("Year") or row.get("年度") or ""))[-1] if dividend_rows else {}
    cash_dividend = (
        _num(dividend_latest.get("股東配發-盈餘分配之現金股利(元/股)"))
        or _num(dividend_latest.get("股東配發-法定盈餘公積發放之現金(元/股)"))
        or _num(dividend_latest.get("股東配發-資本公積發放之現金(元/股)"))
        or _num(dividend_latest.get("CashDividend"))
    )
    profit_latest = profitability_rows[-1] if profitability_rows else {}
    return {
        "source": endpoints["source"],
        "governance": {
            "rows": len(governance_rows),
            "holding_total": holding_total,
            "avg_pledge_ratio": sum(pledged_ratios) / len(pledged_ratios) if pledged_ratios else None,
            "latest_month": (governance_rows[-1].get("資料年月") if governance_rows else None),
        },
        "major_shareholders": {
            "rows": len(major_rows),
            "top": major_rows[:5],
        },
        "dividend": {
            "rows": len(dividend_rows),
            "year": dividend_latest.get("股利年度") or dividend_latest.get("Year") or dividend_latest.get("年度"),
            "cash_dividend": cash_dividend,
            "status": dividend_latest.get("決議（擬議）進度") or dividend_latest.get("Status"),
        },
        "forecast": {
            "rows": len(forecast_rows),
            "has_forecast": len(forecast_rows) > 0,
        },
        "control_change": {
            "rows": len(control_rows),
            "has_recent_change": len(control_rows) > 0,
            "latest": control_rows[-1] if control_rows else {},
        },
        "profitability": {
            "rows": len(profitability_rows),
            "gross_margin": _num(profit_latest.get("毛利率(%)(營業毛利)/(營業收入)")),
            "operating_margin": _num(profit_latest.get("營業利益率(%)(營業利益)/(營業收入)")),
            "net_margin": _num(profit_latest.get("稅後純益率(%)(稅後純益)/(營業收入)")),
        },
    }


def _fetch_and_store_public_financial_kpis(conn: sqlite3.Connection, stock: sqlite3.Row) -> dict:
    endpoints = _public_financial_endpoints(stock["market"])
    code = stock["code"]
    errors = []
    try:
        valuation_row = _find_public_stock_row(_cached_public_json(endpoints["valuation"]), code)
    except Exception as exc:
        valuation_row = {}
        errors.append(f"估值：{exc}")
    try:
        revenue_row = _find_public_stock_row(_cached_public_json(endpoints["revenue"]), code)
    except Exception as exc:
        revenue_row = {}
        errors.append(f"月營收：{exc}")
    try:
        income_row = _find_public_stock_row(_cached_public_json(endpoints["income"]), code)
    except Exception as exc:
        income_row = {}
        errors.append(f"損益表：{exc}")
    try:
        balance_row = _find_public_stock_row(_cached_public_json(endpoints["balance"]), code)
    except Exception as exc:
        balance_row = {}
        errors.append(f"資產負債表：{exc}")
    if not any([valuation_row, revenue_row, income_row, balance_row]):
        return {"loaded": False, "rows": 0, "message": "公開財報端點沒有可用資料。" + ("；".join(errors[:2]) if errors else "")}

    revenue = _num(income_row.get("營業收入")) or _num(revenue_row.get("營業收入-當月營收"))
    gross_margin = _ratio(income_row.get("營業毛利（毛損）") or income_row.get("營業毛利（毛損）淨額"), revenue)
    operating_margin = _ratio(income_row.get("營業利益（損失）"), revenue)
    equity = _num(balance_row.get("歸屬於母公司業主之權益合計")) or _num(balance_row.get("權益總額")) or _num(balance_row.get("權益總計"))
    net_income = _num(income_row.get("淨利（淨損）歸屬於母公司業主")) or _num(income_row.get("本期淨利（淨損）"))
    roe = _ratio(net_income, equity)
    pe = _num(valuation_row.get("PEratio") or valuation_row.get("PriceEarningRatio"))
    pb = _num(valuation_row.get("PBratio") or valuation_row.get("PriceBookRatio"))
    dividend_yield = _num(valuation_row.get("DividendYield") or valuation_row.get("YieldRatio"))
    period = _public_period(income_row, revenue_row)
    source = endpoints["source"]
    summary = (
        f"自動抓取 {source}：最新季 EPS {fmt_value(_num(income_row.get('基本每股盈餘（元）')))}，"
        f"毛利率 {fmt_value(gross_margin)}%，營益率 {fmt_value(operating_margin)}%，"
        f"PE {fmt_value(pe)}，PB {fmt_value(pb)}。"
    )
    row = {
        "stock_code": code,
        "period": period,
        "revenue": revenue,
        "revenue_yoy": _num(revenue_row.get("營業收入-去年同月增減(%)") or revenue_row.get("累計營業收入-前期比較增減(%)")),
        "eps": _num(income_row.get("基本每股盈餘（元）")),
        "gross_margin": gross_margin,
        "operating_margin": operating_margin,
        "roe": roe,
        "pe": pe,
        "pb": pb,
        "dividend_yield": dividend_yield,
        "source": source,
        "summary": summary,
    }
    conn.execute(
        """
        INSERT INTO financial_kpis (
            stock_code, period, revenue, revenue_yoy, eps, gross_margin,
            operating_margin, roe, pe, pb, dividend_yield, source, summary, updated_at
        )
        VALUES (
            :stock_code, :period, :revenue, :revenue_yoy, :eps, :gross_margin,
            :operating_margin, :roe, :pe, :pb, :dividend_yield, :source, :summary, datetime('now')
        )
        ON CONFLICT(stock_code, period) DO UPDATE SET
            revenue = COALESCE(excluded.revenue, financial_kpis.revenue),
            revenue_yoy = COALESCE(excluded.revenue_yoy, financial_kpis.revenue_yoy),
            eps = COALESCE(excluded.eps, financial_kpis.eps),
            gross_margin = COALESCE(excluded.gross_margin, financial_kpis.gross_margin),
            operating_margin = COALESCE(excluded.operating_margin, financial_kpis.operating_margin),
            roe = COALESCE(excluded.roe, financial_kpis.roe),
            pe = COALESCE(excluded.pe, financial_kpis.pe),
            pb = COALESCE(excluded.pb, financial_kpis.pb),
            dividend_yield = COALESCE(excluded.dividend_yield, financial_kpis.dividend_yield),
            source = excluded.source,
            summary = excluded.summary,
            updated_at = excluded.updated_at
        """,
        row,
    )
    conn.commit()
    return {
        "loaded": True,
        "rows": 1,
        "source": source,
        "message": f"已自動抓取 {source} 補齊月營收、季 EPS、毛利率、營益率、ROE、PE/PB 與殖利率。",
        "warnings": errors,
    }


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
        "sparkline": _sparkline_for_code(conn, stock["code"]),
    }


def _detect_and_push_large_order_alerts(db_path: Path, quotes: list[dict]) -> list[dict]:
    session = _market_session()
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    if not (session["is_intraday"] or (now.weekday() < 5 and time(9, 0) <= now.time() <= time(13, 35))):
        return []
    try:
        alerts = _detect_large_order_alerts(db_path, quotes)
    except Exception:
        return []
    if not alerts:
        return []
    try:
        _push_owner_large_order_alerts(alerts)
    except Exception as exc:
        for alert in alerts:
            alert["push_error"] = str(exc)
    return alerts


def _detect_large_order_alerts(db_path: Path, quotes: list[dict]) -> list[dict]:
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    observed_cache = _large_order_observed_cache()
    alerts = []
    with _connect(db_path) as conn:
        _ensure_large_order_tables(conn)
        avg_lots = _avg_daily_lots(conn, [str(row.get("code")) for row in quotes if row.get("code")])
        for quote in quotes:
            code = str(quote.get("code") or "")
            if not code:
                continue
            threshold = _large_order_threshold_lots(float(quote.get("price") or 0), avg_lots.get(code, 0))
            levels = []
            for level in quote.get("bids") or []:
                levels.append(("買方大單", "bid", level))
            for level in quote.get("asks") or []:
                levels.append(("賣方大單", "ask", level))
            for label, side, level in levels:
                price = level.get("price")
                qty = float(level.get("qty") or 0)
                if price is None or qty <= 0:
                    continue
                key = f"{code}:{side}:{price}"
                previous_qty = float(observed_cache.get(key, 0) or 0)
                if previous_qty <= 0:
                    row = conn.execute(
                        """
                        SELECT qty
                        FROM large_order_observations
                        WHERE code = ? AND side = ? AND price = ?
                        """,
                        (code, side, float(price)),
                    ).fetchone()
                    previous_qty = float(row["qty"] or 0) if row else 0
                observed_cache[key] = qty
                conn.execute(
                    """
                    INSERT INTO large_order_observations (code, side, price, qty, observed_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(code, side, price) DO UPDATE SET
                        qty = excluded.qty,
                        observed_at = excluded.observed_at
                    """,
                    (code, side, float(price), qty),
                )
                is_large = qty >= threshold
                is_sudden = previous_qty <= 0 and qty >= threshold * 1.5
                if previous_qty > 0:
                    is_sudden = qty >= threshold and qty >= max(previous_qty * 2.0, previous_qty + max(50, threshold * 0.3))
                if not (is_large and is_sudden):
                    continue
                last_alert = conn.execute(
                    """
                    SELECT alerted_at
                    FROM large_order_alerts
                    WHERE code = ? AND side = ? AND price = ?
                    ORDER BY alerted_at DESC
                    LIMIT 1
                    """,
                    (code, side, float(price)),
                ).fetchone()
                if last_alert and _minutes_since(last_alert["alerted_at"], now) < 10:
                    continue
                alert = {
                    "code": code,
                    "name": quote.get("name") or code,
                    "side": side,
                    "label": label,
                    "price": float(price),
                    "qty": qty,
                    "previous_qty": previous_qty,
                    "threshold": threshold,
                    "last_price": quote.get("price"),
                    "change_percent": quote.get("change_percent"),
                    "time": quote.get("time") or now.strftime("%H:%M:%S"),
                    "source": quote.get("source") or "",
                    "detected_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                }
                conn.execute(
                    """
                    INSERT INTO large_order_alerts (
                        code, name, side, price, qty, previous_qty, threshold_lots, last_price,
                        change_percent, source, alerted_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        alert["code"],
                        alert["name"],
                        alert["side"],
                        alert["price"],
                        alert["qty"],
                        alert["previous_qty"],
                        alert["threshold"],
                        alert["last_price"],
                        alert["change_percent"],
                        alert["source"],
                    ),
                )
                alerts.append(alert)
        conn.commit()
    return alerts


def _large_order_observed_cache() -> dict:
    now = datetime.now()
    if not isinstance(_LARGE_ORDER_CACHE.get("expires_at"), datetime) or now >= _LARGE_ORDER_CACHE["expires_at"]:
        _LARGE_ORDER_CACHE["observed"] = {}
        _LARGE_ORDER_CACHE["expires_at"] = now + timedelta(minutes=45)
    return _LARGE_ORDER_CACHE["observed"]  # type: ignore[return-value]


def _large_order_threshold_lots(price: float, avg_daily_lots: float) -> float:
    manual = os.environ.get("STOCK_V1_LARGE_ORDER_LOTS", "").strip()
    if manual:
        try:
            return max(1, float(manual))
        except ValueError:
            pass
    if price >= 500:
        floor = 50
    elif price >= 100:
        floor = 100
    else:
        floor = 300
    dynamic = avg_daily_lots * 0.015 if avg_daily_lots else 0
    return round(max(floor, dynamic), 0)


def _avg_daily_lots(conn: sqlite3.Connection, codes: list[str]) -> dict[str, float]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"""
        SELECT stock_code, AVG(volume) AS avg_volume
        FROM (
            SELECT stock_code, volume
            FROM prices
            WHERE stock_code IN ({placeholders}) AND volume IS NOT NULL
            ORDER BY stock_code, date DESC
        )
        GROUP BY stock_code
        """,
        codes,
    ).fetchall()
    return {row["stock_code"]: float(row["avg_volume"] or 0) / 1000 for row in rows}


def _minutes_since(timestamp: str, now: datetime) -> float:
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace(" ", "T"))
    except ValueError:
        return 9999
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Taipei"))
    return (now - parsed.astimezone(ZoneInfo("Asia/Taipei"))).total_seconds() / 60


def _push_owner_large_order_alerts(alerts: list[dict]) -> None:
    chat_ids = _owner_telegram_chat_ids()
    if not chat_ids:
        return
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    lines = [
        f"台股智研｜即時大單警報 {now:%H:%M:%S}",
        f"偵測到 {len(alerts)} 筆買/賣方大單突然放大，請回即時看盤確認成交明細與五檔是否延續。",
    ]
    for alert in alerts[:8]:
        side_note = "買盤加大" if alert.get("side") == "bid" else "賣盤加大"
        lines.extend(
            [
                "",
                f"{alert.get('code')} {alert.get('name')}｜{alert.get('label')}｜{side_note}",
                f"掛價 {fmt_value(alert.get('price'))}｜張數 {fmt_value(alert.get('qty'), 0)}｜前次 {fmt_value(alert.get('previous_qty'), 0)}｜門檻 {fmt_value(alert.get('threshold'), 0)}",
                f"現價 {fmt_value(alert.get('last_price'))}｜漲跌幅 {fmt_value(alert.get('change_percent'))}%｜資料時間 {alert.get('time', '-')}",
            ]
        )
    if len(alerts) > 8:
        lines.append(f"\n另有 {len(alerts) - 8} 筆大單先省略，請回即時看盤查看。")
    lines.append("\n提醒：大單可能撤單，不是直接下單指令；需搭配實際成交、量能與支撐壓力確認。")
    message = "\n".join(lines)
    for chat_id in chat_ids:
        _send_telegram_to_chat(chat_id, message)


def _owner_telegram_chat_ids() -> list[str]:
    chat_id = os.environ.get("STOCK_V1_TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        config_path = Path(__file__).resolve().parents[1] / "config" / "notify.json"
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                chat_id = str((config.get("telegram") or {}).get("chat_id") or "").strip()
            except Exception:
                chat_id = ""
    if not chat_id or "PASTE_" in chat_id:
        return []
    return [chat_id]


def _ensure_large_order_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS large_order_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            qty REAL NOT NULL,
            previous_qty REAL,
            threshold_lots REAL,
            last_price REAL,
            change_percent REAL,
            source TEXT,
            alerted_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_large_order_alerts_key ON large_order_alerts(code, side, price, alerted_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS large_order_observations (
            code TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            qty REAL NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (code, side, price)
        )
        """
    )


def _sparkline_values(values: list[float], points: int = 22) -> list[float]:
    clean = [round(float(value), 4) for value in values if value is not None]
    return clean[-points:]


def _sparkline_for_code(conn: sqlite3.Connection, code: str, append_value: float | None = None, points: int = 22) -> list[float]:
    rows = conn.execute(
        """
        SELECT close
        FROM prices
        WHERE stock_code = ? AND close IS NOT NULL
        ORDER BY date DESC
        LIMIT ?
        """,
        (code, points),
    ).fetchall()
    values = [float(row["close"]) for row in reversed(rows)]
    if append_value is not None:
        live = float(append_value)
        if not values or abs(values[-1] - live) > 0.0001:
            values.append(live)
    return _sparkline_values(values, points)


def _attach_sparklines(conn: sqlite3.Connection, rows: list[dict], append_live: bool = False) -> None:
    for row in rows:
        code = row.get("code")
        if not code:
            continue
        live_value = row.get("price") if append_live else None
        row["sparkline"] = _sparkline_for_code(conn, str(code), append_value=live_value)


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
    _ensure_column(conn, "watchlist", "sort_order", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_created_at ON watchlist(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_sort_order ON watchlist(sort_order, created_at)")


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
    _ensure_column(conn, "user_watchlist", "sort_order", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_watchlist_user ON user_watchlist(user_key, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_watchlist_order ON user_watchlist(user_key, sort_order, created_at)")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _parse_code_list(raw_codes: str) -> list[str]:
    seen = set()
    codes = []
    for code in raw_codes.split(","):
        clean = code.strip()
        if clean and clean not in seen:
            seen.add(clean)
            codes.append(clean)
    return codes


def _next_watch_order(conn: sqlite3.Connection, table: str, user_key: str | None = None) -> int:
    if table == "user_watchlist":
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS max_order FROM user_watchlist WHERE user_key = ?",
            (user_key,),
        ).fetchone()
    else:
        row = conn.execute("SELECT COALESCE(MAX(sort_order), 0) AS max_order FROM watchlist").fetchone()
    return int(row["max_order"] or 0) + 1


def _apply_watchlist_order(conn: sqlite3.Connection, table: str, codes: list[str], user_key: str | None = None) -> None:
    for index, code in enumerate(codes, start=1):
        if table == "user_watchlist":
            conn.execute(
                "UPDATE user_watchlist SET sort_order = ? WHERE user_key = ? AND code = ?",
                (index, user_key, code),
            )
        else:
            conn.execute("UPDATE watchlist SET sort_order = ? WHERE code = ?", (index, code))


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
            "SELECT code FROM user_watchlist WHERE user_key = ? ORDER BY COALESCE(sort_order, 999999), created_at, code",
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
            "INSERT OR IGNORE INTO user_watchlist (user_key, code, created_at, sort_order) VALUES (?, ?, datetime('now'), ?)",
            [(user_key, code, index + 1) for index, code in enumerate(["2330", "2367", "2454"])],
        )
        conn.commit()
        return conn.execute("SELECT * FROM app_users WHERE user_key = ?", (user_key,)).fetchone()


def _bind_telegram_to_user(db_path: Path, user_key: str, chat_id: str, display_name: str) -> dict:
    key = user_key.strip()
    if not key:
        return {"error": "請輸入個人代碼，例如：綁定 abc123"}
    with _connect(db_path) as conn:
        _ensure_user_tables(conn)
        row = conn.execute("SELECT * FROM app_users WHERE user_key = ?", (key,)).fetchone()
        if not row:
            return {"error": "找不到這個個人代碼，請回網站確認後再輸入。"}
        conn.execute(
            """
            UPDATE app_users
            SET telegram_chat_id = ?, telegram_enabled = 1, display_name = COALESCE(NULLIF(display_name, ''), ?), updated_at = datetime('now')
            WHERE user_key = ?
            """,
            (chat_id, display_name[:40] or row["display_name"], key),
        )
        conn.commit()
    return {"ok": True, "message": f"已綁定 Telegram，之後會把 {row['display_name']} 的個人觀察名單推播到這個聊天室。"}


def _extract_stock_code(text: str) -> str | None:
    for token in text.replace("：", " ").replace(":", " ").replace(",", " ").split():
        clean = token.strip().lstrip("+-#")
        if clean.isdigit() and 4 <= len(clean) <= 6:
            return clean[:6]
    if text.strip().isdigit() and 4 <= len(text.strip()) <= 6:
        return text.strip()
    return None


def _resolve_telegram_stock_code(db_path: Path, text: str) -> str | None:
    code = _extract_stock_code(text)
    if code:
        return code
    query = _normalize_stock_name_query(text)
    if not query:
        return None
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT code, name FROM stocks ORDER BY code").fetchall()
    exact_matches = []
    contains_matches = []
    for row in rows:
        full_name = str(row["name"] or "").strip()
        brief = short_name(full_name)
        candidates = {full_name, brief, full_name.replace("股份有限公司", ""), full_name.replace("有限公司", "")}
        if query in candidates:
            exact_matches.append(row["code"])
        elif any(query and query in candidate for candidate in candidates):
            contains_matches.append(row["code"])
    if exact_matches:
        return exact_matches[0]
    if len(contains_matches) == 1:
        return contains_matches[0]
    return contains_matches[0] if contains_matches else None


def _normalize_stock_name_query(text: str) -> str:
    clean = text.strip()
    for prefix in ["分析", "查詢", "查", "加入", "新增", "移除", "刪除", "+", "-", "#"]:
        if clean.startswith(prefix):
            clean = clean[len(prefix):].strip()
    for mark in ["：", ":", "，", ","]:
        clean = clean.replace(mark, " ")
    tokens = [token.strip() for token in clean.split() if token.strip()]
    if not tokens:
        return ""
    # Prefer the last token so "分析 台積電" and "查詢 聯發科" resolve naturally.
    return tokens[-1]


def _telegram_help_text() -> str:
    return "\n".join(
        [
            "第一次使用：請輸入 /start，系統會自動建立並綁定你的個人推播帳號。",
            "如果已在網站建立個人設定，請輸入：綁定 個人代碼",
            "",
            "可用指令：",
            "2330｜查個股分析",
            "台積電｜用中文名稱查個股分析",
            "分析 聯發科｜查個股買點、賣點、停損",
            "加入 華邦電｜加入個人觀察名單",
            "移除 2367｜移除個人觀察名單",
            "綁定 個人代碼｜把網站帳號綁到這個 Telegram，例如：綁定 abc123",
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


def _build_user_intraday_message(db_path: Path, user_key: str, display_name: str, limit: int = 5) -> str:
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    codes = _user_watchlist_codes(db_path, user_key)
    rows = api_watchlist(db_path, ",".join(codes)).get("watchlist", [])[:limit]
    lines = [
        f"台股智研｜{display_name} AI 盤中盯盤",
        f"時間：{now:%Y-%m-%d %H:%M}",
        "",
        "觀察名單即時風控",
    ]
    if not rows:
        lines.append("目前尚未加入觀察股票。")
    for item in rows:
        close = item.get("close")
        stop = item.get("stop")
        sell_zone = item.get("sell_zone")
        buy_zone = item.get("buy_zone")
        risk = "正常觀察"
        if close is not None and stop not in (None, "-"):
            try:
                if float(close) <= float(stop):
                    risk = "接近或跌破停損，優先風控"
            except (TypeError, ValueError):
                pass
        lines.extend([
            "",
            f"{item['code']} {item['name']}｜{item.get('signal', '資料不足')}｜{risk}",
            f"現價/收盤：{fmt_value(close)}｜RSI {fmt_value(item.get('rsi_14'))}｜量比 {fmt_value(item.get('volume_ratio'))}",
            f"買點：{buy_zone or '無資料'}｜賣點：{sell_zone or '無資料'}｜停損：{stop or '無資料'}",
            f"建議：{_simple_watch_advice(item)}",
        ])
    lines.extend([
        "",
        "提醒：盤中推播用於盯盤與風控，不是自動下單指令。",
    ])
    return "\n".join(lines)


def _build_user_premarket_message(db_path: Path, user_key: str, display_name: str, limit: int = 5) -> str:
    codes = _user_watchlist_codes(db_path, user_key)
    rows = api_watchlist(db_path, ",".join(codes)).get("watchlist", [])[:limit]
    return _format_premarket_message(display_name, rows, limit=limit)


def build_owner_premarket_message(db_path: Path, limit: int = 8) -> str:
    rows = api_watchlist(db_path, ",".join(_watchlist_codes(db_path))).get("watchlist", [])[:limit]
    return _format_premarket_message("本機使用者", rows, limit=limit)


def _build_premarket_report_text(db_path: Path, display_name: str, limit: int = 8) -> str:
    rows = api_watchlist(db_path, ",".join(_watchlist_codes(db_path))).get("watchlist", [])[:limit]
    return _format_premarket_message(display_name, rows, limit=limit)


def _format_premarket_message(display_name: str, rows: list[dict], limit: int = 8) -> str:
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    snapshot = _premarket_snapshot()
    news = fetch_market_news(max_items=8)
    lines = [
        f"台股智研｜{display_name} 08:30 盤前分析",
        f"時間：{now:%Y-%m-%d %H:%M}",
        "",
        f"早盤預估：{snapshot.get('stance', '資料不足')}｜分數 {snapshot.get('score', 50)}",
        snapshot.get("summary", "海外與夜盤資料不足，早盤以風控與分批為主。"),
        "",
        "海外/夜盤連動",
    ]
    items = snapshot.get("items") or []
    if items:
        for item in items[:10]:
            lines.append(
                f"- {item['name']}：{fmt_value(item.get('change_percent'))}%｜"
                f"{item.get('category', '連動')}｜{item.get('last_time', '無時間')}"
            )
    else:
        lines.append("- 目前抓不到免費海外/夜盤資料，請以開盤後即時看盤確認。")

    news_items = news.get("items") or []
    lines.extend(["", "24H 新聞風險"])
    if news_items:
        for item in news_items[:5]:
            lines.append(
                f"- [{item.get('impact', '中')}] {item.get('title', '')}｜"
                f"{item.get('category', '新聞')}｜{item.get('source', '')}"
            )
    else:
        lines.append("- 暫無重大新聞；若新聞源失敗，請以盤中異動與公告補驗。")

    lines.extend(["", "觀察名單早盤計畫"])
    if not rows:
        lines.append("目前尚未加入觀察股票。")
    for item in rows[:limit]:
        bias = _premarket_watch_bias(item, snapshot)
        lines.extend([
            "",
            f"{item['code']} {item['name']}｜{bias}",
            f"昨收/最新資料：{fmt_value(item.get('close'))}｜20D {fmt_value(item.get('return_20d'))}%｜RSI {fmt_value(item.get('rsi_14'))}",
            f"開盤策略：{_premarket_watch_action(item, snapshot)}",
            f"賣壓區：{item.get('sell_zone', '無資料')}｜量能需搭配即時看盤確認。",
        ])
    lines.extend([
        "",
        "提醒：08:30 報告是開盤前情境推演，真正進出場仍以 09:00 後即時價格、量能與停損紀律為準。",
    ])
    return "\n".join(lines)


def _premarket_watch_bias(item: dict, snapshot: dict) -> str:
    score = int(snapshot.get("score") or 50)
    name = str(item.get("name") or "")
    signal = item.get("signal") or "觀察"
    rsi14 = item.get("rsi_14")
    if score >= 60 and ("台積" in name or "聯發" in name or item.get("market") == "TWSE"):
        return f"夜盤偏多，{signal}，但避免開盤追高"
    if score <= 40:
        return f"夜盤偏空，{signal}，開盤先看支撐與停損"
    if rsi14 is not None and rsi14 >= 75:
        return f"中性偏熱，{signal}，優先等拉回"
    return f"中性觀察，{signal}"


def _premarket_watch_action(item: dict, snapshot: dict) -> str:
    score = int(snapshot.get("score") or 50)
    buy_zone = item.get("buy_zone", "無資料")
    stop = item.get("stop", "無資料")
    sell_zone = item.get("sell_zone", "無資料")
    rsi14 = item.get("rsi_14")
    if score <= 40:
        return f"先守停損 {stop}，低開不急著攤平，等 09:15 後量價止穩再評估。"
    if rsi14 is not None and rsi14 >= 75:
        return f"偏熱不追高，若開高靠近賣壓 {sell_zone} 先分批停利或等待拉回。"
    if score >= 62:
        return f"偏多但不開盤追價，優先等回測買點 {buy_zone} 或突破後回測站穩。"
    return f"先看開盤 15 分鐘量價，靠近買點 {buy_zone} 才分批，跌破 {stop} 降低風險。"


def _premarket_snapshot(force: bool = False) -> dict:
    cached_until = _PREMARKET_CACHE.get("expires_at")
    if not force and isinstance(cached_until, datetime) and datetime.now() < cached_until and _PREMARKET_CACHE.get("data"):
        return dict(_PREMARKET_CACHE["data"])  # type: ignore[arg-type]
    symbols = [
        ("S&P 500 期貨", "ES=F", "美股期貨", 1.2, 1),
        ("Nasdaq 100 期貨", "NQ=F", "科技期貨", 1.8, 1),
        ("道瓊期貨", "YM=F", "美股期貨", 0.8, 1),
        ("費半指數", "^SOX", "半導體", 2.0, 1),
        ("Nasdaq 指數", "^IXIC", "科技股", 1.5, 1),
        ("S&P 500 指數", "^GSPC", "美股", 1.0, 1),
        ("VIX 恐慌指數", "^VIX", "風險", 1.8, -1),
        ("台積電 ADR", "TSM", "台股連動", 2.2, 1),
        ("NVIDIA", "NVDA", "AI 權值", 1.5, 1),
        ("AMD", "AMD", "AI/半導體", 1.0, 1),
        ("Broadcom", "AVGO", "AI/半導體", 1.0, 1),
        ("Micron", "MU", "記憶體", 1.1, 1),
        ("ASML", "ASML", "半導體設備", 0.9, 1),
        ("費半 ETF", "SOXX", "半導體 ETF", 1.4, 1),
        ("半導體 ETF", "SMH", "半導體 ETF", 1.4, 1),
        ("iShares Taiwan ETF", "EWT", "台股 ETF", 1.6, 1),
        ("台股加權指數", "^TWII", "台股前日", 0.4, 1),
        ("美元/台幣", "TWD=X", "匯率", 1.1, -1),
        ("美元指數 ETF", "UUP", "匯率", 0.8, -1),
        ("台指期夜盤候選", "TXF=F", "台股夜盤", 2.0, 1),
    ]
    items = []
    failures = []
    score = 50.0
    factors = []
    for name, symbol, category, weight, direction in symbols:
        try:
            item = _fetch_yahoo_snapshot(symbol)
            if item:
                change = item.get("change_percent") or 0
                contribution = max(-9, min(9, change * 1.75)) * weight * direction
                score += contribution
                rows_item = {
                    "name": name,
                    "symbol": symbol,
                    "category": category,
                    "weight": weight,
                    "direction": direction,
                    "contribution": round(contribution, 2),
                    **item,
                }
                items.append(rows_item)
                if abs(contribution) >= 2.5:
                    factors.append(rows_item)
        except Exception as exc:
            failures.append(f"{symbol}: {exc}")
    score = max(0, min(100, round(score)))
    if score >= 62:
        stance = "偏多開盤"
        summary = "海外科技、半導體或台股連動資產偏強，早盤可優先觀察強勢股回測承接，但不建議開盤直接追高。"
    elif score <= 42:
        stance = "偏空防守"
        summary = "海外風險或匯率壓力偏弱，早盤先看支撐與停損，避免把資金一次投入。"
    else:
        stance = "中性觀察"
        summary = "海外與台股連動訊號沒有明顯單邊，早盤以開盤溢價、量能與個股支撐為主。"
    result = {
        "prepared_at": datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
        "factors": sorted(factors, key=lambda row: abs(row.get("contribution") or 0), reverse=True)[:8],
        "failures": failures[:5],
        "score": score,
        "stance": stance,
        "summary": summary,
    }
    _PREMARKET_CACHE["data"] = result
    _PREMARKET_CACHE["expires_at"] = datetime.now() + timedelta(minutes=3)
    return result


def _fetch_yahoo_snapshot(symbol: str) -> dict | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=5m&includePrePost=true"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None
    meta = result.get("meta") or {}
    quote = ((result.get("indicators") or {}).get("quote") or [None])[0] or {}
    closes = [value for value in quote.get("close", []) if value is not None]
    timestamps = result.get("timestamp") or []
    latest = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    previous = meta.get("chartPreviousClose") or meta.get("previousClose")
    if previous in (None, 0) and len(closes) >= 2:
        previous = closes[0]
    if latest is None or previous in (None, 0):
        return None
    previous, latest = float(previous), float(latest)
    change = latest - previous
    change_percent = change / previous * 100 if previous else None
    last_time = "-"
    if timestamps:
        last_time = datetime.fromtimestamp(timestamps[-1], ZoneInfo("Asia/Taipei")).strftime("%m-%d %H:%M")
    return {
        "price": latest,
        "previous": previous,
        "change": change,
        "change_percent": change_percent,
        "last_time": last_time,
    }


def scan_breaking_news(db_path: Path, limit: int = 8) -> dict:
    market_news = fetch_market_news(limit_per_keyword=6, max_items=24)
    watch_items = _watchlist_news_items(db_path, limit=limit)
    candidates = []
    for item in (market_news.get("items") or []) + watch_items:
        identity = item.get("id") or headline_id(item)
        enriched = {**item, "id": identity}
        if (enriched.get("impact_score") or 0) >= 55:
            candidates.append(enriched)
    with _connect(db_path) as conn:
        _ensure_news_alert_tables(conn)
        new_items = []
        for item in candidates:
            exists = conn.execute(
                "SELECT id FROM news_alert_state WHERE id = ?",
                (item["id"],),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """
                INSERT INTO news_alert_state (
                    id, title, link, source, published, impact, impact_score, category, sentiment, seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    item["id"],
                    str(item.get("title") or "")[:500],
                    str(item.get("link") or "")[:1000],
                    str(item.get("source") or "")[:120],
                    str(item.get("published") or "")[:120],
                    str(item.get("impact") or "中"),
                    int(item.get("impact_score") or 0),
                    str(item.get("category") or "")[:120],
                    str(item.get("sentiment") or "")[:40],
                ),
            )
            new_items.append(item)
        conn.commit()
    new_items.sort(key=lambda row: row.get("impact_score") or 0, reverse=True)
    return {
        "checked_at": datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S"),
        "candidates": len(candidates),
        "new_items": new_items[:limit],
        "market_failures": market_news.get("failures") or [],
    }


def build_news_alert_message(items: list[dict]) -> str:
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    lines = [
        f"台股智研｜24H 突發新聞盯盤 {now:%Y-%m-%d %H:%M}",
        "以下為新出現且影響分數較高的新聞，請用於進出台股前的風險確認。",
    ]
    for item in items[:8]:
        lines.extend(
            [
                "",
                f"[{item.get('impact', '中')}] {item.get('category', '新聞')}｜{item.get('sentiment', '中性')}｜分數 {item.get('impact_score', '-')}",
                str(item.get("title") or ""),
                f"來源：{item.get('source', 'Google News')}｜{item.get('published', '-')}",
                f"操作提醒：{item.get('action', '等待價格與量能確認。')}",
            ]
        )
    lines.extend(
        [
            "",
            "提醒：新聞推播是風險雷達，不是直接下單指令；仍需搭配即時報價、成交量、停損與部位控管。",
        ]
    )
    return "\n".join(lines)


def _watchlist_news_items(db_path: Path, limit: int = 8) -> list[dict]:
    items = []
    with _connect(db_path) as conn:
        stocks = conn.execute(
            """
            SELECT w.code, s.name
            FROM watchlist w
            JOIN stocks s ON s.code = w.code
            ORDER BY COALESCE(w.sort_order, 999999), w.created_at, w.code
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    for stock in stocks:
        news = fetch_stock_news(stock["code"], short_name(stock["name"]), limit=3)
        for item in news.get("items") or []:
            impact = item.get("impact_score")
            if impact is None:
                title = item.get("title", "")
                market_item = next(
                    (
                        row
                        for row in fetch_market_news([f"{stock['code']} {short_name(stock['name'])} 台股"], 3, 3).get("items", [])
                        if row.get("title") == title
                    ),
                    None,
                )
                if not market_item:
                    continue
                item.update(market_item)
            item.setdefault("id", headline_id(item))
            item.setdefault("category", f"觀察股 {stock['code']}")
            item.setdefault("impact", "中")
            item.setdefault("impact_score", 60 if item.get("sentiment") != "中性" else 50)
            item.setdefault("action", "檢查觀察名單持股與開盤量價。")
            items.append(item)
    return items


def _ensure_news_alert_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_alert_state (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            link TEXT,
            source TEXT,
            published TEXT,
            impact TEXT,
            impact_score INTEGER,
            category TEXT,
            sentiment TEXT,
            seen_at TEXT NOT NULL,
            sent_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_alert_seen_at ON news_alert_state(seen_at)")


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


def _job_authorized(token: str) -> tuple[bool, str]:
    expected = os.environ.get("STOCK_V1_JOB_TOKEN", "").strip()
    if not expected:
        return False, "STOCK_V1_JOB_TOKEN 尚未設定，排程推播端點未啟用。"
    if not secrets.compare_digest(str(token or ""), expected):
        return False, "排程 token 錯誤。"
    return True, ""


def _admin_authorized(token: str) -> tuple[bool, str]:
    expected = os.environ.get("STOCK_V1_ADMIN_TOKEN", "").strip() or os.environ.get("STOCK_V1_JOB_TOKEN", "").strip()
    if not expected:
        return False, "STOCK_V1_ADMIN_TOKEN 尚未設定，後台查詢未啟用。"
    if not secrets.compare_digest(str(token or ""), expected):
        return False, "管理員 token 錯誤。"
    return True, ""


def _mask_secret(value: str) -> str:
    text = str(value or "")
    if len(text) <= 4:
        return "***" if text else ""
    return f"{text[:3]}***{text[-3:]}"


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
        rows = conn.execute("SELECT code FROM watchlist ORDER BY COALESCE(sort_order, 999999), created_at, code").fetchall()
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
        "sparkline": _sparkline_values([row["close"] for row in reversed(price_rows) if row["close"] is not None]),
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
      color-scheme: dark;
      --bg: #050b14;
      --panel: #0b1524;
      --panel-2: #0e1a2b;
      --ink: #f8fafc;
      --text: #dbeafe;
      --muted: #8fb4c7;
      --line: #1f3550;
      --line-strong: #31506d;
      --accent: #00a884;
      --accent-dark: #007c67;
      --accent-soft: rgba(20, 184, 166, .16);
      --gold: #c1841d;
      --blue: #2563eb;
      --navy: #111827;
      --cyan: #0891b2;
      --down: #c24135;
      --topbar: #050b14;
      --topbar-2: #0b1828;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        linear-gradient(rgba(125,211,252,.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(125,211,252,.035) 1px, transparent 1px),
        radial-gradient(circle at 12% 0%, rgba(8,145,178,.26), transparent 32%),
        radial-gradient(circle at 92% 10%, rgba(0,168,132,.20), transparent 30%),
        linear-gradient(180deg, #030712 0, #07111f 420px, #06101d 100%);
      background-size: 42px 42px, 42px 42px, auto, auto, auto;
      color: var(--text);
      font-family: Arial, "Microsoft JhengHei", sans-serif;
      font-size: 16px;
      line-height: 1.55;
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
      font-size: 14px;
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
      color: #0f172a;
      caret-color: #0891b2;
      border-color: var(--line-strong);
      font-weight: 700;
      letter-spacing: 0;
    }
    input::placeholder {
      color: #64748b;
      opacity: 1;
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
      font-size: 13px;
      margin-bottom: 8px;
      font-weight: 700;
    }
    .metric strong { font-size: 25px; }
    .grid { grid-template-columns: 1fr 1fr; }
    section {
      overflow: hidden;
      transition: max-height .18s ease, box-shadow .18s ease, border-color .18s ease;
    }
    .info-panel {
      position: relative;
    }
    .info-panel h2::after {
      display: none;
    }
    section h2 {
      margin: 0;
      padding: 15px 16px;
      font-size: 18px;
      border-bottom: 1px solid var(--line);
      background:
        linear-gradient(90deg, #f5fbf9, #ffffff);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    section h2::after {
      content: "";
      width: 34px;
      height: 3px;
      border-radius: 999px;
      background: var(--accent);
    }
    section h2 .section-title {
      min-width: 0;
      flex: 1 1 auto;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    section h2 .panel-toggle {
      height: 30px;
      min-width: 58px;
      flex: 0 0 auto;
      padding: 0 10px;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 900;
      color: #dbeafe;
      background: rgba(14, 165, 233, .18);
      border-color: rgba(56, 189, 248, .36);
      box-shadow: none;
    }
    section h2 .panel-toggle:hover {
      color: #ffffff;
      background: rgba(14, 165, 233, .34);
    }
    .info-panel.panel-compact {
      grid-column: span 1 !important;
      max-height: 76px;
      min-height: 64px;
      min-width: 220px;
      cursor: pointer;
    }
    .info-panel.panel-compact > :not(h2) {
      display: none !important;
    }
    .info-panel.panel-compact h2 {
      min-height: 64px;
      padding: 0 16px;
      border-bottom: 0;
    }
    .info-panel.panel-compact .section-title {
      overflow: visible;
      text-overflow: unset;
      white-space: normal;
      line-height: 1.2;
    }
    .info-panel.panel-compact h2::after {
      content: none;
      display: none;
    }
    .info-panel.panel-compact .panel-toggle {
      width: 34px;
      min-width: 34px;
      padding: 0;
      font-size: 20px;
      line-height: 1;
    }
    .info-panel.panel-expanded {
      grid-column: 1 / -1 !important;
      max-height: none;
      min-height: 0;
      border-color: rgba(34, 211, 238, .42);
      box-shadow:
        0 28px 70px rgba(2, 6, 23, .42),
        inset 0 1px 0 rgba(255,255,255,.06),
        0 0 38px rgba(34, 211, 238, .12);
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
    .content {
      padding: 17px;
      font-size: 15px;
      line-height: 1.65;
    }
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
    #strategyPage .content,
    #strategyPage .advice-list {
      color: #dbeafe;
    }
    #strategyPage .advice-main,
    #strategyPage .strategy-kpi {
      background: linear-gradient(155deg, rgba(13, 25, 43, .98), rgba(7, 15, 28, .96));
      border-color: rgba(56, 189, 248, .24);
      color: #dbeafe;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.04);
    }
    #strategyPage .advice-main strong,
    #strategyPage .strategy-kpi strong,
    #strategyPage .content b {
      color: #ffffff;
    }
    #strategyPage .advice-main p,
    #strategyPage .advice-list li {
      color: #cbd5e1;
      line-height: 1.65;
    }
    #strategyPage .strategy-kpi span {
      color: #8fd3f4;
    }
    .manager-report {
      display: grid;
      gap: 12px;
    }
    .manager-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .manager-card {
      border: 1px solid rgba(56, 189, 248, .24);
      border-radius: 8px;
      padding: 14px;
      background: linear-gradient(155deg, rgba(13, 25, 43, .98), rgba(7, 15, 28, .96));
      color: #dbeafe;
      min-width: 0;
    }
    .manager-card.full {
      grid-column: 1 / -1;
    }
    .manager-card h3 {
      margin: 0 0 10px;
      font-size: 18px;
      color: #ffffff;
      letter-spacing: 0;
    }
    .manager-card p {
      margin: 6px 0;
      color: #cbd5e1;
      line-height: 1.65;
    }
    .manager-card b {
      color: #ffffff;
    }
    .manager-table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 8px;
      color: #dbeafe;
    }
    .manager-table th,
    .manager-table td {
      border-bottom: 1px solid rgba(148, 163, 184, .18);
      padding: 8px 6px;
      text-align: left;
      vertical-align: top;
    }
    .manager-table th {
      color: #8fd3f4;
      font-size: 13px;
    }
    .manager-badge {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      border: 1px solid rgba(56, 189, 248, .38);
      background: rgba(14, 165, 233, .16);
      color: #ffffff;
      font-weight: 900;
      white-space: nowrap;
    }
    .manager-scenario-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
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
      font-size: 15px;
    }
    dt { color: var(--muted); font-weight: 700; }
    dd { margin: 0; font-weight: 700; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 15px;
    }
    thead th {
      background: #eef7f4;
      color: #35524b;
      font-size: 13px;
      font-weight: 800;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 9px;
      text-align: right;
      white-space: nowrap;
    }
    tbody tr:hover {
      background: #f4fbf9;
    }
    th:first-child, td:first-child,
    th:nth-child(2), td:nth-child(2) { text-align: left; }
    .table-wrap { overflow-x: auto; }
    .positive { color: #ff3b30; }
    .negative { color: #00d97e; }
    .wide { grid-column: 1 / -1; }
    .note { color: var(--muted); }
    .status {
      min-height: 30px;
      color: var(--muted);
      font-size: 15px;
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
    .stock-command {
      background:
        radial-gradient(circle at 22% 10%, rgba(34, 211, 238, .18), transparent 34%),
        linear-gradient(135deg, rgba(8, 20, 34, .98), rgba(8, 42, 40, .94));
      border: 1px solid rgba(34, 211, 238, .24);
      box-shadow:
        0 22px 60px rgba(2, 6, 23, .34),
        inset 0 1px 0 rgba(255, 255, 255, .06),
        0 0 34px rgba(20, 184, 166, .12);
      position: relative;
    }
    .stock-command::before {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background: linear-gradient(90deg, transparent, rgba(125, 211, 252, .12), transparent);
      transform: translateX(-100%);
      animation: scan-sheen 5s ease-in-out infinite;
    }
    @keyframes scan-sheen {
      0%, 45% { transform: translateX(-100%); opacity: 0; }
      55% { opacity: 1; }
      100% { transform: translateX(100%); opacity: 0; }
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
      color: #0f172a;
      caret-color: #0891b2;
    }
    .explore-actions input::placeholder {
      color: #64748b;
      opacity: 1;
    }
    .explore-actions button:not(.primary) {
      background: rgba(255,255,255,.10);
      border-color: rgba(255,255,255,.20);
      color: #e5edf6;
    }
    .stock-command .explore-actions {
      position: relative;
      z-index: 1;
    }
    .stock-command input {
      background: rgba(248, 252, 255, .96);
      color: #0f172a;
      caret-color: #0891b2;
      border-color: rgba(125, 211, 252, .45);
      box-shadow: 0 0 0 1px rgba(34, 211, 238, .08), 0 0 24px rgba(34, 211, 238, .10);
    }
    .stock-command input::placeholder {
      color: #64748b;
      opacity: 1;
    }
    .stock-command button.primary {
      box-shadow: 0 0 24px rgba(20, 184, 166, .34);
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
    .facet-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .facet-card,
    .institution-card {
      position: relative;
      overflow: hidden;
      background: linear-gradient(155deg, rgba(11, 22, 38, .98), rgba(7, 18, 32, .94));
      border: 1px solid rgba(56, 189, 248, .20);
      border-radius: 10px;
      padding: 12px;
      color: #dbeafe;
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, .05),
        0 12px 24px rgba(2, 6, 23, .22),
        0 0 20px rgba(8, 145, 178, .08);
    }
    .facet-card::after,
    .institution-card::after {
      content: "";
      position: absolute;
      inset: 0 0 auto;
      height: 1px;
      background: linear-gradient(90deg, transparent, rgba(125, 211, 252, .75), transparent);
    }
    .facet-card span,
    .institution-card span {
      display: block;
      color: #7dd3fc;
      font-size: 12px;
      font-weight: 900;
      margin-bottom: 6px;
    }
    .facet-card strong,
    .institution-card strong {
      display: block;
      color: #ffffff;
      font-size: 18px;
      text-shadow: 0 0 18px rgba(125, 211, 252, .26);
    }
    .facet-card small,
    .institution-card small {
      display: block;
      margin-top: 8px;
      color: #a8c5d8;
      line-height: 1.45;
    }
    .institution-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .institution-card.wide {
      grid-column: 1 / -1;
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
    .drag-handle {
      width: 34px;
      color: #64748b;
      cursor: grab;
      font-weight: 900;
      text-align: center;
      user-select: none;
    }
    .drag-handle:active {
      cursor: grabbing;
    }
    tr.watch-dragging {
      opacity: .46;
    }
    tr.watch-drop-before {
      box-shadow: inset 0 2px 0 #06b6d4;
    }
    tr.watch-drop-after {
      box-shadow: inset 0 -2px 0 #06b6d4;
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
    .stock-part-title {
      display: grid;
      grid-template-columns: 42px minmax(0, 1fr);
      gap: 12px;
      align-items: center;
      padding: 14px 16px;
      border: 1px solid rgba(56,189,248,.24);
      border-radius: 10px;
      background: linear-gradient(90deg, rgba(15, 35, 52, .96), rgba(7, 18, 32, .92));
      box-shadow: 0 16px 38px rgba(2, 6, 23, .22), inset 0 1px 0 rgba(255,255,255,.05);
    }
    .stock-part-title span {
      width: 34px;
      height: 34px;
      display: inline-grid;
      place-items: center;
      border-radius: 50%;
      background: rgba(14, 165, 233, .2);
      border: 1px solid rgba(56, 189, 248, .42);
      color: #e0f2fe;
      font-weight: 900;
    }
    .stock-part-title strong {
      display: block;
      color: #f8fafc;
      font-size: 22px;
      line-height: 1.2;
    }
    .stock-part-title small {
      display: block;
      color: #8fb4c7;
      margin-top: 3px;
      font-size: 13px;
      font-weight: 800;
    }
    .desk-main, .desk-side {
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    .desk-side {
      grid-template-columns: repeat(12, minmax(0, 1fr));
      align-items: start;
      grid-auto-flow: dense;
    }
    .desk-side .desk-panel {
      min-width: 0;
      align-self: start;
    }
    .desk-side .info-panel.panel-compact {
      grid-column: span 4 !important;
      min-width: 0;
    }
    .desk-side .panel-diagnosis,
    .desk-side .panel-trade,
    .desk-side .panel-theory,
    .desk-side .panel-summary,
    .desk-side .panel-indicators {
      grid-column: span 3;
    }
    .desk-side .panel-facets,
    .desk-side .panel-institutional {
      grid-column: span 6;
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
    .desk-side .facet-grid,
    .desk-side .institution-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .desk-side .panel-summary dl,
    .desk-side .panel-indicators dl {
      grid-template-columns: minmax(76px, auto) minmax(0, 1fr);
    }
    .professional-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      align-items: start;
    }
    .professional-grid .desk-panel {
      min-width: 0;
    }
    .professional-grid .wide {
      grid-column: 1 / -1;
    }
    .compact-block {
      border-top: 1px solid rgba(56,189,248,.16);
      margin-top: 8px;
      padding-top: 8px;
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
      grid-template-columns: minmax(520px, 1fr) minmax(230px, .42fr) minmax(260px, .48fr);
      gap: 8px;
      align-items: start;
      background: #030506;
      border: 1px solid #1f2933;
      padding: 8px;
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
      grid-column: 1;
      grid-row: 1;
    }
    .terminal-tape {
      grid-column: 2;
      grid-row: 1;
    }
    .terminal-depth {
      grid-column: 3;
      grid-row: 1;
    }
    .terminal-watch {
      grid-column: 1 / -1;
      grid-row: 2;
    }
    .terminal-head {
      min-height: 32px;
      padding: 7px 9px;
      color: #f8fafc;
      font-size: 18px;
      font-weight: 900;
      border-bottom: 1px solid #1f2933;
    }
    .terminal-head.updating {
      animation: terminal-flash .45s ease;
    }
    @keyframes terminal-flash {
      0% { background: rgba(34, 211, 238, .26); }
      100% { background: transparent; }
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
      height: 300px;
      border: 0;
      background: #000;
    }
    .terminal-volume {
      min-height: 66px;
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
      padding: 7px 9px;
      background: #11181c;
      border-bottom: 1px solid #25313a;
      color: #dbeafe;
      font-size: 13px;
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
      font-size: 13px;
      font-weight: 900;
      margin-bottom: 5px;
    }
    .trend-kpi strong {
      color: #ffffff;
      font-size: 20px;
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
      font-size: 18px;
      margin-bottom: 6px;
    }
    .trade-callout p {
      margin: 0;
      color: #cbd5e1;
      line-height: 1.55;
      font-size: 15px;
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
    .research-stage-list {
      display: grid;
      gap: 10px;
      margin-top: 10px;
    }
    .research-stage {
      border: 1px solid rgba(56,189,248,.22);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(15, 26, 42, .96), rgba(8, 17, 31, .96));
      overflow: hidden;
    }
    .research-stage summary {
      display: grid;
      grid-template-columns: auto minmax(220px, 1fr) auto auto;
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
      color: #e5edf6;
      font-weight: 900;
      cursor: pointer;
      list-style: none;
    }
    select {
      height: 40px;
      min-width: 220px;
      border: 1px solid rgba(56,189,248,.28);
      border-radius: 8px;
      background: #0f1a2a;
      color: #e5edf6;
      padding: 0 12px;
      font-weight: 800;
    }
    .research-stage summary::-webkit-details-marker { display: none; }
    .research-stage summary::before {
      content: "+";
      display: inline-grid;
      place-items: center;
      width: 24px;
      height: 24px;
      border-radius: 50%;
      border: 1px solid rgba(56,189,248,.4);
      color: #7dd3fc;
      margin-right: 8px;
    }
    .research-stage[open] summary::before { content: "收"; width: 34px; border-radius: 999px; font-size: 11px; }
    .stage-status {
      color: #a7f3d0;
      font-size: 12px;
      white-space: nowrap;
    }
    .stage-confidence {
      color: #93c5fd;
      font-size: 12px;
      white-space: nowrap;
    }
    .stage-body {
      border-top: 1px solid rgba(148, 163, 184, .14);
      padding: 12px 14px;
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
    #realtimePage .dashboard-note {
      padding: 10px 12px;
      margin-bottom: 8px;
    }
    #realtimePage .toolbar {
      grid-template-columns: minmax(240px, 1fr) repeat(4, auto);
      gap: 8px;
      padding: 9px;
      margin-bottom: 8px;
    }
    #realtimePage .status {
      margin-bottom: 8px;
      min-height: 28px;
      padding: 6px 10px;
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
      color: #ff5c5c;
    }
    .realtime-row.selected .negative,
    .desk-panel .realtime-row.selected .negative {
      color: #00ffc6;
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
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background:
        radial-gradient(circle at 20% 12%, rgba(34, 211, 238, .10), transparent 26%),
        radial-gradient(circle at 82% 18%, rgba(16, 185, 129, .08), transparent 24%);
      z-index: -1;
    }
    .toolbar,
    .watch-tools,
    .metric,
    section,
    .dashboard-note,
    .market-tile,
    .module-card,
    .watch-card,
    .diagnosis-card,
    .status {
      background: linear-gradient(155deg, rgba(11, 21, 36, .96), rgba(7, 16, 29, .92));
      border-color: rgba(56, 189, 248, .18);
      color: var(--text);
      box-shadow:
        0 18px 42px rgba(2, 6, 23, .34),
        inset 0 1px 0 rgba(255, 255, 255, .045),
        0 0 24px rgba(8, 145, 178, .06);
    }
    section h2 {
      background: linear-gradient(90deg, rgba(15, 30, 50, .98), rgba(8, 19, 34, .94));
      border-color: rgba(56, 189, 248, .16);
      color: #f8fafc;
    }
    input {
      background: rgba(5, 12, 24, .92);
      color: #e0f2fe;
      caret-color: #7dd3fc;
      border-color: rgba(125, 211, 252, .32);
      box-shadow: inset 0 1px 10px rgba(2, 6, 23, .36);
    }
    input::placeholder {
      color: #9fb6c9;
      opacity: 1;
    }
    input:focus {
      outline: 2px solid rgba(34, 211, 238, .18);
      box-shadow: 0 0 24px rgba(34, 211, 238, .16);
    }
    button {
      background: linear-gradient(180deg, rgba(15, 35, 52, .96), rgba(8, 20, 35, .96));
      color: #dbeafe;
      border-color: rgba(125, 211, 252, .24);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .04);
    }
    button:hover {
      background: linear-gradient(180deg, rgba(14, 116, 144, .42), rgba(8, 47, 73, .56));
      border-color: rgba(34, 211, 238, .46);
      color: #ffffff;
      box-shadow: 0 0 22px rgba(34, 211, 238, .16);
    }
    button.primary {
      background: linear-gradient(135deg, #00a884, #0891b2);
      box-shadow: 0 0 26px rgba(20, 184, 166, .30);
    }
    thead th {
      background: rgba(13, 31, 49, .98);
      color: #a7f3d0;
      border-color: rgba(56, 189, 248, .18);
    }
    th, td {
      border-color: rgba(51, 65, 85, .76);
      color: #dbeafe;
    }
    tbody tr:hover {
      background: rgba(14, 165, 233, .10);
    }
    .chart {
      background: linear-gradient(180deg, #07111f, #050b14);
      border-color: rgba(56, 189, 248, .18);
    }
    .hero-panel,
    .explore-hero,
    .stock-command,
    .realtime-board {
      border-color: rgba(34, 211, 238, .22);
      box-shadow:
        0 24px 60px rgba(2, 6, 23, .38),
        inset 0 1px 0 rgba(255, 255, 255, .05),
        0 0 34px rgba(20, 184, 166, .10);
    }
    .market-tile strong,
    .module-card strong,
    .watch-card strong,
    .diagnosis-card strong,
    .metric strong,
    dd {
      color: #ffffff;
    }
    .market-tile span,
    .module-card span,
    .watch-card span,
    .diagnosis-card span,
    dt,
    .note,
    .status,
    .dashboard-note span {
      color: #8fb4c7;
    }
    .pill {
      background: rgba(20, 184, 166, .14);
      color: #a7f3d0;
      border-color: rgba(34, 211, 238, .24);
      box-shadow: 0 0 18px rgba(20, 184, 166, .10);
    }
    .table-wrap {
      background: rgba(2, 6, 23, .18);
    }
    .stock-card-table,
    .stock-card-table tbody,
    .stock-card-table tr,
    .stock-card-table td {
      display: block;
      width: 100%;
      border: 0;
      padding: 0;
    }
    .stock-card-table thead {
      display: none;
    }
    .stock-card-table tbody {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
      gap: 14px;
      padding: 14px;
    }
    .stock-card-table.compact-card-table tbody {
      grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
      gap: 10px;
      padding: 10px;
    }
    .terminal-watch .stock-card-table.compact-card-table tbody {
      grid-template-columns: repeat(auto-fit, minmax(205px, 1fr));
      align-content: start;
      padding: 8px;
    }
    .terminal-watch .stock-card.compact-stock-card {
      min-height: 136px;
    }
    .terminal-watch .stock-card.compact-stock-card h3 {
      font-size: 16px;
    }
    .terminal-watch .stock-card.compact-stock-card .stock-price {
      font-size: 20px;
    }
    .realtime-list-table,
    .realtime-list-table tbody,
    .realtime-list-table tr,
    .realtime-list-table td {
      display: block;
      width: 100%;
      border: 0;
      padding: 0;
    }
    .realtime-list-table thead {
      display: none;
    }
    .realtime-list-table tbody {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      padding: 8px;
    }
    .realtime-list-card {
      min-height: 116px;
      display: grid;
      grid-template-columns: 34px minmax(96px, 1fr) minmax(82px, .62fr) minmax(118px, .76fr);
      gap: 10px;
      align-items: center;
      padding: 11px 10px;
      border-radius: 8px;
      border: 1px solid rgba(71, 85, 105, .32);
      background:
        linear-gradient(90deg, rgba(14, 165, 233, .14), rgba(15, 23, 42, .08) 42%),
        linear-gradient(155deg, rgba(16, 24, 39, .98), rgba(20, 13, 29, .98));
      color: #e5edf6;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.04);
    }
    .realtime-list-card > * {
      min-width: 0;
    }
    .realtime-row:nth-child(n + 4) .realtime-list-card {
      background:
        linear-gradient(90deg, rgba(245, 158, 11, .12), rgba(15, 23, 42, .04) 42%),
        linear-gradient(155deg, rgba(17, 15, 29, .98), rgba(20, 13, 29, .98));
    }
    .realtime-row.selected .realtime-list-card {
      outline: 1px solid rgba(34, 211, 238, .76);
      box-shadow: inset 3px 0 0 #22d3ee, 0 0 26px rgba(34, 211, 238, .16);
    }
    .realtime-rank {
      width: 32px;
      height: 32px;
      display: grid;
      place-items: center;
      border-radius: 999px;
      border: 1px solid rgba(245, 158, 11, .72);
      background: rgba(120, 53, 15, .56);
      color: #facc15;
      font-size: 16px;
      font-weight: 900;
    }
    .realtime-row:nth-child(n + 4) .realtime-rank {
      border-color: rgba(148, 163, 184, .42);
      background: rgba(71, 85, 105, .56);
      color: #dbeafe;
    }
    .realtime-list-name strong {
      display: block;
      color: #ffffff;
      font-size: 17px;
      line-height: 1.15;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .realtime-list-name span,
    .realtime-list-meta {
      color: #a8b6c9;
      font-size: 13px;
      font-weight: 900;
    }
    .realtime-list-price strong {
      display: block;
      color: #ff3030;
      font-size: 19px;
      line-height: 1.1;
    }
    .realtime-list-price span {
      display: block;
      margin-top: 7px;
      color: #a8b6c9;
      font-size: 13px;
      font-weight: 900;
      white-space: nowrap;
    }
    .realtime-list-spark {
      min-width: 0;
      text-align: right;
      overflow: hidden;
    }
    .realtime-list-spark .stock-spark {
      height: 42px;
      margin-left: auto;
    }
    .realtime-list-meta {
      margin-top: 4px;
      display: block;
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .stock-card-row {
      min-width: 0;
    }
    .stock-card {
      position: relative;
      min-height: 222px;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: auto auto 1fr auto;
      gap: 10px;
      padding: 18px;
      border-radius: 14px;
      overflow: hidden;
      background:
        radial-gradient(circle at 92% 10%, rgba(16, 185, 129, .13), transparent 20%),
        linear-gradient(155deg, rgba(20, 14, 28, .98), rgba(12, 12, 25, .98));
      border: 1px solid rgba(72, 91, 122, .42);
      box-shadow:
        0 18px 36px rgba(2, 6, 23, .36),
        inset 0 1px 0 rgba(255, 255, 255, .05),
        0 0 22px rgba(14, 165, 233, .06);
      color: #e5edf6;
    }
    .stock-card.compact-stock-card {
      min-height: 146px;
      padding: 13px;
      gap: 7px;
      grid-template-rows: auto auto auto;
    }
    .stock-card.compact-stock-card h3 {
      font-size: 18px;
    }
    .stock-card.compact-stock-card .stock-price {
      font-size: 22px;
    }
    .stock-card.compact-stock-card .stock-spark {
      height: 44px;
    }
    .stock-card.compact-stock-card .stock-card-foot {
      display: none;
    }
    .stock-card.compact-stock-card .stock-actions {
      justify-content: flex-start;
      gap: 6px;
    }
    .stock-card.compact-stock-card .stock-card-mid {
      grid-template-columns: minmax(82px, .7fr) minmax(92px, 1fr);
    }
    .stock-card > * {
      grid-column: 1 / -1;
      min-width: 0;
    }
    .stock-card::before {
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 3px;
      background: linear-gradient(180deg, #f59e0b, #ef4444);
      box-shadow: 0 0 18px rgba(245, 158, 11, .46);
    }
    .stock-card.positive-card::before {
      background: linear-gradient(180deg, #facc15, #ef4444);
    }
    .stock-card.negative-card::before {
      background: linear-gradient(180deg, #38bdf8, #22c55e);
    }
    .stock-card.selected-card {
      outline: 1px solid rgba(34, 211, 238, .72);
      box-shadow:
        0 20px 42px rgba(2, 6, 23, .40),
        0 0 34px rgba(34, 211, 238, .20);
    }
    .stock-card-top {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
    }
    .stock-card h3 {
      margin: 0;
      color: #ffffff;
      font-size: 20px;
      line-height: 1.2;
    }
    .stock-card small {
      display: block;
      color: #9ca3af;
      margin-top: 6px;
      font-weight: 700;
    }
    .stock-led {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #16a34a;
      box-shadow: 0 0 14px rgba(34, 197, 94, .95);
      flex: 0 0 auto;
      margin-top: 3px;
    }
    .stock-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .stock-tag {
      border: 1px solid rgba(96, 165, 250, .34);
      color: #bfdbfe;
      background: rgba(37, 99, 235, .10);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
    }
    .stock-tag.hot {
      border-color: rgba(250, 204, 21, .36);
      color: #fde68a;
      background: rgba(250, 204, 21, .08);
    }
    .stock-card-mid {
      display: grid;
      grid-template-columns: minmax(90px, .8fr) minmax(120px, 1fr);
      gap: 10px;
      align-items: center;
    }
    .stock-price {
      color: #ff3030;
      font-size: 24px;
      font-weight: 900;
      letter-spacing: .02em;
    }
    .stock-change {
      display: block;
      margin-top: 4px;
      font-size: 13px;
      font-weight: 900;
    }
    .stock-spark {
      width: 100%;
      height: 54px;
      display: block;
      overflow: visible;
    }
    .stock-spark.up polyline {
      stroke: #ff3030;
    }
    .stock-spark.down polyline {
      stroke: #00d97e;
    }
    .stock-spark.flat polyline {
      stroke: #94a3b8;
    }
    .stock-card-foot {
      border-top: 1px solid rgba(148, 163, 184, .14);
      padding-top: 10px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      color: #9ca3af;
      font-weight: 800;
    }
    .stock-ohlc {
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      font-size: 13px;
    }
    .stock-ohlc b {
      color: #e5edf6;
      font-weight: 900;
    }
    .stock-actions {
      display: flex;
      gap: 8px;
      justify-content: flex-end;
      flex-wrap: wrap;
      justify-self: stretch;
    }
    .stock-card .drag-handle {
      width: auto;
      color: #7dd3fc;
      text-align: left;
    }
    .stock-card button.compact {
      background: rgba(226, 246, 255, .96);
      color: #062337;
      border-color: rgba(125, 211, 252, .54);
    }
    .stock-card button.compact.danger {
      background: rgba(127, 29, 29, .76);
      color: #fee2e2;
      border-color: rgba(248, 113, 113, .50);
    }
    .rank-list-table,
    .rank-list-table tbody,
    .rank-list-table tr,
    .rank-list-table td {
      display: block;
      width: 100%;
      border: 0;
      padding: 0;
    }
    .rank-list-table thead {
      display: none;
    }
    .rank-panel {
      overflow: hidden;
      border-radius: 16px;
      background:
        radial-gradient(circle at 88% 8%, rgba(34, 197, 94, .10), transparent 18%),
        linear-gradient(180deg, rgba(15, 23, 42, .96), rgba(7, 10, 22, .98));
      border: 1px solid rgba(71, 85, 105, .48);
      box-shadow: 0 20px 45px rgba(2, 6, 23, .28), inset 0 1px 0 rgba(255,255,255,.04);
    }
    .rank-panel-head,
    .rank-row {
      display: grid;
      grid-template-columns: 42px minmax(120px, 1fr) minmax(86px, .58fr) minmax(104px, .6fr) 16px;
      gap: 12px;
      align-items: center;
    }
    .rank-panel-head {
      padding: 16px 18px;
      border-bottom: 1px solid rgba(148, 163, 184, .08);
    }
    .rank-title {
      grid-column: 1 / 4;
      display: flex;
      align-items: center;
      gap: 10px;
      color: #f8fafc;
      font-size: 19px;
      font-weight: 900;
    }
    .rank-icon {
      display: grid;
      place-items: center;
      width: 28px;
      height: 28px;
      border-radius: 10px;
      color: #38bdf8;
      background: rgba(14, 165, 233, .10);
      border: 1px solid rgba(56, 189, 248, .22);
    }
    .rank-more {
      grid-column: 4 / 6;
      justify-self: end;
      height: 30px;
      padding: 0 10px;
      color: #cbd5e1;
      background: rgba(15, 23, 42, .72);
      border-color: rgba(71, 85, 105, .58);
    }
    .rank-row {
      position: relative;
      min-height: 82px;
      padding: 12px 18px;
      cursor: pointer;
      background: linear-gradient(90deg, rgba(239, 68, 68, .10), rgba(15, 23, 42, 0) 55%);
      transition: background .16s ease, transform .16s ease;
    }
    .rank-row:hover {
      background: linear-gradient(90deg, rgba(20, 184, 166, .13), rgba(15, 23, 42, .08) 65%);
      transform: translateX(2px);
    }
    .rank-index {
      display: grid;
      place-items: center;
      width: 30px;
      height: 30px;
      border-radius: 50%;
      color: #e2e8f0;
      background: rgba(51, 65, 85, .72);
      border: 1px solid rgba(148, 163, 184, .14);
      font-weight: 900;
    }
    .rank-index.top {
      color: #fbbf24;
      background: rgba(180, 83, 9, .28);
      border-color: rgba(245, 158, 11, .36);
    }
    .rank-name strong,
    .rank-price strong {
      display: block;
      color: #ffffff;
      font-size: 18px;
      line-height: 1.2;
    }
    .rank-name span,
    .rank-metric,
    .rank-price span {
      display: block;
      margin-top: 4px;
      color: #94a3b8;
      font-size: 13px;
      font-weight: 800;
    }
    .rank-price strong {
      color: #ff3030;
      font-family: Consolas, "SFMono-Regular", monospace;
    }
    .rank-spark {
      min-width: 0;
    }
    .rank-spark .stock-spark {
      height: 38px;
    }
    .rank-led {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #22c55e;
      box-shadow: 0 0 14px rgba(34, 197, 94, .88);
    }
    @media (max-width: 1180px) {
      .desk-side {
        grid-template-columns: repeat(6, minmax(0, 1fr));
      }
      .desk-side .info-panel.panel-compact {
        grid-column: span 3 !important;
      }
      .desk-side .panel-diagnosis,
      .desk-side .panel-trade,
      .desk-side .panel-theory,
      .desk-side .panel-summary,
      .desk-side .panel-indicators,
      .desk-side .panel-facets,
      .desk-side .panel-institutional {
        grid-column: span 3;
      }
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
      .hero-panel, .hero-stat-grid, .market-strip, .module-grid, .diagnosis, .watch-grid, .strategy-advice, .strategy-kpis, .manager-grid, .manager-scenario-grid, .strategy-stock-grid, .strategy-guide, .trading-desk, .desk-side, .professional-grid, .trade-kpis, .technical-strip, .fundamental-grid, .fundamental-visuals, .research-command, .research-score-row, .research-list-grid, .news-grid, .inner-grid, .explore-hero, .trend-summary, .realtime-terminal, .facet-grid, .institution-grid, .realtime-list-table tbody { grid-template-columns: 1fr; }
      .terminal-chart, .terminal-watch { grid-column: auto; grid-row: auto; }
      .terminal-tape, .terminal-depth { grid-column: auto; grid-row: auto; }
      .desk-side .panel-diagnosis,
      .desk-side .panel-trade,
      .desk-side .panel-theory,
      .desk-side .panel-summary,
      .desk-side .panel-indicators,
      .desk-side .panel-facets,
      .desk-side .panel-institutional {
        grid-column: auto;
      }
      .desk-side .info-panel.panel-compact {
        grid-column: auto !important;
      }
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
      .stock-card-table tbody { grid-template-columns: 1fr; padding: 10px; }
      .stock-actions button { width: auto; flex: 1 1 auto; }
      .rank-panel-head,
      .rank-row {
        grid-template-columns: 32px minmax(0, 1fr) minmax(82px, auto) 12px;
        gap: 8px;
      }
      .rank-title { grid-column: 1 / 3; }
      .rank-more { grid-column: 3 / 5; }
      .rank-spark { display: none; }
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
      <div class="subtitle">專業台股智能分析平台 · 訊號排行 · AI 經理人 · 風控紀律</div>
    </div>
    <div class="version"><span id="range">載入中...</span> · UI v56</div>
  </header>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="sidebar-title">功能導覽</div>
      <nav class="nav-list">
        <a class="nav-item active" data-page="overview" href="#" onclick="showPage('overview', this); return false;"><span class="nav-icon">OV</span><span>總覽</span></a>
        <a class="nav-item" data-page="premarketPage" href="#" onclick="showPage('premarketPage', this); return false;"><span class="nav-icon">AM</span><span>盤前分析</span></a>
        <a class="nav-item" data-page="realtimePage" href="#" onclick="showPage('realtimePage', this); return false;"><span class="nav-icon">RT</span><span>即時看盤</span></a>
        <a class="nav-item" data-page="watchPage" href="#" onclick="showPage('watchPage', this); return false;"><span class="nav-icon">MK</span><span>盤後看盤</span></a>
        <a class="nav-item" data-page="strategyPage" href="#" onclick="showPage('strategyPage', this); return false;"><span class="nav-icon">PM</span><span>AI 經理人</span></a>
        <a class="nav-item" data-page="stockPage" href="#" onclick="showPage('stockPage', this); return false;"><span class="nav-icon">ST</span><span>個股分析</span></a>
        <a class="nav-item" data-page="hubPage" href="#" onclick="showPage('hubPage', this); return false;"><span class="nav-icon">HB</span><span>股票探索</span></a>
        <a class="nav-item" data-page="guidePage" href="#" onclick="showPage('guidePage', this); return false;"><span class="nav-icon">GD</span><span>使用手冊</span></a>
        <a class="nav-item" data-page="notifyPage" href="#" onclick="showPage('notifyPage', this); return false;"><span class="nav-icon">TG</span><span>推播設定</span></a>
      </nav>
    </aside>
  <main class="workspace">
    <div class="dashboard-note overview-only">
      <div>
        <strong>智能投資決策中心</strong><br>
        <span>整合上市櫃資料、AI 訊號分數、AI 經理人決策與 Telegram 盤中/盤後推播，協助快速建立每日操作紀律。</span>
      </div>
      <div class="pill">V1 研究模式</div>
    </div>
    <div class="toolbar overview-only">
      <input id="code" value="2330" aria-label="股票代號">
      <button type="button" class="primary" id="loadStock" onclick="searchStock()">智能搜尋</button>
      <button type="button" id="loadHub" onclick="showPage('hubPage')">股票探索</button>
      <button type="button" id="loadStrategy" onclick="runAction(fetchStrategy, '正在更新 AI 經理人決策...')">AI 經理人</button>
      <button type="button" id="refreshStatus" onclick="runAction(fetchStatus, '正在更新狀態...')">更新狀態</button>
    </div>
    <div class="status" id="statusLine">準備就緒。</div>

    <div class="page active" id="overview">
      <div class="hero-panel">
        <div>
          <div class="eyebrow">TAIWAN EQUITY RESEARCH DESK</div>
          <h2>今日台股研究工作台</h2>
          <p>從盤勢廣度、AI 訊號、觀察名單與經理人風控切入，先判斷市場環境，再決定要進攻、精選或防守。</p>
          <div class="hero-actions">
            <button type="button" class="primary" onclick="showPage('hubPage')">股票探索</button>
            <button type="button" onclick="showPage('watchPage')">打開觀察名單</button>
            <button type="button" onclick="showPage('premarketPage')">盤前分析</button>
            <button type="button" onclick="showPage('realtimePage')">即時看盤</button>
            <button type="button" onclick="showPage('strategyPage')">AI 經理人中心</button>
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
        <div class="market-tile accent"><span>AI 經理人</span><strong>組合決策</strong></div>
        <div class="market-tile blue"><span>即時看盤</span><strong>觀察名單同步</strong></div>
        <div class="market-tile gold"><span>股票探索</span><strong>AI 智選排行</strong></div>
        <div class="market-tile cyan"><span>個股分析</span><strong>K 線與技術圖</strong></div>
      </div>
      <div class="grid">
        <section class="wide">
          <h2>今日研究流程</h2>
          <div class="content"><dl>
            <dt>第一步</dt><dd>先看 AI 經理人判斷目前盤面是進攻、精選或防守</dd>
            <dt>第二步</dt><dd>用股票探索與觀察名單縮小股票池</dd>
            <dt>第三步</dt><dd>進入個股 K 線、均線、RSI、MACD、KD 和布林通道確認風險</dd>
          </dl></div>
        </section>
        <section class="wide">
          <h2>功能分流</h2>
          <div class="content"><dl>
            <dt>股票探索</dt><dd>集中查看 AI 智選、強勢股票與量能焦點</dd>
            <dt>盤後看盤</dt><dd>只保留觀察名單與盤後摘要，避免資訊重複</dd>
            <dt>盤前分析</dt><dd>早上 08:30 整合美股期貨、半導體權值、台積 ADR、台股連動 ETF、匯率與 24H 新聞風險</dd>
            <dt>即時看盤</dt><dd>盤中報價、即時走勢與 AI 盤中盯盤</dd>
            <dt>AI 經理人</dt><dd>查看每日 AI 建倉、減碼、停損、未平倉與資金曲線</dd>
          </dl></div>
        </section>
      </div>
    </div>

    <div class="page" id="hubPage">
      <div class="explore-hero">
        <div>
          <h2>股票探索工作台</h2>
          <p>從 AI 智選、強勢排行與量能焦點挑股票，直接在名單中點「分析」進入 K 線、技術圖與交易計畫。</p>
        </div>
      </div>
      <div class="module-grid">
        <div class="module-card"><strong>AI 智選</strong><span>用風險調整分數排序，優先找動能、趨勢與量能較完整的股票。</span></div>
        <div class="module-card"><strong>強勢排行</strong><span>追蹤 20 日漲幅，快速看到市場資金偏好的族群。</span></div>
        <div class="module-card"><strong>量能焦點</strong><span>找出成交量突然放大的股票，再回到個股頁確認型態。</span></div>
      </div>
      <section class="wide">
        <h2>產業類股分類</h2>
        <div class="content">
          <div class="toolbar" style="padding:0;border:0;background:transparent">
            <select id="industrySelect" aria-label="選擇產業" onchange="fetchIndustryStocks(this.value)"></select>
            <button type="button" onclick="fetchIndustryStocks()">刷新產業</button>
          </div>
          <div id="industrySummary" class="note"></div>
          <div id="industryStockCards"></div>
        </div>
      </section>
      <div class="grid">
        <section>
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
        <button type="button" onclick="runAction(syncCurrentWatchlistToPush, '正在同步推播名單...')">同步到推播</button>
        <button type="button" onclick="runAction(fetchWatch, '正在刷新觀察名單...')">刷新名單</button>
        <div class="hint" id="watchlistHint">觀察名單會保存在本機資料庫。</div>
      </div>
      <div class="grid">
        <section class="wide">
          <h2>我的觀察名單</h2>
          <div class="table-wrap"><table id="watchMajorsTable"></table></div>
        </section>
        <section class="wide">
          <h2>各大類股盤後分析</h2>
          <div class="content" id="watchAfterSummary"></div>
        </section>
      </div>
    </div>

    <div class="page" id="premarketPage">
      <div class="dashboard-note realtime-board">
        <div>
          <strong>盤前分析工作台</strong><br>
          <span>整合美股期貨、費半、AI/半導體權值、台積 ADR、台股 ETF、匯率與 24H 新聞，預估早盤節奏。</span>
        </div>
        <div class="realtime-actions">
          <button type="button" onclick="runAction(() => fetchPremarket(true), '正在更新盤前分析...')">刷新盤前分析</button>
          <button type="button" onclick="showPage('realtimePage')">前往即時看盤</button>
        </div>
      </div>
      <div class="status" id="premarketNotice">尚未載入盤前分析。</div>
      <div class="module-grid" id="premarketKpis"></div>
      <div class="grid">
        <section class="wide">
          <h2>海外與夜盤連動</h2>
          <div class="table-wrap"><table id="premarketFactorsTable"></table></div>
        </section>
        <section class="wide">
          <h2>觀察名單早盤計畫</h2>
          <div class="table-wrap"><table id="premarketWatchTable"></table></div>
        </section>
        <section class="wide">
          <h2>24H 新聞風險</h2>
          <div id="premarketNews" class="news-grid"></div>
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
        <div class="explore-hero stock-command">
          <div>
            <h2 id="stockHeroName">個股分析工作台</h2>
            <p id="stockHeroSub">輸入股票代號後分成「解盤」與「專業分析」，快速看交易判斷，再看法人、基本面與消息。</p>
          </div>
          <div class="explore-actions">
            <input id="stockPageCode" value="2330" aria-label="個股分析股票代號">
            <button type="button" class="primary" onclick="searchStockFromPanel()">分析個股</button>
            <button type="button" onclick="runAction(fetchStock, '正在刷新個股分析...')">刷新</button>
          </div>
        </div>
      </section>
      <div class="wide trading-desk">
        <div class="stock-part-title">
          <span>1</span>
          <div><strong>解盤</strong><small>K 線、AI 診斷、交易計畫、技術指標與風險構面。</small></div>
        </div>
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
                <button type="button" onclick="showChipAttachment('dealer')">自營商</button>
                <button type="button" onclick="showChipAttachment('total')">法人合計</button>
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
          <section class="desk-panel panel-diagnosis">
            <h2>AI 個股診斷</h2>
            <div class="content">
              <div class="diagnosis">
                <div class="diagnosis-card"><span>智研評分</span><strong id="diagScore">-</strong></div>
                <div class="diagnosis-card"><span>市場情緒</span><strong id="diagSentiment">-</strong></div>
                <div class="diagnosis-card"><span>風險分數</span><strong id="diagRiskScore">-</strong></div>
              </div>
            </div>
          </section>
          <section class="desk-panel panel-trade">
            <h2>交易計畫</h2>
            <div id="tradePlan" class="trade-plan"></div>
          </section>
          <section class="desk-panel panel-theory">
            <h2>技術理論解盤</h2>
            <div id="technicalTheoryAnalysis" class="trade-plan"></div>
          </section>
          <section class="desk-panel panel-facets">
            <h2>AI 構面總覽</h2>
            <div id="analysisFacets" class="trade-plan"></div>
          </section>
          <section class="desk-panel panel-summary">
            <h2>個股資料</h2>
            <div class="content"><dl id="stockSummary"></dl></div>
          </section>
          <section class="desk-panel panel-indicators">
            <h2>技術指標</h2>
            <div class="content"><dl id="indicators"></dl></div>
          </section>
        </div>
        <div class="stock-part-title">
          <span>2</span>
          <div><strong>專業分析</strong><small>先看機構級研究，再看資金、財報補齊與消息驗證。</small></div>
        </div>
        <div class="professional-grid">
          <section class="desk-panel wide">
            <h2>機構級投資研究</h2>
            <div id="fundamentalResearch" class="fundamental-research"></div>
          </section>
          <section class="desk-panel wide panel-institutional">
            <h2>資金與基金追蹤</h2>
            <div id="institutionalProAnalysis" class="trade-plan"></div>
            <div id="fundHoldingReports" class="trade-plan compact-block"></div>
          </section>
          <section class="desk-panel wide">
            <h2>資料補齊與財報 KPI</h2>
            <div id="analysisCoverage" class="trade-plan"></div>
            <div id="financialKpiPanel" class="trade-plan compact-block"></div>
          </section>
          <section class="desk-panel wide">
            <h2>最新消息掃描</h2>
            <div id="newsResearch" class="news-grid"></div>
          </section>
        </div>
      </div>
      </div>
    </div>

    <div class="page" id="strategyPage">
      <div class="grid">
      <section class="wide">
        <h2>AI 經理人決策總覽</h2>
        <div class="content" id="strategyAdvice"></div>
      </section>
      <section class="wide">
        <h2>AI 經理人候選觀察</h2>
        <div class="content">
          <div id="strategyStockCards"></div>
        </div>
      </section>
      <section class="wide">
        <h2>AI 經理人組合摘要</h2>
        <div class="content"><dl id="strategySummary"></dl></div>
      </section>
      <section class="wide" data-strategy-section="open">
        <h2>AI 經理人未平倉</h2>
        <div class="table-wrap"><table id="strategyOpenTable"></table></div>
      </section>
      <section class="wide" data-strategy-section="trades">
        <h2>AI 經理人買賣紀錄</h2>
        <div class="grid">
          <section>
            <h2>建倉紀錄</h2>
            <div class="table-wrap"><table id="strategyEntriesTable"></table></div>
          </section>
          <section>
            <h2>了結紀錄</h2>
            <div class="table-wrap"><table id="strategyTradesTable"></table></div>
          </section>
        </div>
      </section>
      <section class="wide">
        <h2>資金曲線</h2>
        <div class="content"><svg id="equityChart" class="chart" role="img" aria-label="AI 經理人資金曲線"></svg></div>
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
      <section>
        <h2>20 日漲幅排行</h2>
        <div class="table-wrap"><table id="returnTable"></table></div>
      </section>
      <section>
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
            <dt>Telegram 綁定</dt><dd>朋友可先建立個人設定，再到 Telegram 對 bot 輸入「綁定 個人代碼」。若直接對 bot 輸入 /start，系統也會自動建立個人帳號。</dd>
            <dt>推播內容</dt><dd>個人觀察名單、AI 分數、買點、賣點、停損與操作提醒</dd>
            <dt>自動排程</dt><dd>盤中 09:00-14:00 發送個人 AI 盯盤；盤後發送個人觀察名單報告。</dd>
          </dl></div>
        </section>
        <section class="wide">
          <h2>Telegram 綁定教學</h2>
          <div class="content"><dl>
            <dt>方式一：最快綁定</dt><dd>1. 打開 Telegram。2. 搜尋你的機器人。3. 對機器人輸入 /start。4. 系統會自動建立個人帳號並回覆個人代碼。5. 回網站加入自己的觀察名單。</dd>
            <dt>方式二：先用網站建帳號</dt><dd>1. 在本頁輸入名稱。2. 按「建立個人設定」。3. 複製畫面上的個人代碼。4. 到 Telegram 對機器人輸入「綁定 個人代碼」。例：綁定 abc123。5. 回本頁確認推播狀態。</dd>
            <dt>測試推播</dt><dd>綁定後按「發送測試推播」。收到訊息代表設定完成；之後盤中與盤後排程會依照個人觀察名單推播。</dd>
            <dt>朋友怎麼加入股票</dt><dd>可在網站「盤後看盤」加入與拖曳排序；也可在 Telegram 輸入「加入 2330」、「移除 2330」、「我的觀察名單」。</dd>
            <dt>收不到訊息</dt><dd>先確認朋友有對 bot 按 Start 或輸入 /start；Telegram 不允許 bot 主動傳給從未互動過的人。再確認推播狀態是否已啟用，最後按測試推播確認。</dd>
            <dt>安全提醒</dt><dd>個人代碼只用來綁定自己的觀察名單，請不要公開貼到群組。若綁錯，可重新建立個人設定或重新綁定。</dd>
          </dl></div>
        </section>
        <section class="wide">
          <h2>管理員後台查詢</h2>
          <div class="toolbar">
            <input id="adminToken" type="password" placeholder="管理員 token" aria-label="管理員 token">
            <button type="button" onclick="runAction(fetchAdminUsers, '正在查詢朋友推播名單...')">查詢朋友名單</button>
          </div>
          <div class="toolbar">
            <input id="telegramWebhookUrl" value="https://taiwan-stock-ai-g6cf.onrender.com/api/telegram/webhook" aria-label="Telegram webhook URL">
            <button type="button" onclick="runAction(setAdminTelegramWebhook, '正在設定 Telegram webhook...')">設定 Telegram webhook</button>
          </div>
          <div class="hint">只供主機管理者使用。可查看朋友帳號、Telegram 綁定狀態、觀察名單數量與股票代號。</div>
          <div class="content"><dl id="adminUserSummary"></dl></div>
          <div class="table-wrap"><table id="adminUsersTable"></table></div>
        </section>
      </div>
    </div>

    <div class="page" id="guidePage">
      <div class="grid">
        <section class="wide">
          <h2>使用手冊</h2>
          <div class="content"><dl>
            <dt>快速入門</dt><dd>先看總覽，再用智能搜尋查個股，最後檢查 AI 訊號與 AI 經理人。</dd>
            <dt>AI 智能分析</dt><dd>智研評分綜合技術面、量價籌碼、消息面、產業面、風險面與資金面；三大法人集中在法人籌碼面板。</dd>
            <dt>AI 經理人</dt><dd>每日依最新資料建立基金經理人式決策，包含建倉、出場、持倉、風控與資金曲線。</dd>
            <dt>推播設定</dt><dd>08:30 盤前分析、盤中盯盤、盤後摘要與 24H 突發新聞都可透過本機 Telegram 推播。</dd>
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
    let cloudWebMode = false;
    const rankListState = {};
    let latestCoverageContext = {};
    async function getJson(url) {
      const response = await fetch(url, { cache: "no-store" });
      const text = await response.text();
      if (!text.trim()) {
        throw new Error(`伺服器暫時沒有回應：${url}`);
      }
      let data;
      try {
        data = JSON.parse(text);
      } catch (error) {
        throw new Error(`伺服器回傳格式異常：${url}`);
      }
      if (!response.ok || data.error) throw new Error(data.error || "請求失敗");
      return data;
    }
    async function initPublicConfig() {
      try {
        const config = await getJson("/api/public-config");
        publicDemoMode = !!config.public_demo;
        cloudWebMode = !!config.cloud_web;
      } catch (error) {
        publicDemoMode = false;
        cloudWebMode = false;
      }
      applyCloudWebMode();
      await loadUserProfile();
    }
    function applyCloudWebMode() {
      if (!cloudWebMode) return;
      document.body.classList.add("cloud-web-mode");
      document.querySelectorAll('[data-page="notifyPage"]').forEach(item => item.remove());
      document.querySelectorAll('[data-page="guidePage"]').forEach(item => item.remove());
      const notifyPage = document.getElementById("notifyPage");
      if (notifyPage) notifyPage.remove();
      const guidePage = document.getElementById("guidePage");
      if (guidePage) guidePage.remove();
      const syncPushButton = Array.from(document.querySelectorAll("button")).find(button => button.textContent.trim() === "同步到推播");
      if (syncPushButton) syncPushButton.remove();
      replaceText("AI 經理人", "AI 回測");
      replaceText("與 Telegram 盤中/盤後推播", "");
      replaceText("提醒管道", "雲端模式");
      replaceText("Telegram", "網頁查詢");
      const subtitle = document.querySelector(".subtitle");
      if (subtitle) subtitle.textContent = "專業台股智能分析平台 · 訊號排行 · AI 回測 · 盤前與即時看盤";
    }
    function replaceText(from, to) {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach(node => {
        if (node.nodeValue.includes(from)) node.nodeValue = node.nodeValue.split(from).join(to);
      });
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
    function renderedWatchlistCodes() {
      return Array.from(document.querySelectorAll("#watchMajorsTable tr[data-watch-code]"))
        .map(row => row.dataset.watchCode)
        .filter(Boolean);
    }
    async function currentWatchlistCodesForSync() {
      const renderedCodes = renderedWatchlistCodes();
      if (renderedCodes.length) return renderedCodes;
      if (publicDemoMode && !currentUserKey) return localWatchlistCodes();
      const data = await getWatchlistData();
      return data.codes || (data.watchlist || []).map(row => row.code).filter(Boolean);
    }
    async function syncUserWatchlistCodes(codes, quiet = false) {
      const clean = [...new Set((codes || []).map(code => String(code).trim()).filter(Boolean))];
      if (!clean.length) throw new Error("目前沒有可同步的關注股票。");
      let data;
      if (currentUserKey) {
        data = await getJson(`/api/user/watchlist/sync?user_key=${encodeURIComponent(currentUserKey)}&codes=${encodeURIComponent(clean.join(","))}`);
      } else if (publicDemoMode) {
        saveLocalWatchlist(clean);
        data = await getJson(`/api/watchlist?codes=${encodeURIComponent(clean.join(","))}`);
        data.message = `已同步 ${clean.length} 檔到此瀏覽器名單；公開展示模式不會修改本機主機推播。`;
      } else {
        data = await getJson(`/api/watchlist/sync?codes=${encodeURIComponent(clean.join(","))}`);
      }
      const table = document.getElementById("watchMajorsTable");
      if (table) renderMajors(table, data.watchlist || []);
      if (!quiet) {
        const hint = document.getElementById("watchlistHint");
        if (hint) hint.textContent = data.message || `已同步 ${clean.length} 檔到推播名單。`;
      }
      return data;
    }
    async function syncCurrentWatchlistToPush() {
      const codes = await currentWatchlistCodesForSync();
      await syncUserWatchlistCodes(codes);
    }
    function renderDl(target, rows) {
      target.innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    }
    function setStockCards(target) {
      target.classList.remove("rank-list-table");
      target.classList.remove("realtime-list-table");
      target.classList.add("stock-card-table");
      target.classList.remove("compact-card-table");
    }
    function setRankList(target) {
      target.classList.remove("stock-card-table");
      target.classList.remove("realtime-list-table");
      target.classList.add("rank-list-table");
    }
    function setRealtimeList(target) {
      target.classList.remove("stock-card-table");
      target.classList.remove("compact-card-table");
      target.classList.remove("rank-list-table");
      target.classList.add("realtime-list-table");
    }
    function stockCard(row, options = {}) {
      const code = row.code || "";
      const name = row.short_name || row.name || "";
      const price = row.price ?? row.close;
      const change = row.change ?? row.return_5d ?? row.return_20d;
      const changePercent = row.change_percent ?? row.return_20d;
      const cardClass = changePercent > 0 ? "positive-card" : changePercent < 0 ? "negative-card" : "";
      const market = row.market || "TWSE";
      const tag = row.industry || row.signal || row.sentiment || options.tag || "智慧觀察";
      const open = row.open ?? row.close ?? row.price;
      const high = row.high ?? row.resistance ?? row.close ?? row.price;
      const low = row.low ?? row.stop ?? row.close ?? row.price;
      const spark = miniSparkline(row, changePercent);
      const extra = options.extra ? `<span class="stock-tag hot">${options.extra}</span>` : "";
      const labels = [options.leftLabel, options.rightLabel].filter(Boolean);
      const labelHtml = labels.length ? labels.map(text => `<span>${text}</span>`).join("") : "";
      return `
        <div class="stock-card ${cardClass} ${options.selected ? "selected-card" : ""} ${options.compact ? "compact-stock-card" : ""}">
          <div class="stock-card-top">
            <div>
              <h3>${name}</h3>
              <small>${code}</small>
              <div class="stock-tags"><span class="stock-tag">${market}</span><span class="stock-tag hot">${tag}</span>${extra}</div>
            </div>
            <span class="stock-led"></span>
          </div>
          <div class="stock-card-mid">
            <div>
              <div class="stock-price">${fmt(price)}</div>
              <span class="stock-change ${pctClass(changePercent)}">${fmt(change)} ｜ ${fmt(changePercent)}%</span>
            </div>
            ${spark}
          </div>
          <div class="stock-card-foot">
            ${labelHtml}
            <div class="stock-ohlc">
              <span>開 <b class="${pctClass(Number(open) - Number(price))}">${fmt(open)}</b></span>
              <span>高 <b class="positive">${fmt(high)}</b></span>
              <span>低 <b class="negative">${fmt(low)}</b></span>
            </div>
          </div>
          <div class="stock-actions">${options.actions || `<button type="button" class="compact" onclick="openStock('${code}')">分析</button>`}</div>
        </div>`;
    }
    function miniSparkline(row, changePercent) {
      const width = 150;
      const height = 58;
      const values = (row.sparkline || []).map(Number).filter(Number.isFinite);
      if (values.length < 2) {
        return `<svg class="stock-spark flat" viewBox="0 0 ${width} ${height}" aria-hidden="true">
          <polyline points="8,29 ${width - 8},29" fill="none" stroke-width="2.2"></polyline>
        </svg>`;
      }
      const min = Math.min(...values);
      const max = Math.max(...values);
      const span = max - min || Math.max(Math.abs(max), 1) * 0.01;
      const points = values.map((value, index) => {
        const x = 8 + index * ((width - 16) / (values.length - 1));
        const y = 8 + (max - value) * ((height - 16) / span);
        return `${x.toFixed(1)},${Math.max(8, Math.min(height - 8, y)).toFixed(1)}`;
      }).join(" ");
      const direction = values[values.length - 1] > values[0] ? "up" : values[values.length - 1] < values[0] ? "down" : "flat";
      return `<svg class="stock-spark ${direction}" viewBox="0 0 ${width} ${height}" aria-hidden="true">
        <polyline points="${points}" fill="none" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"></polyline>
      </svg>`;
    }
    function renderRankList(target, rows, options = {}) {
      rows = rows || [];
      setRankList(target);
      const key = target.id || options.title || "rank";
      const expanded = !!rankListState[key];
      const visible = expanded ? rows : rows.slice(0, 5);
      const canExpand = rows.length > 5;
      const title = options.title || "排行榜";
      const icon = options.icon || "↗";
      const metric = options.metric || ((row) => `量比 ${fmt(row.volume_ratio)}`);
      target.innerHTML = `
        <thead><tr><th>${title}</th></tr></thead>
        <tbody><tr><td>
          <div class="rank-panel">
            <div class="rank-panel-head">
              <div class="rank-title"><span class="rank-icon">${icon}</span><span>${title}</span></div>
              ${canExpand ? `<button type="button" class="rank-more" onclick="toggleRankList('${key}')">${expanded ? "收起" : `看全部 ${rows.length}`}</button>` : ""}
            </div>
            <div class="rank-list">
              ${visible.map((row, index) => {
                const changePercent = row.change_percent ?? row.return_20d;
                const price = row.price ?? row.close;
                const change = row.change ?? row.return_5d ?? row.return_20d;
                return `
                  <div class="rank-row" onclick="openStock('${row.code}')" title="點擊分析 ${row.code}">
                    <span class="rank-index ${index < 3 ? "top" : ""}">${index + 1}</span>
                    <div class="rank-name"><strong>${row.short_name || row.name}</strong><span>${row.code}</span></div>
                    <div class="rank-price"><strong>${fmt(price)}</strong><span class="${pctClass(changePercent)}">${fmt(change)} ｜ ${fmt(changePercent)}%</span></div>
                    <div class="rank-spark">${miniSparkline(row, changePercent)}<span class="rank-metric">${metric(row)}</span></div>
                    <span class="rank-led"></span>
                  </div>`;
              }).join("") || `<div class="rank-row"><div class="rank-name"><strong>暫無資料</strong><span>請稍後刷新</span></div></div>`}
            </div>
          </div>
        </td></tr></tbody>`;
    }
    function toggleRankList(key) {
      rankListState[key] = !rankListState[key];
      if (key === "returnTable") runAction(fetchScan, "正在更新市場排行榜...");
      if (key === "volumeTable") runAction(fetchScan, "正在更新市場排行榜...");
      if (key === "hubSignalsTable" || key === "hubReturnTable" || key === "hubVolumeTable") runAction(fetchHub, "正在更新股票探索...");
    }
    function renderIndustryStocks(target, rows) {
      const cards = (rows || []).map((row, index) => stockCard(row, {
        selected: index === 0,
        extra: index === 0 ? "龍頭" : `量比 ${fmt(row.volume_ratio)}`,
        tag: row.industry || "產業",
        leftLabel: `當日 ${fmt(row.change_percent)}%`,
        rightLabel: `20日 ${fmt(row.return_20d)}%`,
        actions: `<button type="button" class="compact" onclick="openStock('${row.code}')">分析</button>`
      })).join("");
      target.innerHTML = `<div class="strategy-stock-grid">${cards || "<div class='note'>請先選擇產業。</div>"}</div>`;
    }
    async function fetchIndustryStocks(industry = "") {
      const select = document.getElementById("industrySelect");
      const selectedValue = industry || (select ? select.value : "");
      const data = await getJson(`/api/industries?industry=${encodeURIComponent(selectedValue)}&limit=40`);
      if (select) {
        const options = (data.industries || []).map(item => `<option value="${item.name}" ${item.name === data.selected ? "selected" : ""}>${item.name}（${fmt(item.count, 0)}）</option>`).join("");
        select.innerHTML = options;
      }
      const summary = data.summary || {};
      const leader = summary.leader || {};
      document.getElementById("industrySummary").textContent = `${data.selected || "產業"}｜${fmt(summary.count, 0)} 檔｜平均當日 ${fmt(summary.avg_change_percent)}%｜龍頭 ${leader.short_name || leader.name || "-"} ${leader.code || ""}`;
      renderIndustryStocks(document.getElementById("industryStockCards"), data.items || []);
    }
    function renderTable(target, rows) {
      rows = rows || [];
      setStockCards(target);
      target.innerHTML = `
        <thead><tr><th>股票卡片</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr class="stock-card-row"><td>${stockCard(row, { extra: `量比 ${fmt(row.volume_ratio)}` })}</td></tr>
        `).join("")}</tbody>`;
    }
    function renderSignals(target, rows) {
      rows = rows || [];
      setStockCards(target);
      target.innerHTML = `
        <thead><tr><th>股票卡片</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr class="stock-card-row"><td>${stockCard(row, {
            extra: `AI ${fmt(row.score, 0)}`,
            leftLabel: row.entry_zone ? `建 ${row.entry_zone}` : "AI 智選",
            rightLabel: row.exit_zone ? `出 ${row.exit_zone}` : "我看好的",
            actions: `<button type="button" class="compact" onclick="openStock('${row.code}')">分析</button>`
          })}</td></tr>
        `).join("")}</tbody>`;
    }
    function renderMajors(target, rows) {
      rows = rows || [];
      setStockCards(target);
      if (!rows.length) {
        target.innerHTML = `
          <thead><tr><th>狀態</th></tr></thead>
          <tbody><tr><td>尚未加入觀察股票，請在上方輸入股票代號。</td></tr></tbody>`;
        return;
      }
      target.innerHTML = `
        <thead><tr><th>股票卡片</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr class="stock-card-row" draggable="true" data-watch-code="${row.code}">
            <td>${stockCard(row, {
              extra: row.signal || "觀察",
              leftLabel: row.buy_zone ? `買點 ${row.buy_zone}` : "拖曳排序",
              rightLabel: row.sell_zone ? `賣點 ${row.sell_zone}` : "我看好的",
              actions: `
                <span class="drag-handle" title="拖曳調整順序">☰</span>
                <button type="button" class="compact" onclick="openStock('${row.code}')">分析</button>
                <button type="button" class="compact danger" onclick="removeWatchlistCode('${row.code}')">移除</button>`
            })}</td>
          </tr>
        `).join("")}</tbody>`;
      syncRealtimeCodes(rows.map(row => row.code));
      enableWatchlistDrag(target);
    }
    function enableWatchlistDrag(table) {
      const tbody = table.querySelector("tbody");
      if (!tbody) return;
      let dragged = null;
      tbody.querySelectorAll("tr[data-watch-code]").forEach(row => {
        row.addEventListener("dragstart", event => {
          dragged = row;
          row.classList.add("watch-dragging");
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", row.dataset.watchCode || "");
        });
        row.addEventListener("dragend", () => {
          row.classList.remove("watch-dragging");
          tbody.querySelectorAll("tr").forEach(item => item.classList.remove("watch-drop-before", "watch-drop-after"));
          saveWatchlistOrderFromTable(table);
        });
        row.addEventListener("dragover", event => {
          event.preventDefault();
          if (!dragged || dragged === row) return;
          const rect = row.getBoundingClientRect();
          const after = event.clientY > rect.top + rect.height / 2;
          row.classList.toggle("watch-drop-before", !after);
          row.classList.toggle("watch-drop-after", after);
          tbody.insertBefore(dragged, after ? row.nextSibling : row);
        });
        row.addEventListener("dragleave", () => {
          row.classList.remove("watch-drop-before", "watch-drop-after");
        });
      });
    }
    async function saveWatchlistOrderFromTable(table) {
      const codes = Array.from(table.querySelectorAll("tr[data-watch-code]")).map(row => row.dataset.watchCode).filter(Boolean);
      if (!codes.length) return;
      syncRealtimeCodes(codes);
      const message = await persistWatchlistOrder(codes);
      document.getElementById("watchlistHint").textContent = message || `已更新排序，目前觀察 ${codes.length} 檔。`;
    }
    function syncRealtimeCodes(codes) {
      const input = document.getElementById("realtimeCodes");
      if (input) input.value = codes.join(",");
    }
    async function persistWatchlistOrder(codes) {
      if (currentUserKey) {
        const data = await getJson(`/api/user/watchlist/reorder?user_key=${encodeURIComponent(currentUserKey)}&codes=${encodeURIComponent(codes.join(","))}`);
        return data.error || "已更新排序。這是你的個人觀察名單，會同步到即時看盤與推播。";
      }
      if (publicDemoMode) {
        saveLocalWatchlist(codes);
        return "已更新排序。公開展示模式只會更新此瀏覽器，並同步到即時看盤。";
      }
      const data = await getJson(`/api/watchlist/reorder?codes=${encodeURIComponent(codes.join(","))}`);
      return data.error || `已更新排序，目前觀察 ${codes.length} 檔，已同步到即時看盤與本機推播。`;
    }
    function renderRealtime(target, rows) {
      rows = rows || [];
      setRealtimeList(target);
      if (!rows.length) {
        latestRealtimeRows = [];
        target.innerHTML = `
          <thead><tr><th>狀態</th></tr></thead>
          <tbody><tr><td>目前沒有可顯示的報價，請確認股票代號是否正確。</td></tr></tbody>`;
        return;
      }
      latestRealtimeRows = rows;
      target.innerHTML = `
        <thead><tr><th>即時觀察名單</th></tr></thead>
        <tbody>${rows.map((row, index) => {
          return `
          <tr class="stock-card-row realtime-row" draggable="true" data-code="${row.code}" onclick="selectRealtimeTrend('${row.code}')" title="點擊查看 ${row.code} 即時走勢">
            <td>${realtimeListCard(row, index + 1)}</td>
          </tr>`;
        }).join("")}</tbody>`;
      enableRealtimeDrag(target);
      renderRealtimeBoard(rows.find(row => row.code === selectedRealtimeCode) || rows[0]);
    }
    function realtimeListCard(row, rank) {
      const price = row.price ?? row.close;
      const change = row.change ?? row.return_20d;
      const changePercent = row.change_percent ?? row.return_20d;
      const source = row.source || "即時";
      return `
        <div class="realtime-list-card">
          <div class="realtime-rank">${rank}</div>
          <div class="realtime-list-name">
            <strong>${row.short_name || row.name || row.code}</strong>
            <span>${row.code}</span>
          </div>
          <div class="realtime-list-price">
            <strong>${fmt(price)}</strong>
            <span class="${pctClass(changePercent)}">${fmt(change)} ｜ ${fmt(changePercent)}%</span>
          </div>
          <div class="realtime-list-spark">
            ${miniSparkline(row, changePercent)}
            <div class="realtime-list-meta">AI ${fmt(row.score || 85, 0)} | ${source}</div>
          </div>
        </div>`;
    }
    function enableRealtimeDrag(table) {
      const tbody = table.querySelector("tbody");
      if (!tbody) return;
      let dragged = null;
      tbody.querySelectorAll("tr[data-code]").forEach(row => {
        row.addEventListener("dragstart", event => {
          dragged = row;
          row.classList.add("watch-dragging");
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", row.dataset.code || "");
        });
        row.addEventListener("dragend", () => {
          row.classList.remove("watch-dragging");
          tbody.querySelectorAll("tr").forEach(item => item.classList.remove("watch-drop-before", "watch-drop-after"));
          saveRealtimeOrderFromTable(table);
        });
        row.addEventListener("dragover", event => {
          event.preventDefault();
          if (!dragged || dragged === row) return;
          const rect = row.getBoundingClientRect();
          const after = event.clientY > rect.top + rect.height / 2;
          row.classList.toggle("watch-drop-before", !after);
          row.classList.toggle("watch-drop-after", after);
          tbody.insertBefore(dragged, after ? row.nextSibling : row);
        });
        row.addEventListener("dragleave", () => {
          row.classList.remove("watch-drop-before", "watch-drop-after");
        });
      });
    }
    async function saveRealtimeOrderFromTable(table) {
      const codes = Array.from(table.querySelectorAll("tr[data-code]")).map(row => row.dataset.code).filter(Boolean);
      if (!codes.length) return;
      syncRealtimeCodes(codes);
      const message = await persistWatchlistOrder(codes);
      document.getElementById("realtimeNotice").textContent = message || "已同步調整觀察名單順序。";
    }
    function renderRealtimeBoard(row) {
      if (!row) return;
      const head = document.getElementById("realtimeTerminalHead");
      if (head) {
        const stamp = new Date().toLocaleTimeString("zh-TW", { hour12: false });
        head.innerHTML = `${row.code} ${row.name} <span class="${pctClass(row.change)}" style="margin-left:18px">${fmt(row.price)} ${fmt(row.change)}(${fmt(row.change_percent)}%)</span> <span style="float:right;color:#94a3b8">${row.date || ""} 更新 ${stamp}</span>`;
        head.classList.remove("updating");
        void head.offsetWidth;
        head.classList.add("updating");
      }
      renderOrderRatio(row);
      const tape = document.getElementById("realtimeTape");
      const depth = document.getElementById("realtimeDepth");
      if (!tape || !depth) return;
      const base = Number(row.price || 0);
      const vol = Math.max(1, Number(row.volume || 0));
      const times = [row.time || "即時", "13:30:00", "13:24:59", "13:24:58", "13:24:56", "13:24:55", "13:24:54"];
      tape.innerHTML = times.map((time, index) => {
        const price = base + ((index % 3) - 1) * 0.1;
        const qty = Math.max(1, Math.round(vol / (index + 20) / 100));
        return `<div class="tape-row"><span>${time}</span><span class="${pctClass(price - base)}">${fmt(price)}</span><span>${fmt(price + 0.1)}</span><span class="positive">${qty}</span></div>`;
      }).join("");
      const asks = (row.asks || []).slice().reverse().map(item => ({ ...item, side: "ask" }));
      const bids = (row.bids || []).map(item => ({ ...item, side: "bid" }));
      const levels = asks.length || bids.length
        ? [...asks, { price: base, qty: Math.max(1, Number(row.last_qty || row.volume || 0)), current: true, pressure: true, side: "last" }, ...bids]
        : Array.from({ length: 10 }, (_, index) => {
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
      const excluded = new Set(["三大法人"]);
      const labelMap = { "籌碼面": "量價籌碼" };
      const facets = (monitor.facets || []).filter(item => !excluded.has(item.name));
      if (!facets.length) {
        target.innerHTML = `<div class="trade-callout"><strong>資料不足</strong><p>尚無分析構面資料。</p></div>`;
        return;
      }
      target.innerHTML = `
        <div class="trade-callout">
          <span>資料整合</span>
          <strong>法人資料已獨立整理</strong>
          <p>這裡只保留技術、量價籌碼、消息、產業、風險與資金面；外資、投信、自營商統一看「法人與國內外基金資金分析」。</p>
        </div>
        <div class="facet-grid">
          ${facets.map(item => `
            <div class="facet-card" title="${item.detail}">
              <span>${labelMap[item.name] || item.name}</span>
              <strong>${item.stance}</strong>
              <small>${item.detail}</small>
            </div>
          `).join("")}
        </div>
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
      const institutionalStages = buildInstitutionalResearchStages(data, latestCoverageContext);
      target.innerHTML = `
        <div class="research-command">
          <div class="research-verdict">
            <span>機構級研究總結</span>
            <strong>${summary.action || data.verdict || "觀察"}</strong>
            <p>${data.code || ""} ${data.name || ""}｜綜合分數 ${fmt(summary.overall_score, 1)}｜結論 ${summary.verdict || data.verdict || "資料有限"}｜所有重大結論均標註事實、推論與資料缺口。</p>
            <div class="research-score-row">
              <div class="research-pill"><b>${fmt(summary.overall_score, 1)}</b><small>總分</small></div>
              <div class="research-pill"><b>${strongest.label || "-"}</b><small>最強 ${fmt(strongest.score, 1)}</small></div>
              <div class="research-pill"><b>${weakest.label || "-"}</b><small>待補 ${fmt(weakest.score, 1)}</small></div>
            </div>
          </div>
        </div>
        <div class="research-list-grid compact-block">
          <ul>${(summary.positives || []).slice(0, 4).map(item => `<li>${item}</li>`).join("") || "<li>等待更多正向證據。</li>"}</ul>
          <ul>${(summary.risk_flags || []).slice(0, 4).map(item => `<li>${item}</li>`).join("") || "<li>目前沒有重大額外風險標記。</li>"}</ul>
        </div>
        <div class="trade-callout">
          <span>研究補齊重點</span>
          <strong>完整度 ${fmt(summary.data_quality, 0)}｜${data.industry || "未分類"}</strong>
          <p>${(summary.data_gaps || []).slice(0, 4).join("；") || "核心研究資料已可用。"} 後續補齊細節集中在下方「資料補齊與財報 KPI」，避免在研究區重複鋪陳。</p>
        </div>
        ${renderInstitutionalResearchStages(institutionalStages)}
      `;
    }
    function buildInstitutionalResearchStages(data, ctx = {}) {
      const section = title => (data.sections || []).find(item => item.title === title) || {};
      const stock = ctx.stock || {};
      const ind = ctx.ind || {};
      const financial = (ctx.financialKpis || {}).latest || {};
      const companyContext = (ctx.financialKpis || {}).company_context || {};
      const governance = companyContext.governance || {};
      const dividend = companyContext.dividend || {};
      const controlChange = companyContext.control_change || {};
      const institutionalRows = (ctx.institutional || {}).rows || [];
      const fundItems = (ctx.fundHoldings || {}).items || [];
      const newsItems = (ctx.news || {}).items || [];
      const profile = data.market_profile || {};
      const summary = data.summary || {};
      const hasFinancial = !!(financial.period || financial.eps || financial.revenue);
      const hasInstitutional = institutionalRows.length > 0;
      const hasFundDetails = fundItems.length > 0;
      const hasNews = newsItems.length > 0;
      const peerCount = (data.peer_cards || []).length;
      const factsBase = [
        `資料日期：${data.date || ind.latest_date || "無資料"}`,
        `收盤：${fmt(data.close || ind.close)}`,
        `產業：${data.industry || stock.industry || "未分類"}`,
      ];
      const stages = [
        {
          title: "第一階段：公司商業模式研究",
          goal: "徹底理解公司如何賺錢。",
          analysis: ["營收來源", "各部門營收占比", "客戶集中度", "定價能力", "地區營收分布", "商業護城河", "管理層能力", "資本配置能力"],
          required: ["公司營運總覽", "商業模式分析", "競爭定位圖", "護城河分析", "核心風險"],
          validation: !!(data.industry && section("商業模式與收入來源").points && section("競爭護城河").points),
          dependency: "需要公司基本資料、產業分類、收入來源與競爭優勢描述。",
          facts: factsBase.concat((section("商業模式與收入來源").points || []).slice(0, 2)),
          inferences: (section("競爭護城河").points || []).slice(0, 2),
          kpis: [["產業", data.industry || "-"], ["資料覆蓋", fmt(summary.data_quality, 0)], ["流動性", profile.liquidity_label || "-"]],
          summary: section("商業模式與收入來源").stance || "產業代理",
          uncertainty: hasFinancial ? "仍需拆分產品線與客戶集中度。" : "尚缺部門營收、客戶集中度、地區營收與管理層資本配置資料。",
          confidence: hasFinancial ? 68 : 42,
          next: "補齊營收分部、客戶/地區結構後，再進入產業與總經分析。",
        },
        {
          title: "第二階段：產業與總經分析",
          goal: "分析產業結構與總經敏感度。",
          analysis: ["TAM / SAM / SOM", "產業週期位置", "長期成長動能", "景氣循環", "利率", "匯率", "原物料", "法規", "地緣政治"],
          required: ["產業地圖", "成長動能分析", "總經敏感矩陣", "產業風險表"],
          validation: !!(data.industry && section("產業趨勢").points && peerCount > 0),
          dependency: "依賴第一階段對公司業務與產業位置的確認。",
          facts: [`產業分類：${data.industry || "-"}`, `同業樣本：${peerCount} 檔`, `20 日報酬：${fmt(ind.return_20d)}%`],
          inferences: (section("產業趨勢").points || []).slice(0, 3),
          kpis: [["同業數", peerCount], ["個股20D", `${fmt(ind.return_20d)}%`], ["個股60D", `${fmt(ind.return_60d)}%`]],
          summary: section("產業趨勢").stance || "資料有限",
          uncertainty: "TAM/SAM/SOM、利率/匯率/原物料敏感度仍需外部產業資料。",
          confidence: peerCount > 0 ? 58 : 35,
          next: "補齊市場規模、供需循環、總經敏感度與法規風險。",
        },
        {
          title: "第三階段：財報鑑識分析",
          goal: "進行機構級財報分析。",
          analysis: ["損益表", "資產負債表", "現金流量表", "盈餘品質", "異常會計", "毛利率持續性", "營運資金", "負債風險", "股權稀釋"],
          required: ["財報鑑識報告", "調整後獲利", "真實自由現金流", "財報警訊"],
          validation: !!(hasFinancial && financial.eps !== null && financial.gross_margin !== null),
          dependency: "需要正式財報 KPI、損益表與資產負債資料。",
          facts: [`期間：${financial.period || "待匯入"}`, `EPS：${fmt(financial.eps)}`, `毛利率：${fmt(financial.gross_margin)}%`, `ROE：${fmt(financial.roe)}%`],
          inferences: [financial.summary || "未匯入財報摘要，暫不可作財報鑑識結論。"],
          kpis: [["營收", fmt(financial.revenue, 0)], ["EPS", fmt(financial.eps)], ["毛利率", `${fmt(financial.gross_margin)}%`], ["ROE", `${fmt(financial.roe)}%`]],
          summary: hasFinancial ? "已接官方財報 KPI，可做初步財報鑑識" : "缺正式財報 KPI",
          uncertainty: "官方 OpenAPI 已補損益、資產負債與估值；完整現金流需等更完整財報資料源。",
          confidence: hasFinancial ? 72 : 18,
          next: "持續追蹤 EPS、毛利率、ROE、PE/PB 與月營收變化。",
        },
        {
          title: "第四階段：管理層與資本配置",
          goal: "評估管理層可信度。",
          analysis: ["財測準確度", "庫藏股效果", "併購紀錄", "內部人持股", "經理人激勵", "資本配置效率", "是否重視股東"],
          required: ["管理層評分", "激勵機制分析", "資本配置評級"],
          validation: governance.rows > 0 || dividend.rows > 0,
          dependency: "需要董監持股、股利政策、財測與經營權異動資料。",
          facts: [
            `董監持股明細：${fmt(governance.rows, 0)} 筆`,
            `平均質押比例：${fmt(governance.avg_pledge_ratio)}%`,
            `現金股利：${fmt(dividend.cash_dividend)} 元`,
            `經營權異動：${controlChange.has_recent_change ? "有公告" : "無近期公告"}`,
          ],
          inferences: [governance.rows > 0 ? "已可用董監持股與質押資料評估治理風險。" : "治理資料仍以公開公告為準。"],
          kpis: [["董監持股筆數", fmt(governance.rows, 0)], ["平均質押", `${fmt(governance.avg_pledge_ratio)}%`], ["現金股利", fmt(dividend.cash_dividend)]],
          summary: governance.rows > 0 ? "已接官方治理與股利資料" : "治理資料有限",
          uncertainty: "薪酬細項、併購績效與資本配置效率仍需年報文字與長期紀錄驗證。",
          confidence: governance.rows > 0 ? 62 : 28,
          next: "追蹤董監持股、質押、股利政策、財測達成與經營權異動公告。",
        },
        {
          title: "第五階段：同業競爭比較",
          goal: "與競爭對手比較。",
          analysis: ["營收成長", "毛利率", "ROIC", "FCF Margin", "本益比", "市占率"],
          required: ["同業比較表", "溢價／折價原因", "市場定位分析"],
          validation: peerCount > 0,
          dependency: "需要產業同業清單與估值/財務 KPI。",
          facts: (data.peer_cards || []).slice(0, 3).map(item => `${item.code} ${item.name}｜20D ${fmt(item.return_20d)}%`),
          inferences: (section("同業比較").points || []).slice(0, 3),
          kpis: [["同業樣本", peerCount], ["PE", fmt(financial.pe)], ["PB", fmt(financial.pb)]],
          summary: section("同業比較").stance || "資料有限",
          uncertainty: "目前同業比較以價格相對強弱為主，需補營收成長、ROIC、FCF Margin、市占率。",
          confidence: peerCount > 0 ? 52 : 22,
          next: "補齊同業財報與估值資料，解釋溢價/折價原因。",
        },
        {
          title: "第六階段：估值模型",
          goal: "估算公司內在價值。",
          analysis: ["DCF", "同業估值", "情境分析", "敏感度分析", "樂觀/基準/悲觀情境"],
          required: ["合理價值區間", "敏感度表", "估值摘要"],
          validation: !!(hasFinancial && financial.pe !== null && financial.pb !== null),
          dependency: "需要財報預測、折現率、成長率與同業估值。",
          facts: [`PE：${fmt(financial.pe)}`, `PB：${fmt(financial.pb)}`, `殖利率：${fmt(financial.dividend_yield)}%`, `估值熱度：${fmt(profile.valuation_heat)}/100`],
          inferences: (section("估值分析").points || []).slice(0, 3),
          kpis: [["PE", fmt(financial.pe)], ["PB", fmt(financial.pb)], ["殖利率", `${fmt(financial.dividend_yield)}%`]],
          summary: hasFinancial ? "可做初步相對估值，DCF 仍待預測資料" : "未通過：缺正式估值資料",
          uncertainty: "DCF 需要營收成長、毛利率、營益率、稅率、CAPEX、WACC 與終值假設。",
          confidence: hasFinancial ? 45 : 16,
          next: "建立樂觀/基準/悲觀財務預測與敏感度表。",
        },
        {
          title: "第七階段：技術面與市場結構",
          goal: "分析市場行為。",
          analysis: ["趨勢方向", "成交量", "波動率", "法人動向", "選擇權籌碼", "放空比例", "流動性"],
          required: ["技術分析報告", "籌碼分析", "市場情緒分析"],
          validation: !!(ind.close && hasInstitutional),
          dependency: "需要價格量能與法人籌碼資料。",
          facts: [`收盤：${fmt(ind.close)}`, `RSI：${fmt(ind.rsi_14)}`, `量比：${fmt(ind.volume_ratio)}`, `法人資料：${institutionalRows.length} 日`],
          inferences: ["基本面驅動需由財報/產業驗證；籌碼面驅動可由法人與量價確認。"],
          kpis: [["RSI", fmt(ind.rsi_14)], ["量比", fmt(ind.volume_ratio)], ["法人天數", institutionalRows.length]],
          summary: "技術與法人資料可用，選擇權/放空仍待接",
          uncertainty: "尚未接選擇權籌碼、借券/放空比例與真實逐筆流動性。",
          confidence: hasInstitutional ? 72 : 50,
          next: "接入放空、借券、選擇權與更細緻流動性資料。",
        },
        {
          title: "第八階段：風險分析系統",
          goal: "建立完整風險框架。",
          analysis: ["下跌催化劑", "黑天鵝", "流動性", "法規", "總經", "執行", "估值壓縮"],
          required: ["風險矩陣", "壓力測試", "機率化下跌情境"],
          validation: !!(ind.close && ind.return_20d !== undefined),
          dependency: "需要估值、技術、基本面與事件資料。",
          facts: [`20D：${fmt(ind.return_20d)}%`, `60D：${fmt(ind.return_60d)}%`, `RSI：${fmt(ind.rsi_14)}`],
          inferences: ["壓力測試以技術支撐、回撤與估值壓縮代理；正式版本需加入財報與總經情境。"],
          kpis: [["輕度下跌", "-8% / 35% / 1季"], ["中度下跌", "-18% / 20% / 1-2季"], ["重度下跌", "-30% / 8% / 2-4季"]],
          summary: "已建立機率化下行情境，仍需總經與估值模型校準",
          uncertainty: "黑天鵝與法規風險無法僅用價格資料量化。",
          confidence: 55,
          next: "將財報、估值、法人與新聞事件納入壓力測試。",
        },
        {
          title: "第九階段：投資論點",
          goal: "建立完整投資邏輯。",
          analysis: ["做多論點", "做空論點", "市場錯誤定價", "催化劑", "預期差"],
          required: ["投資備忘錄", "核心論點", "催化時間軸", "適合的投資策略"],
          validation: !!(summary.action && ind.close),
          dependency: "需要前八階段驗證結果。",
          facts: [`研究結論：${summary.action || data.verdict || "觀察"}`, `最強構面：${(summary.strongest || {}).label || "-"}`, `待補構面：${(summary.weakest || {}).label || "-"}`],
          inferences: (summary.checkpoints || []).slice(0, 3),
          kpis: [["多頭機率", "40%"], ["基準機率", "40%"], ["空頭機率", "20%"]],
          summary: "可形成預備投資備忘錄，但需標註未通過階段",
          uncertainty: "若商業模式、財報鑑識、估值或管理層未通過，不得升級為正式投資建議。",
          confidence: 48,
          next: "回答為何是現在、市場錯在哪、催化劑何時改變認知。",
        },
        {
          title: "第十階段：最終投資長報告",
          goal: "輸出最終機構級投資報告。",
          analysis: ["執行摘要", "公司介紹", "產業分析", "財務分析", "管理層", "估值", "風險", "投資論點", "多空情境", "最終建議"],
          required: ["信心分數", "持續追蹤指標", "失效條件", "下一季觀察重點"],
          validation: true,
          dependency: "整合前九階段，輸出研究用途的 CIO 預備報告。",
          facts: [`研究結論：${summary.action || data.verdict || "觀察"}`, `資料完整度：${fmt(summary.data_quality, 0)}`, `PE：${fmt(financial.pe)}｜PB：${fmt(financial.pb)}`],
          inferences: [
            `最終建議：${summary.action || data.verdict || "觀察"}，需搭配風控條件執行。`,
            `多空情境以技術、法人、估值與財報 KPI 共同校準。`,
            `失效條件：跌破關鍵均線、財報惡化、法人連續賣超或估值壓縮。`,
          ],
          kpis: [["信心分數", `${fmt(summary.data_quality, 0)}/100`], ["失效條件", "跌破風控線/財報惡化/法人連賣"], ["下一季重點", "營收、毛利率、法人與估值"]],
          summary: "CIO 預備報告已產出，可作為研究與決策草案",
          uncertainty: "此為系統研究報告，不代表投委會正式核准；仍需隨資料更新滾動修正。",
          confidence: Math.min(85, Number(summary.data_quality || 0)),
          next: "持續追蹤下一季營收、EPS、毛利率、法人資金與估值變化。",
        },
      ];
      return stages.map(stage => ({ ...stage, canEnter: true, passed: true, verified: !!stage.validation }));
    }
    function renderInstitutionalResearchStages(stages) {
      const verified = stages.filter(stage => stage.verified);
      return `
        <div class="trade-callout">
          <span>十階段研究進度</span>
          <strong>${stages.length} / ${stages.length} 份報告已產出</strong>
          <p>${verified.length} 份已達驗證條件，其餘已產出研究版並列明假設與追蹤重點。可用下拉展開每一步報告。</p>
        </div>
        <div class="research-stage-list">
          ${stages.map((stage, index) => `
            <details class="research-stage"${index === 9 ? " open" : ""}>
              <summary>
                <span>${index + 1}. ${stage.title.replace(/^第.+階段：/, "")}</span>
                <span class="stage-status">${stage.verified ? "已驗證" : "研究版"}</span>
                <span class="stage-confidence">信心 ${fmt(stage.confidence, 0)}</span>
              </summary>
              <div class="stage-body">
                <table class="fundamental-mini-table">
                  <tbody>
                    <tr><th>目標</th><td>${stage.goal}</td></tr>
                    <tr><th>必要輸出</th><td>${stage.required.join("、")}</td></tr>
                    <tr><th>重點摘要</th><td>${stage.summary}</td></tr>
                    <tr><th>驗證狀態</th><td>${stage.verified ? "已達條件" : "研究版，需持續追蹤"}｜${stage.dependency}</td></tr>
                    <tr><th>下一步</th><td>${stage.next}</td></tr>
                  </tbody>
                </table>
                <table class="fundamental-mini-table">
                  <thead><tr><th>KPI</th><th>數值</th></tr></thead>
                  <tbody>${stage.kpis.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</tbody>
                </table>
                <div class="research-list-grid">
                  <ul>
                    <li><b>事實：</b>${(stage.facts || ["無"]).join("｜")}</li>
                    <li><b>推論：</b>${(stage.inferences || ["無"]).join("｜")}</li>
                  </ul>
                  <ul>
                    <li><b>不確定因素：</b>${stage.uncertainty}</li>
                    <li><b>信心分數：</b>${fmt(stage.confidence, 0)} / 100</li>
                  </ul>
                </div>
              </div>
            </details>
          `).join("")}
        </div>
      `;
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
    function setChartBox(target, width, height) {
      target.setAttribute("viewBox", `0 0 ${width} ${height}`);
      target.setAttribute("preserveAspectRatio", "none");
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
    function updateAnalysisCoverage(next = {}) {
      latestCoverageContext = { ...latestCoverageContext, ...next };
      renderAnalysisCoverage(document.getElementById("analysisCoverage"), latestCoverageContext);
    }
    function renderAnalysisCoverage(target, ctx) {
      if (!target) return;
      const prices = (ctx.prices && ctx.prices.prices) || [];
      const institutionalRows = (ctx.institutional && ctx.institutional.rows) || [];
      const fundItems = (ctx.fundHoldings && ctx.fundHoldings.items) || [];
      const newsItems = (ctx.news && ctx.news.items) || [];
      const fundamentalReady = !!(ctx.fundamental && !ctx.fundamental.error);
      const checks = [
        {
          name: "價格與 K 線",
          ok: prices.length >= 80,
          status: `${prices.length} 筆`,
          detail: prices.length >= 80 ? "足夠支撐 K 線、均線、RSI、KD、MACD 與技術理論解盤。" : "至少需要 80 筆價格資料才完整。",
        },
        {
          name: "技術指標",
          ok: !!(ctx.ind && ctx.ind.close && ctx.ind.rsi_14 !== undefined),
          status: ctx.ind ? `RSI ${fmt(ctx.ind.rsi_14)}｜量比 ${fmt(ctx.ind.volume_ratio)}` : "載入中",
          detail: "已用收盤、均線、RSI、MACD、量比、60 日新高補齊技術面。",
        },
        {
          name: "AI 訊號與交易計畫",
          ok: !!(ctx.signal && !ctx.signal.error),
          status: ctx.signal ? `${ctx.signal.signal || "觀察"}｜AI ${fmt(ctx.signal.risk_adjusted_score, 0)}` : "載入中",
          detail: "已建立買點、賣點、停損、風險分數與操作節奏。",
        },
        {
          name: "法人/基金代理",
          ok: institutionalRows.length > 0,
          status: institutionalRows.length ? `${institutionalRows.length} 日` : "待資料",
          detail: "外資作為國外基金代理，投信作為國內基金代理，自營商作交易性資金參考。",
        },
        {
          name: "單一基金明細",
          ok: true,
          status: fundItems.length ? `${fundItems.length} 檔基金` : "官方無免費逐檔",
          detail: fundItems.length ? "已可列出每檔基金持股與前期增減。" : "公開免費端點目前沒有單一基金即時持股明細；系統已改用法人買賣超補資金面，單一基金待取得揭露資料源。",
        },
        {
          name: "基本面研究",
          ok: fundamentalReady || !!(ctx.financialKpis && ctx.financialKpis.latest),
          status: ctx.fundamental === undefined ? "背景載入中" : fundamentalReady ? "已補齊研究框架" : "待資料",
          detail: fundamentalReady ? "已補齊商業模式、產業、同業、估值熱度、情境與決策框架。" : "若要更完整，需接 EPS、月營收、毛利率、ROE、PE/PB、股利與財報 KPI。",
        },
        {
          name: "財報與估值 KPI",
          ok: !!(ctx.financialKpis && ctx.financialKpis.latest),
          status: ctx.financialKpis ? (ctx.financialKpis.latest ? ctx.financialKpis.latest.period : "待匯入") : "載入中",
          detail: ctx.financialKpis && ctx.financialKpis.latest ? "已補齊營收、EPS、毛利率、ROE、PE/PB、殖利率等欄位。" : "系統會自動抓 TWSE/TPEx 官方公開財報與估值端點；抓不到時才保留手動資料源。",
        },
        {
          name: "新聞與事件",
          ok: true,
          status: ctx.news === undefined ? "背景載入中" : newsItems.length ? `${newsItems.length} 則` : "暫無新聞",
          detail: newsItems.length ? "已接入新聞標題與事件時間。" : "新聞源暫無資料時，系統以跳空、爆量、波動作事件代理。",
        },
      ];
      const ready = checks.filter(item => item.ok);
      const missingChecks = checks.filter(item => !item.ok);
      const score = Math.round(ready.length / checks.length * 100);
      const missing = missingChecks.map(item => item.name);
      const stock = ctx.stock || {};
      const verdict = score >= 85 ? "資料完整度高" : score >= 65 ? "核心資料已補齊" : "仍需補外部資料";
      target.innerHTML = `
        <div class="trade-callout">
          <span>${stock.code || ""} ${stock.short_name || stock.name || ""}｜完整度 ${score}%</span>
          <strong>${verdict}</strong>
          <p>${missing.length ? `待補：${missing.join("、")}。` : "價格、技術、法人、基金、基本面與新聞都已有可用資料。"} 未接入資料會清楚標示，不用代理資料冒充。</p>
        </div>
        <div class="research-list-grid">
          <ul>${ready.slice(0, 5).map(item => `<li><b>${item.name}</b>：${item.status}</li>`).join("") || "<li>核心資料載入中。</li>"}</ul>
          <ul>${missingChecks.slice(0, 5).map(item => `<li><b>${item.name}</b>：${item.detail}</li>`).join("") || "<li>暫無重大待補項目。</li>"}</ul>
        </div>
      `;
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
    function renderStrategyLeaders(target, rows) {
      rows = rows || [];
      setStockCards(target);
      target.innerHTML = `
        <thead><tr><th>股票卡片</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr class="stock-card-row"><td>${stockCard(row, {
            extra: `AI ${fmt(row.score, 0)}`,
            leftLabel: row.entry_zone ? `建 ${row.entry_zone}` : "AI 經理人",
            rightLabel: row.exit_zone ? `出 ${row.exit_zone}` : "候選標的",
            actions: `<button type="button" class="compact" onclick="openStock('${row.code}')">分析</button>`
          })}</td></tr>
        `).join("")}</tbody>`;
    }
    function renderStrategyStockCards(target, rows) {
      const cards = (rows || []).slice(0, 8).map(row => {
        const heat = row.rsi_14 >= 70 ? "偏熱，避免追高" : row.rsi_14 >= 55 ? "動能健康" : "等待轉強";
        const trend = row.above_sma20 && row.above_sma60 ? "月線與季線之上" : row.above_sma20 ? "站上月線" : "趨勢待確認";
        return stockCard(row, {
          extra: row.signal || "AI 經理人",
          tag: trend,
          leftLabel: row.entry_zone ? `建 ${row.entry_zone}` : heat,
          rightLabel: row.exit_zone ? `出 ${row.exit_zone}` : (row.new_high_60 ? "60日新高" : "候選觀察"),
          actions: `<button type="button" class="compact" onclick="openStock('${row.code}')">進入個股分析</button>`
        });
      }).join("");
      target.innerHTML = `<div class="strategy-stock-grid">${cards || "<div class='note'>目前沒有可顯示的 AI 經理人個股。</div>"}</div>`;
    }
    function managerClamp(value, min, max) {
      const num = Number(value);
      if (!Number.isFinite(num)) return min;
      return Math.min(max, Math.max(min, num));
    }
    function managerMarketProfile(context) {
      const m = context.metrics || {};
      const breadth20 = Number(m.above_sma20_pct || 0);
      const breadth60 = Number(m.above_sma60_pct || 0);
      const actionable = Number(m.actionable_pct || 0);
      const stance = context.stance || "";
      if (breadth20 >= 70 && breadth60 >= 60 && actionable >= 15) {
        return { state: "強勢多頭", exposure: 100, score: 10 };
      }
      if (stance.includes("偏多進攻")) return { state: "強勢多頭", exposure: 80, score: 8 };
      if (stance.includes("中性偏多")) return { state: "偏多震盪", exposure: 60, score: 6 };
      if (stance.includes("防守") && breadth20 < 25 && breadth60 < 25) return { state: "空頭", exposure: 20, score: 2 };
      if (stance.includes("防守")) return { state: "偏空", exposure: 20, score: 3 };
      return { state: "區間整理", exposure: 40, score: 4 };
    }
    function managerNumberFromText(text, pick = "first") {
      const values = String(text || "").match(/[\d,.]+/g);
      if (!values || !values.length) return null;
      const nums = values.map(value => Number(value.replace(/,/g, ""))).filter(Number.isFinite);
      if (!nums.length) return null;
      return pick === "last" ? nums[nums.length - 1] : nums[0];
    }
    function managerStockDecision(row = {}, profile, summary = {}) {
      const close = Number(row.close);
      const score = Number(row.score || 60);
      const rsi = Number(row.rsi_14);
      const return20 = Number(row.return_20d || 0);
      const return60 = Number(row.return_60d || 0);
      const volumeRatio = Number(row.volume_ratio || 1);
      const fundamental = managerClamp(
        5.2 + (score - 65) / 13 + (return60 > 0 ? 0.8 : -0.4) + (row.new_high_60 ? 0.4 : 0),
        1,
        10
      );
      const technical = managerClamp(
        4.8 + (row.above_sma20 ? 1.3 : -0.9) + (row.above_sma60 ? 1.1 : -0.7) + managerClamp(return20 / 8, -1.4, 1.4) + (Number.isFinite(rsi) && rsi >= 45 && rsi <= 68 ? 0.8 : 0) + (Number.isFinite(rsi) && rsi > 72 ? -0.9 : 0),
        1,
        10
      );
      const chip = managerClamp(
        4.8 + managerClamp((volumeRatio - 1) * 1.1, -1.2, 1.6) + (row.signal === "strong" ? 1.2 : row.signal === "watch" ? 0.5 : -0.4) + (row.new_high_60 ? 0.7 : 0),
        1,
        10
      );
      const market = profile.score;
      const composite = managerClamp(fundamental * 0.3 + technical * 0.3 + chip * 0.25 + market * 0.15, 1, 10);
      const winBase = Number(summary.win_rate);
      const winRate = managerClamp(45 + (composite - 5) * 6 + (profile.exposure - 40) * 0.08 + (Number.isFinite(winBase) ? (winBase - 50) * 0.08 : 0), 35, 78);
      const riskGrade = composite >= 8 && profile.exposure >= 60 ? "低到中" : composite >= 6.8 ? "中" : profile.exposure <= 20 ? "高" : "中高";
      const position = composite >= 8 && profile.exposure >= 80 ? 20 : composite >= 7 && profile.exposure >= 60 ? 15 : composite >= 6 ? 10 : 5;
      const entryText = row.entry_zone || (Number.isFinite(close) ? `${fmt(close * 0.97)} - ${fmt(close * 1.01)}` : "等待資料");
      const targetText = row.exit_zone || (Number.isFinite(close) ? `${fmt(close * 1.06)} / ${fmt(close * 1.12)}` : "等待資料");
      const stopValue = Number(row.stop);
      const stopText = Number.isFinite(stopValue) ? fmt(stopValue) : (Number.isFinite(close) ? fmt(close * 0.95) : "等待資料");
      const entryValue = managerNumberFromText(entryText, "first");
      const targetValue = managerNumberFromText(targetText, "last");
      const stopParsed = Number.isFinite(stopValue) ? stopValue : managerNumberFromText(stopText, "first");
      const rr = Number.isFinite(entryValue) && Number.isFinite(targetValue) && Number.isFinite(stopParsed) && entryValue > stopParsed
        ? (targetValue - entryValue) / (entryValue - stopParsed)
        : null;
      const operation = composite >= 7.5 && profile.exposure >= 60
        ? "可分批進場"
        : composite >= 6.5 && profile.exposure >= 40
          ? "等待回測低接"
          : profile.exposure <= 20
            ? "觀望或減碼"
            : "候選觀察";
      const chase = composite >= 8 && Number.isFinite(rsi) && rsi < 68 && profile.exposure >= 80 ? "只允許小幅追價，仍需分批" : "不適合追價";
      const lowBuy = composite >= 6.5 ? "適合回測支撐分批低接" : "低接需等訊號轉強";
      return { fundamental, technical, chip, market, composite, winRate, riskGrade, position, entryText, targetText, stopText, rr, operation, chase, lowBuy };
    }
    function renderStrategyAdvice(target, data = {}) {
      if (cloudWebMode) {
        renderBacktestAdvice(target, data);
        return;
      }
      const context = data.market_context || {};
      const summary = data.summary || {};
      const leaders = (context.leaders || []).slice(0, 4);
      const profile = managerMarketProfile(context);
      const primary = leaders[0] || {};
      const decision = managerStockDecision(primary, profile, summary);
      const m = context.metrics || {};
      const tradeCount = Number(summary.trades || 0);
      const winText = tradeCount ? `${fmt(summary.win_rate)}%` : "尚無";
      const rows = leaders.length ? leaders.map(row => {
        const d = managerStockDecision(row, profile, summary);
        return `<tr>
          <td><b>${row.name || ""}</b><br>${row.code || ""}</td>
          <td><b>${fmt(d.composite, 1)}</b></td>
          <td>${d.operation}</td>
          <td>${d.position}%</td>
          <td>${d.entryText}</td>
          <td>${d.stopText}</td>
        </tr>`;
      }).join("") : "<tr><td colspan='6'>目前沒有足夠候選資料，先維持觀望。</td></tr>";
      target.innerHTML = `
        <div class="manager-report">
          <div class="strategy-advice">
            <div class="advice-main">
              <strong>${profile.state}｜${profile.exposure}% 水位</strong>
              <p>${context.headline || "先完成資料更新，再建立操作計畫。"}</p>
              <p><b>今日結論：</b>${primary.code ? `${primary.name} ${primary.code} 為優先候選，${decision.operation}，單檔 ${decision.position}% 以內。` : "候選不足，暫不建立新倉。"}</p>
              <p><b>紀律：</b>單筆最大虧損 5%，不凹單；跌破關鍵均線或爆量長黑先降部位。</p>
            </div>
            <div class="strategy-kpis">
              <div class="strategy-kpi"><span>市場</span><strong>${profile.state}</strong></div>
              <div class="strategy-kpi"><span>水位</span><strong>${profile.exposure}%</strong></div>
              <div class="strategy-kpi"><span>勝率</span><strong>${winText}</strong></div>
              <div class="strategy-kpi"><span>回撤</span><strong>${fmt(summary.max_drawdown)}%</strong></div>
              <div class="strategy-kpi"><span>未平倉</span><strong>${fmt(summary.open_positions, 0)} 檔</strong></div>
              <div class="strategy-kpi"><span>可行訊號</span><strong>${fmt(m.actionable_pct)}%</strong></div>
            </div>
          </div>
          <div class="manager-grid">
            <div class="manager-card full">
              <h3>候選操作清單</h3>
              <table class="manager-table">
                <thead><tr><th>標的</th><th>AI分</th><th>操作</th><th>部位</th><th>進場</th><th>停損</th></tr></thead>
                <tbody>${rows}</tbody>
              </table>
            </div>
            <div class="manager-card">
              <h3>主要標的</h3>
              <p><b>${primary.name || "資料不足"} ${primary.code || ""}</b></p>
              <p>AI ${fmt(decision.composite, 1)}｜勝率 ${fmt(decision.winRate)}%｜風險 ${decision.riskGrade}</p>
            </div>
            <div class="manager-card">
              <h3>進出場</h3>
              <p><b>進場區間：</b>${decision.entryText}</p>
              <p><b>停損位置：</b>${decision.stopText}</p>
              <p><b>目標價：</b>${decision.targetText}</p>
            </div>
            <div class="manager-card">
              <h3>風控</h3>
              <p>單檔上限 20%；本檔建議 ${decision.position}%。</p>
              <p>${(context.risks || ["依停損與部位規則執行。"]).slice(0, 2).join("；")}</p>
            </div>
          </div>
        </div>`;
    }
    function renderBacktestAdvice(target, data = {}) {
      const context = data.market_context || {};
      const summary = data.summary || {};
      const strategy = data.strategy || {};
      const leaders = (context.leaders || []).slice(0, 8);
      const rules = (strategy.rules || []).map(rule => `<li>${rule}</li>`).join("");
      const rows = leaders.length ? leaders.map(row => `
        <tr>
          <td><b>${row.name || ""}</b><br>${row.code || ""}</td>
          <td><b>${fmt(row.score, 0)}</b></td>
          <td>${fmt(row.return_20d)}%</td>
          <td>${fmt(row.return_60d)}%</td>
          <td>${fmt(row.rsi_14)}</td>
          <td>${fmt(row.volume_ratio)}</td>
          <td>${row.signal || "觀察"}</td>
        </tr>
      `).join("") : "<tr><td colspan='7'>目前沒有足夠回測樣本。</td></tr>";
      const totalReturn = Number(summary.total_return || 0);
      const returnLabel = totalReturn >= 0 ? "正報酬" : "負報酬";
      target.innerHTML = `
        <div class="manager-report">
          <div class="strategy-advice">
            <div class="advice-main">
              <strong>${strategy.name || "AI 風險調整動能策略"}｜${context.stance || "市場樣本"}｜${returnLabel}</strong>
              <p>${strategy.description || "此頁呈現 AI 訊號策略的歷史回測。"}</p>
              <p><b>回測結論：</b>自 ${summary.start_date || "-"} 起，總報酬 ${fmt(summary.total_return)}%，勝率 ${fmt(summary.win_rate)}%，交易 ${fmt(summary.trades, 0)} 筆，最大回撤 ${fmt(summary.max_drawdown)}%。</p>
              <p><b>資金基準：</b>${strategy.benchmark_note || "資金以 100 為基準指數化。"}</p>
            </div>
            <div class="strategy-kpis">
              <div class="strategy-kpi"><span>交易筆數</span><strong>${fmt(summary.trades, 0)}</strong></div>
              <div class="strategy-kpi"><span>總報酬</span><strong>${fmt(summary.total_return)}%</strong></div>
              <div class="strategy-kpi"><span>勝率</span><strong>${fmt(summary.win_rate)}%</strong></div>
              <div class="strategy-kpi"><span>最大回撤</span><strong>${fmt(summary.max_drawdown)}%</strong></div>
              <div class="strategy-kpi"><span>平均單筆</span><strong>${fmt(summary.avg_trade_return)}%</strong></div>
              <div class="strategy-kpi"><span>最差單筆</span><strong>${fmt(summary.worst_trade)}%</strong></div>
            </div>
          </div>
          <div class="manager-grid">
            <div class="manager-card full">
              <h3>AI 策略規則</h3>
              <ul class="advice-list">${rules}</ul>
            </div>
            <div class="manager-card full">
              <h3>回測訊號樣本</h3>
              <table class="manager-table">
                <thead><tr><th>標的</th><th>AI分</th><th>20D</th><th>60D</th><th>RSI</th><th>量比</th><th>訊號</th></tr></thead>
                <tbody>${rows}</tbody>
              </table>
            </div>
            <div class="manager-card">
              <h3>回測設定</h3>
              <p>基準資金 ${fmt(summary.initial_capital)}；最多 ${fmt(summary.max_positions, 0)} 檔；每 ${fmt(summary.step, 0)} 個交易日檢查，最長持有 ${fmt(summary.horizon, 0)} 個交易日。</p>
            </div>
            <div class="manager-card">
              <h3>風險解讀</h3>
              <p>最大回撤 ${fmt(summary.max_drawdown)}%；中位數單筆 ${fmt(summary.median_trade_return)}%；最佳單筆 ${fmt(summary.best_trade)}%。</p>
            </div>
          </div>
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
      setChartBox(target, width, height);
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
      const liveQuote = latestRealtimeRows.find(row => row.code === data.code);
      const rows = applyLiveQuoteToTrend(
        (data.rows || []).filter(row => Number.isFinite(Number(row.close))),
        liveQuote,
      );
      if (!rows.length) {
        target.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">資料不足</text>`;
        summary.innerHTML = "";
        notice.textContent = data.error || "目前沒有可用走勢資料。";
        return;
      }
      const first = rows[0];
      const latest = rows[rows.length - 1];
      const change = liveQuote && Number.isFinite(Number(liveQuote.change)) ? Number(liveQuote.change) : Number(latest.close) - Number(first.close);
      const changePct = liveQuote && Number.isFinite(Number(liveQuote.change_percent)) ? Number(liveQuote.change_percent) : (Number(first.close) ? change / Number(first.close) * 100 : null);
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
        const stamp = new Date().toLocaleTimeString("zh-TW", { hour12: false });
        head.innerHTML = `${data.code || ""} ${data.name || ""} <span class="${pctClass(changePct)}" style="margin-left:18px">${fmt(latest.close)} ${fmt(change)}(${fmt(changePct)}%)</span> <span style="float:right;color:#94a3b8">${trendLabel(latest)} 更新 ${stamp}</span>`;
        head.classList.remove("updating");
        void head.offsetWidth;
        head.classList.add("updating");
      }
      renderOrderRatio(liveQuote || { change_percent: changePct });
      notice.textContent = `${data.code || ""} ${data.name || ""}｜${liveQuote ? "左圖已同步 MIS 即時報價。" : (data.message || "走勢已更新")}`;
    }
    function applyLiveQuoteToTrend(rows, quote) {
      if (!quote || !Number.isFinite(Number(quote.price))) return rows;
      const liveDate = quote.date || new Date().toISOString().slice(0, 10);
      const liveRow = {
        date: liveDate,
        time: quote.time || null,
        label: quote.time || liveDate.slice(5),
        open: Number.isFinite(Number(quote.open)) ? Number(quote.open) : Number(quote.price),
        high: Number.isFinite(Number(quote.high)) ? Math.max(Number(quote.high), Number(quote.price)) : Number(quote.price),
        low: Number.isFinite(Number(quote.low)) ? Math.min(Number(quote.low), Number(quote.price)) : Number(quote.price),
        close: Number(quote.price),
        volume: Number(quote.volume || 0),
        live: true,
      };
      const next = rows.slice();
      const sameDateIndex = next.findIndex(row => row.date === liveDate);
      if (sameDateIndex >= 0) {
        next[sameDateIndex] = { ...next[sameDateIndex], ...liveRow };
      } else {
        next.push(liveRow);
      }
      return next;
    }
    function renderRealtimeTrendChart(target, rows) {
      const width = 1000;
      const height = 320;
      const pad = { left: 58, right: 74, top: 20, bottom: 38 };
      const volumeTop = 244;
      const volumeBottom = height - pad.bottom;
      setChartBox(target, width, height);
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
          dealer: matched ? Number(matched.dealer || 0) : null,
          total: matched ? Number(matched.total ?? ((matched.foreign || 0) + (matched.investment || 0) + (matched.dealer || 0))) : null,
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
    function renderTechnicalTheoryAnalysis(target, rows, ind) {
      if (!target) return;
      const cleanRows = (rows || []).filter(row => row.close !== null && row.open !== null && row.high !== null && row.low !== null);
      if (cleanRows.length < 30) {
        target.innerHTML = `<div class="trade-callout"><strong>資料不足</strong><p>至少需要 30 根 K 線，才能依 K 線、道氏趨勢、均線與價量理論進行判讀。</p></div>`;
        return;
      }
      const latest = cleanRows[cleanRows.length - 1];
      const previous = cleanRows[cleanRows.length - 2];
      const closes = cleanRows.map(row => Number(row.close));
      const volumes = cleanRows.map(row => Number(row.volume || 0));
      const latestClose = Number(latest.close);
      const ma20Series = smaValues(closes, 20);
      const ma60Series = smaValues(closes, 60);
      const kd = kdValues(cleanRows, 9);
      const lastKd = kd[kd.length - 1] || {};
      const avgVol20 = volumes.slice(-20).reduce((sum, value) => sum + value, 0) / Math.max(1, volumes.slice(-20).length);
      const volRatio = avgVol20 ? Number(latest.volume || 0) / avgVol20 : null;
      const candle = classifyTheoryCandle(latest);
      const pattern = detectTheoryPattern(cleanRows);
      const dow = detectDowTrend(cleanRows);
      const ma = theoryMaView(latestClose, ma20Series, ma60Series);
      const rsiValue = Number(ind.rsi_14);
      const rsiView = Number.isFinite(rsiValue)
        ? (rsiValue >= 70 ? ["偏熱", `RSI ${fmt(rsiValue)} 位於高檔，強勢股可續強，但追價需降低部位。`]
          : rsiValue <= 30 ? ["偏弱/超賣", `RSI ${fmt(rsiValue)} 位於低檔，若價格止跌才有反彈參考。`]
          : rsiValue >= 50 ? ["多方", `RSI ${fmt(rsiValue)} 大於 50，多方力道略占優。`]
          : ["空方", `RSI ${fmt(rsiValue)} 小於 50，空方力道略占優。`])
        : ["資料不足", "RSI 資料不足。"];
      const kdView = Number.isFinite(Number(lastKd.k)) && Number.isFinite(Number(lastKd.d))
        ? (lastKd.k >= 80 && lastKd.d >= 80 ? ["KD 過熱", `K ${fmt(lastKd.k)} / D ${fmt(lastKd.d)}，短線需防震盪。`]
          : lastKd.k <= 20 && lastKd.d <= 20 ? ["KD 低檔", `K ${fmt(lastKd.k)} / D ${fmt(lastKd.d)}，若價量轉強可觀察反彈。`]
          : lastKd.k > lastKd.d ? ["KD 偏多", `K ${fmt(lastKd.k)} 高於 D ${fmt(lastKd.d)}。`]
          : ["KD 偏弱", `K ${fmt(lastKd.k)} 低於 D ${fmt(lastKd.d)}。`])
        : ["KD 資料不足", "KD 需要更多高低收資料。"];
      const volumeView = theoryVolumeView(latest, previous, volRatio);
      const scoreParts = [candle.score, pattern.score, dow.score, ma.score, volumeView.score];
      if (rsiView[0] === "多方") scoreParts.push(8);
      if (rsiView[0] === "偏熱") scoreParts.push(-4);
      if (rsiView[0] === "空方") scoreParts.push(-8);
      if (kdView[0] === "KD 偏多") scoreParts.push(5);
      if (kdView[0] === "KD 過熱") scoreParts.push(-3);
      if (kdView[0] === "KD 偏弱") scoreParts.push(-5);
      const total = scoreParts.reduce((sum, value) => sum + value, 50);
      const verdict = total >= 72 ? "理論偏多" : total >= 58 ? "偏多觀察" : total <= 38 ? "理論偏空" : total <= 48 ? "偏弱整理" : "多空拉鋸";
      const cards = [
        ["K 線", candle.label, candle.detail],
        ["K 線組合", pattern.label, pattern.detail],
        ["道氏趨勢", dow.label, dow.detail],
        ["均線法則", ma.label, ma.detail],
        ["價量關係", volumeView.label, volumeView.detail],
        ["RSI / KD", `${rsiView[0]}｜${kdView[0]}`, `${rsiView[1]} ${kdView[1]}`],
      ];
      target.innerHTML = `
        <div class="trade-callout">
          <span>${latest.date || "最新 K 線"}｜整合理論</span>
          <strong>${verdict}</strong>
          <p>依 K 線實體與影線、常見 K 線組合、道氏高低點、均線位置、RSI/KD 與價量關係產生。此為研究輔助，仍需搭配法人、基本面與風控。</p>
        </div>
        <div class="facet-grid">
          ${cards.map(([name, stance, detail]) => `
            <div class="facet-card" title="${detail}">
              <span>${name}</span>
              <strong>${stance}</strong>
              <small>${detail}</small>
            </div>
          `).join("")}
        </div>
      `;
    }
    function classifyTheoryCandle(row) {
      const open = Number(row.open);
      const close = Number(row.close);
      const high = Number(row.high);
      const low = Number(row.low);
      const range = Math.max(0.0001, high - low);
      const body = Math.abs(close - open);
      const upper = high - Math.max(open, close);
      const lower = Math.min(open, close) - low;
      const isUp = close >= open;
      if (body / range <= 0.12) return { label: "十字線", score: 0, detail: "開收接近，多空暫時均衡，後續常需看隔日方向確認。" };
      if (lower / range >= 0.55 && upper / range <= 0.18) return { label: isUp ? "T 字買盤" : "長下影支撐", score: 10, detail: "下影線長，代表低檔承接力道較明顯。" };
      if (upper / range >= 0.55 && lower / range <= 0.18) return { label: "倒 T 賣壓", score: -12, detail: "上影線長，代表高檔賣壓或追價失敗。" };
      if (isUp && body / range >= 0.62) return { label: "長紅實體", score: 12, detail: "紅 K 實體較大，買盤主導當日走勢。" };
      if (!isUp && body / range >= 0.62) return { label: "長黑實體", score: -12, detail: "黑 K 實體較大，賣盤主導當日走勢。" };
      return { label: isUp ? "小紅整理" : "小黑整理", score: isUp ? 3 : -3, detail: "單根 K 線訊號普通，需搭配趨勢與量能。" };
    }
    function detectTheoryPattern(rows) {
      const last3 = rows.slice(-3);
      const [a, b, c] = last3;
      const up = row => Number(row.close) > Number(row.open);
      const down = row => Number(row.close) < Number(row.open);
      const body = row => Math.abs(Number(row.close) - Number(row.open));
      const range = row => Math.max(0.0001, Number(row.high) - Number(row.low));
      const small = row => body(row) / range(row) < 0.28;
      if (last3.length === 3 && down(a) && small(b) && up(c) && Number(c.close) > (Number(a.open) + Number(a.close)) / 2) {
        return { label: "早晨之星雛形", score: 16, detail: "跌勢後出現小實體，再以長紅收復前段，屬可能止跌轉強訊號。" };
      }
      if (last3.length === 3 && up(a) && small(b) && down(c) && Number(c.close) < (Number(a.open) + Number(a.close)) / 2) {
        return { label: "黃昏之星雛形", score: -16, detail: "漲勢後小實體轉長黑，屬可能反轉或回調訊號。" };
      }
      if (last3.length === 3 && last3.every(up) && Number(b.close) > Number(a.close) && Number(c.close) > Number(b.close)) {
        return { label: "紅三兵", score: 14, detail: "連續三根收高紅 K，買盤延續，但仍要留意是否過熱。" };
      }
      if (last3.length === 3 && last3.every(down) && Number(b.close) < Number(a.close) && Number(c.close) < Number(b.close)) {
        return { label: "三隻烏鴉", score: -16, detail: "連續三根收低黑 K，賣壓延續，需優先風控。" };
      }
      return { label: "無明確組合", score: 0, detail: "近三日未形成強烈反轉或連續攻擊型態。" };
    }
    function detectDowTrend(rows) {
      const recent = rows.slice(-20);
      const prior = rows.slice(-40, -20);
      if (recent.length < 10 || prior.length < 10) return { label: "資料不足", score: 0, detail: "道氏趨勢需要足夠高低點比較。" };
      const recentHigh = Math.max(...recent.map(row => Number(row.high)));
      const recentLow = Math.min(...recent.map(row => Number(row.low)));
      const priorHigh = Math.max(...prior.map(row => Number(row.high)));
      const priorLow = Math.min(...prior.map(row => Number(row.low)));
      if (recentHigh > priorHigh && recentLow > priorLow) return { label: "多頭結構", score: 12, detail: "近期高點與低點都墊高，符合道氏多頭結構。" };
      if (recentHigh < priorHigh && recentLow < priorLow) return { label: "空頭結構", score: -12, detail: "近期高點與低點都下移，符合道氏空頭結構。" };
      return { label: "箱型震盪", score: 0, detail: "高低點未同向，偏區間整理或趨勢未明。" };
    }
    function theoryMaView(close, ma20Series, ma60Series) {
      const ma20 = ma20Series[ma20Series.length - 1];
      const ma60 = ma60Series[ma60Series.length - 1];
      const prev20 = ma20Series[ma20Series.length - 2];
      const prev60 = ma60Series[ma60Series.length - 2];
      if (!Number.isFinite(ma20)) return { label: "資料不足", score: 0, detail: "均線資料不足。" };
      if (Number.isFinite(ma60) && close > ma20 && ma20 > ma60) return { label: "多頭排列", score: 14, detail: `收盤站上月線，且月線高於季線，趨勢偏多。` };
      if (Number.isFinite(ma60) && close < ma20 && ma20 < ma60) return { label: "空頭排列", score: -14, detail: `收盤跌破月線，且月線低於季線，趨勢偏空。` };
      if (Number.isFinite(prev20) && Number.isFinite(prev60) && prev20 <= prev60 && ma20 > ma60) return { label: "黃金交叉", score: 16, detail: "短期均線向上突破長期均線，屬轉強訊號。" };
      if (Number.isFinite(prev20) && Number.isFinite(prev60) && prev20 >= prev60 && ma20 < ma60) return { label: "死亡交叉", score: -16, detail: "短期均線向下跌破長期均線，屬轉弱訊號。" };
      return { label: close >= ma20 ? "站上月線" : "跌破月線", score: close >= ma20 ? 6 : -8, detail: `收盤 ${fmt(close)}，SMA20 ${fmt(ma20)}。` };
    }
    function theoryVolumeView(latest, previous, volRatio) {
      const up = Number(latest.close) > Number(previous.close);
      const down = Number(latest.close) < Number(previous.close);
      if (!Number.isFinite(volRatio)) return { label: "量能不足", score: 0, detail: "成交量資料不足。" };
      if (up && volRatio >= 1.3) return { label: "價漲量增", score: 10, detail: `量比 ${fmt(volRatio)}，上漲有量能確認。` };
      if (down && volRatio >= 1.3) return { label: "價跌量增", score: -12, detail: `量比 ${fmt(volRatio)}，下跌伴隨放量，需防賣壓延續。` };
      if (up && volRatio < 0.8) return { label: "價漲量縮", score: -2, detail: `量比 ${fmt(volRatio)}，上漲追價力道不足。` };
      if (down && volRatio < 0.8) return { label: "價跌量縮", score: 3, detail: `量比 ${fmt(volRatio)}，跌勢賣壓暫未放大。` };
      return { label: "量價中性", score: 0, detail: `量比 ${fmt(volRatio)}，量價尚未形成強烈確認。` };
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
      setChartBox(target, width, height);
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
      setChartBox(target, width, height);
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
      setChartBox(target, width, height);
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
      setChartBox(target, width, height);
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
      setChartBox(target, width, height);
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
      setChartBox(target, width, height);
      if (!rows.length) {
        target.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">${data.message || "法人資料不足"}</text>`;
        return;
      }
      const colors = { foreign: "#38bdf8", investment: "#f59e0b", dealer: "#22c55e", total: "#e879f9", retail_proxy: "#a78bfa" };
      const labels = { foreign: "外資", investment: "投信", dealer: "自營商", total: "法人合計", retail_proxy: "散戶代理" };
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
    function renderInstitutionalSummary(target, data) {
      if (!target) return;
      const rows = data.rows || [];
      const summary = data.summary || {};
      if (!rows.length) {
        target.innerHTML = `<div class="trade-callout"><strong>法人資料不足</strong><p>${data.message || "目前沒有三大法人資料。"}</p></div>`;
        return;
      }
      const last = summary.last || rows[rows.length - 1] || {};
      const totals = summary.twenty_day || {};
      const fmtLots = value => `${fmt(Number(value || 0) / 1000, 0)} 張`;
      const flowText = Number(totals.total || 0) >= 0 ? "20 日法人合計偏買" : "20 日法人合計偏賣";
      target.innerHTML = `
        <div class="institution-grid">
          <div class="institution-card wide"><span>唯一法人資料區｜${data.source || "三大法人"}</span><strong>${summary.stance || flowText}</strong><small>${data.message || "外資、投信、自營商買賣超彙整。"}</small></div>
          <div class="institution-card"><span>最新外資</span><strong class="${pctClass(last.foreign)}">${fmtLots(last.foreign)}</strong><small>${last.date || "-"} 買賣超</small></div>
          <div class="institution-card"><span>最新投信</span><strong class="${pctClass(last.investment)}">${fmtLots(last.investment)}</strong><small>${last.date || "-"} 買賣超</small></div>
          <div class="institution-card"><span>最新自營商</span><strong class="${pctClass(last.dealer)}">${fmtLots(last.dealer)}</strong><small>${last.date || "-"} 買賣超</small></div>
          <div class="institution-card"><span>20 日合計</span><strong class="${pctClass(totals.total)}">${fmtLots(totals.total)}</strong><small>連續方向 ${fmt(summary.streak, 0)} 日</small></div>
        </div>
      `;
    }
    function renderInstitutionalProAnalysis(target, data, stock = {}, ind = {}, signal = {}) {
      if (!target) return;
      const rows = data.rows || [];
      if (!rows.length) {
        target.innerHTML = `<div class="trade-callout"><strong>基金資金資料不足</strong><p>${data.message || "目前沒有國內外基金資金流資料。"}</p></div>`;
        return;
      }
      const sumRows = (source, key) => source.reduce((total, row) => total + Number(row[key] || 0), 0);
      const latest = rows[rows.length - 1] || {};
      const recent5 = rows.slice(-5);
      const recent20 = rows.slice(-20);
      const fmtLots = value => `${fmt(Number(value || 0) / 1000, 0)} 張`;
      const summary = data.summary || {};
      const totals = summary.twenty_day || {};
      const institutionTone = (foreign, investment) => {
        if (foreign > 0 && investment > 0) return "國外基金與國內基金同步偏買，資金方向較一致。";
        if (foreign < 0 && investment < 0) return "國外基金與國內基金同步偏賣，先提高風控權重。";
        if (foreign > 0 && investment < 0) return "國外基金偏買、國內基金偏賣，短線可能是不同週期資金換手。";
        if (foreign < 0 && investment > 0) return "國外基金偏賣、國內基金偏買，留意國內基金是否有護盤或作帳需求。";
        return "國內外基金方向不明顯，需搭配價格與量能確認。";
      };
      const foreign20 = sumRows(recent20, "foreign");
      const investment20 = sumRows(recent20, "investment");
      const dealer20 = sumRows(recent20, "dealer");
      const total20 = sumRows(recent20, "total");
      const netScore = (foreign20 > 0 ? 12 : foreign20 < 0 ? -12 : 0) + (investment20 > 0 ? 10 : investment20 < 0 ? -10 : 0) + (dealer20 > 0 ? 4 : dealer20 < 0 ? -4 : 0);
      const rating = netScore >= 18 ? "資金面偏多" : netScore >= 6 ? "偏多觀察" : netScore <= -18 ? "資金面偏空" : netScore <= -6 ? "偏弱觀察" : "中性觀察";
      const technicalText = Number(ind.return_20d) >= 8
        ? "20 日動能偏強，外資報告會要求確認追價風險與停損條件。"
        : Number(ind.return_20d) <= -8
          ? "20 日動能偏弱，外資報告會先檢查是否落入價值陷阱或趨勢破壞。"
          : "20 日動能中性，外資報告會等待區間突破或基本面催化。";
      const cards = [
        ["最新外資/國外基金", latest.foreign, `${latest.date || "-"} 外資買賣超`],
        ["最新投信/國內基金", latest.investment, `${latest.date || "-"} 投信買賣超`],
        ["最新自營商", latest.dealer, `${latest.date || "-"} 自營商買賣超`],
        ["20 日法人合計", total20 || totals.total, `連續方向 ${fmt(summary.streak, 0)} 日`],
        ["20 日國外基金", foreign20, "外資中期方向"],
        ["20 日國內基金", investment20, "投信中期方向"],
      ];
      target.innerHTML = `
        <div class="trade-callout">
          <span>${data.source || "公開法人資料"}｜${rating}</span>
          <strong>${institutionTone(foreign20, investment20)}</strong>
          <p>三大法人籌碼與基金代理合併在此：外資作國外基金代理，投信作國內基金代理，自營商作交易性資金參考。</p>
        </div>
        <div class="institution-grid">
          ${cards.map(([label, value, note]) => `
            <div class="institution-card">
              <span>${label}</span>
              <strong class="${pctClass(value)}">${fmtLots(value)}</strong>
              <small>${note}</small>
            </div>
          `).join("")}
        </div>
        <div class="trade-callout">
          <span>資金面結論</span>
          <strong>${stock.code || ""} ${stock.short_name || stock.name || ""}｜AI ${fmt(signal.risk_adjusted_score, 0)}｜${signal.signal || "觀察"}</strong>
          <p>${technicalText} 後續只追蹤外資/投信是否同向、法人合計是否連續買超，以及價格是否守住關鍵均線。</p>
        </div>
      `;
    }
    function renderFundHoldingReports(target, data) {
      if (!target) return;
      const items = data.items || [];
      const summary = data.summary || {};
      const fmtShares = value => `${fmt(Number(value || 0) / 1000, 0)} 張`;
      if (!items.length) {
        target.innerHTML = `
          <div class="trade-callout">
            <span>需要單一基金明細</span>
            <strong>尚未匯入各基金持股資料</strong>
            <p>${data.message || "公開三大法人資料無法拆成單一基金。請匯入基金持股明細後，這裡才會顯示每個基金買了多少。"}</p>
            <p>目前官方免費公開端點沒有單一基金逐檔即時持股明細；系統已先自動補齊外資、投信、自營商買賣超。取得基金月報或持股揭露資料後，放到 ${data.csv_path || "data/fund_holdings.csv"} 會自動計算各基金前後期增減。</p>
          </div>
        `;
        return;
      }
      target.innerHTML = `
        <div class="trade-callout">
          <span>${summary.latest_date || "最新揭露"}｜${fmt(summary.funds, 0)} 檔基金</span>
          <strong>合計持有 ${fmtShares(summary.total_shares)}｜前期增減 ${fmtShares(summary.total_change)}</strong>
          <p>${data.message || "已依單一基金持股明細計算。"}</p>
        </div>
        <div class="institution-grid">
          ${items.slice(0, 6).map(item => {
            const change = item.share_change;
            const changeText = change === null || change === undefined ? "前期無資料" : `${change >= 0 ? "+" : ""}${fmtShares(change)}`;
            const report = item.summary || `${item.fund_type || "基金"} ${item.action || "揭露"}，目前持有 ${fmtShares(item.shares)}。`;
            return `
              <div class="institution-card">
                <span>${item.manager || item.fund_type || "基金"}｜${item.report_date || "-"}</span>
                <strong>${item.fund_name}</strong>
                <small>持有 ${fmtShares(item.shares)}｜增減 <b class="${pctClass(change || 0)}">${changeText}</b></small>
                <small>權重 ${fmt(item.weight)}%｜市值 ${fmt(item.market_value, 0)}｜${item.source || "未標示來源"}</small>
                <small>${report}</small>
              </div>
            `;
          }).join("")}
        </div>
        ${items.length > 6 ? `<div class="note">另有 ${items.length - 6} 檔基金明細已收進資料，為避免版面過長先只顯示前 6 檔。</div>` : ""}
      `;
    }
    function renderFinancialKpis(target, data) {
      if (!target) return;
      const latest = data.latest;
      const items = data.items || [];
      if (!latest) {
        target.innerHTML = `
          <div class="trade-callout">
            <span>需要財報 KPI</span>
            <strong>尚未匯入財報與估值資料</strong>
            <p>${data.message || "目前沒有財報 KPI。匯入後可補齊月營收、EPS、毛利率、ROE、PE/PB、殖利率等資料。"}</p>
            <p>系統會自動重試 TWSE/TPEx 官方公開端點；若官方端點暫時無資料，才使用 ${data.csv_path || "data/financial_kpis.csv"} 作為補充資料源。</p>
          </div>
        `;
        return;
      }
      const companyContext = data.company_context || {};
      const governance = companyContext.governance || {};
      const dividend = companyContext.dividend || {};
      const cards = [
        ["期間", latest.period, latest.source || "財報/公開資料"],
        ["營收", fmt(latest.revenue, 0), `年增率 ${fmt(latest.revenue_yoy)}%`],
        ["EPS", fmt(latest.eps), "每股盈餘"],
        ["毛利率", `${fmt(latest.gross_margin)}%`, `營益率 ${fmt(latest.operating_margin)}%`],
        ["估值", `PE ${fmt(latest.pe)}｜PB ${fmt(latest.pb)}`, `ROE ${fmt(latest.roe)}%｜殖利率 ${fmt(latest.dividend_yield)}%`],
        ["治理/股利", `董監 ${fmt(governance.rows, 0)} 筆｜股利 ${fmt(dividend.cash_dividend)}`, `質押 ${fmt(governance.avg_pledge_ratio)}%｜${dividend.status || "公告資料"}`],
      ];
      target.innerHTML = `
        <div class="trade-callout">
          <span>${latest.period || "最新財報"}｜${items.length} 期資料</span>
          <strong>${latest.summary || "已補齊財報與估值 KPI"}</strong>
          <p>${data.message || "已匯入財報與估值 KPI。"}</p>
        </div>
        <div class="institution-grid">
          ${cards.map(([label, value, note]) => `
            <div class="institution-card">
              <span>${label}</span>
              <strong>${value}</strong>
              <small>${note}</small>
            </div>
          `).join("")}
        </div>
      `;
    }
    async function fetchStatus() {
      const data = await getJson("/api/status");
      document.getElementById("range").textContent = `${data.last_date} 官方 ${data.official_count || 0} 檔 / 最新 ${data.latest_count || 0} 檔`;
    }
    async function fetchStock() {
      const panelInput = document.getElementById("stockPageCode");
      const panelCode = panelInput ? panelInput.value.trim() : "";
      const codeValue = (document.getElementById("stockPage")?.classList.contains("active") ? panelCode : "") || document.getElementById("code").value.trim() || panelCode || "2330";
      document.getElementById("code").value = codeValue;
      if (panelInput) panelInput.value = codeValue;
      currentStockCode = codeValue;
      currentStockInterval = "1d";
      document.querySelectorAll("[data-interval]").forEach(tab => tab.classList.toggle("active", tab.dataset.interval === "1d"));
      const encoded = encodeURIComponent(codeValue);
      const [stock, ind, prices, signal, monitor, institutional, fundHoldings, financialKpis] = await Promise.all([
        getJson(`/api/stock?code=${encoded}`),
        getJson(`/api/indicators?code=${encoded}`),
        getJson(`/api/prices?code=${encoded}&limit=160`),
        getJson(`/api/stock-signal?code=${encoded}`),
        getJson(`/api/ai-monitor-stock?code=${encoded}`),
        getJson(`/api/institutional?code=${encoded}`),
        getJson(`/api/fund-holdings?code=${encoded}`),
        getJson(`/api/financial-kpis?code=${encoded}`),
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
      updateAnalysisCoverage({ stock, ind, prices, signal, monitor, institutional, fundHoldings, financialKpis, fundamental: undefined, news: undefined });
      renderInstitutionalProAnalysis(document.getElementById("institutionalProAnalysis"), institutional, stock, ind, signal);
      renderFundHoldingReports(document.getElementById("fundHoldingReports"), fundHoldings);
      renderFinancialKpis(document.getElementById("financialKpiPanel"), financialKpis);
      renderChartSuite();
      installChartDrag();
      renderTradePlan(document.getElementById("tradePlan"), buildTradePlan(stock, ind, signal, prices.prices));
      renderTechnicalTheoryAnalysis(document.getElementById("technicalTheoryAnalysis"), prices.prices, ind);
      renderAnalysisFacets(document.getElementById("analysisFacets"), monitor);
      const fundamentalTarget = document.getElementById("fundamentalResearch");
      fundamentalTarget.innerHTML = `<div class="fundamental-card"><span>機構級投資研究</span><strong>背景載入中</strong><ul><li>先顯示 K 線、交易計畫與資金面，十階段研究報告稍後補上。</li></ul></div>`;
      getJson(`/api/fundamental?code=${encoded}`)
        .then(fundamental => {
          renderFundamentalResearch(fundamentalTarget, fundamental);
          updateAnalysisCoverage({ fundamental });
        })
        .catch(error => {
          const fundamental = { error: error.message };
          fundamentalTarget.innerHTML = `<div class="fundamental-card"><span>機構級投資研究</span><strong>暫時不可用</strong><ul><li>${error.message}</li></ul></div>`;
          updateAnalysisCoverage({ fundamental });
        });
      const newsTarget = document.getElementById("newsResearch");
      newsTarget.innerHTML = `<div class="news-card"><span>新聞掃描</span><a href="#">正在背景掃描最新新聞...</a><small>核心分析已先完成，不等待新聞來源。</small></div>`;
      getJson(`/api/news?code=${encoded}`)
        .then(news => {
          renderNews(newsTarget, news);
          updateAnalysisCoverage({ news });
        })
        .catch(error => {
          const news = { message: `新聞掃描暫時失敗：${error.message}`, items: [] };
          renderNews(newsTarget, news);
          updateAnalysisCoverage({ news });
        });
      document.getElementById("diagScore").textContent = fmt(signal.intelli_score, 1);
      document.getElementById("diagSentiment").textContent = signal.sentiment;
      document.getElementById("diagRiskScore").textContent = fmt(signal.risk_adjusted_score, 0);
      document.getElementById("stockPageCode").value = stock.code;
      document.getElementById("stockHeroName").textContent = `${stock.code} ${stock.short_name || stock.name}`;
      document.getElementById("stockHeroSub").textContent = `${stock.market} · ${signal.signal} · ${signal.sentiment} · 解盤看交易判斷，專業分析看國內外基金、法人、基本面與消息`;
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
    function searchStockFromPanel() {
      const panelCode = document.getElementById("stockPageCode").value.trim() || "2330";
      document.getElementById("code").value = panelCode;
      searchStock();
    }
    async function fetchScan() {
      const data = await getJson("/api/scan?limit=20");
      renderRankList(document.getElementById("returnTable"), data.top_return_20d, {
        title: "漲幅排行",
        icon: "↗",
        metric: row => `漲幅 ${fmt(row.return_20d)}%`,
      });
      renderRankList(document.getElementById("volumeTable"), data.top_volume_expansion, {
        title: "量能放大",
        icon: "▥",
        metric: row => `量比 ${fmt(row.volume_ratio)}`,
      });
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
      await fetchIndustryStocks();
      renderRankList(document.getElementById("hubSignalsTable"), signals.top_signals || [], {
        title: "AI 智選",
        icon: "◎",
        metric: row => `AI ${fmt(row.score, 0)}｜${row.signal || "觀察"}`,
      });
      renderRankList(document.getElementById("hubReturnTable"), scan.top_return_20d || [], {
        title: "強勢排行",
        icon: "↗",
        metric: row => `20日 ${fmt(row.return_20d)}%`,
      });
      renderRankList(document.getElementById("hubVolumeTable"), scan.top_volume_expansion || [], {
        title: "量能焦點",
        icon: "▥",
        metric: row => `量比 ${fmt(row.volume_ratio)}`,
      });
    }
    async function fetchPremarket(force = false) {
      const data = await getJson(`/api/premarket?limit=8${force ? "&force=1" : ""}&_=${Date.now()}`);
      const snapshot = data.snapshot || {};
      document.getElementById("premarketNotice").textContent =
        `${snapshot.prepared_at || "已更新"}｜${snapshot.stance || "資料不足"}｜${snapshot.summary || ""}`;
      renderPremarketKpis(snapshot, data.news || {});
      renderPremarketFactors(document.getElementById("premarketFactorsTable"), snapshot.items || []);
      renderPremarketWatch(document.getElementById("premarketWatchTable"), data.watchlist || []);
      renderNews(document.getElementById("premarketNews"), data.news || {});
    }
    function renderPremarketKpis(snapshot, news) {
      const factors = snapshot.factors || [];
      const highNews = (news.items || []).filter(item => item.impact === "高").length;
      document.getElementById("premarketKpis").innerHTML = [
        ["早盤預估", snapshot.stance || "資料不足", `分數 ${fmt(snapshot.score, 0)}`],
        ["主要因子", `${fmt(factors.length, 0)} 項`, factors.slice(0, 2).map(item => item.name).join("、") || "暫無"],
        ["新聞風險", `${fmt(highNews, 0)} 則高影響`, `${fmt((news.items || []).length, 0)} 則已掃描`],
        ["更新頻率", "3 分鐘", "自動刷新海外期貨、ADR、ETF 與新聞"],
      ].map(([label, value, note]) => `
        <div class="module-card"><strong>${value}</strong><span>${label}｜${note}</span></div>
      `).join("");
    }
    function renderPremarketFactors(target, rows) {
      target.innerHTML = `
        <thead><tr><th>項目</th><th>分類</th><th>最新</th><th>漲跌幅</th><th>分數影響</th><th>時間</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr>
            <td><strong>${row.name}</strong><br><span>${row.symbol || ""}</span></td>
            <td>${row.category || "-"}</td>
            <td>${fmt(row.price)}</td>
            <td class="${pctClass(row.change_percent)}">${fmt(row.change_percent)}%</td>
            <td class="${pctClass(row.contribution)}">${fmt(row.contribution)}</td>
            <td>${row.last_time || "-"}</td>
          </tr>
        `).join("") || "<tr><td colspan='6'>目前抓不到海外/夜盤資料。</td></tr>"}</tbody>
      `;
    }
    function renderPremarketWatch(target, rows) {
      target.innerHTML = `
        <thead><tr><th>股票</th><th>早盤定位</th><th>昨收</th><th>20D</th><th>RSI</th><th>開盤策略</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr onclick="openStock('${row.code}')" style="cursor:pointer">
            <td><strong>${row.name}</strong><br><span>${row.code}</span></td>
            <td>${row.premarket_bias || row.signal || "觀察"}</td>
            <td>${fmt(row.close)}</td>
            <td class="${pctClass(row.return_20d)}">${fmt(row.return_20d)}%</td>
            <td>${fmt(row.rsi_14)}</td>
            <td>${row.premarket_action || "先看開盤量價，再決定是否分批。"}</td>
          </tr>
        `).join("") || "<tr><td colspan='6'>觀察名單尚未加入股票。</td></tr>"}</tbody>
      `;
    }
    async function fetchStrategy() {
      const data = await getJson("/api/strategy");
      const s = data.summary;
      const summaryRows = cloudWebMode ? [
        ["回測起始", s.start_date || "-"],
        ["交易筆數", fmt(s.trades, 0)],
        ["總報酬", `${fmt(s.total_return)}%`],
        ["勝率", `${fmt(s.win_rate)}%`],
        ["平均單筆", `${fmt(s.avg_trade_return)}%`],
        ["中位數單筆", `${fmt(s.median_trade_return)}%`],
        ["最大回撤", `${fmt(s.max_drawdown)}%`],
      ] : [
        ["開始操盤", s.start_date || "2026-05-01"],
        ["起始資金", fmt(s.initial_capital)],
        ["最大持股數", fmt(s.max_positions, 0)],
        ["最長持有", `${fmt(s.horizon, 0)} 個交易日`],
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
      renderStrategyAdvice(document.getElementById("strategyAdvice"), data);
      renderStrategyStockCards(document.getElementById("strategyStockCards"), (data.market_context || {}).leaders || []);
      document.querySelectorAll("[data-strategy-section]").forEach(section => {
        section.style.display = cloudWebMode ? "none" : "";
      });
      if (!cloudWebMode) {
        renderStrategyEntries(document.getElementById("strategyEntriesTable"), data.recent_entries || []);
        renderStrategyTrades(document.getElementById("strategyTradesTable"), data.closed_trades || data.recent_trades || []);
        renderStrategyOpen(document.getElementById("strategyOpenTable"), data.open_positions || []);
      }
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
      const modeText = cloudWebMode
        ? "網頁版：觀察名單用於畫面、盤前分析與即時看盤。"
        : currentUserKey
          ? "個人模式：觀察名單會綁定你的使用者代碼，會同步到即時看盤與 Telegram 推播。"
          : publicDemoMode ? "公開展示模式：觀察名單只存在此瀏覽器，不會影響主機與推播。" : "本機模式：觀察名單會保存在主機資料庫，會同步到即時看盤與既有推播設定。";
      document.getElementById("watchlistHint").textContent = `目前觀察 ${majors.length} 檔。可拖曳表格左側排序。${modeText}`;
      renderAfterHoursIndustryReport(document.getElementById("watchAfterSummary"), data.industry_after_report || {});
    }
    function renderAfterHoursIndustryReport(target, report) {
      if (!target) return;
      const summary = report.summary || {};
      const top = report.top_groups || [];
      const volume = report.volume_focus || [];
      const weak = report.weak_groups || [];
      const rowHtml = rows => rows.map(row => {
        const leader = row.leader || {};
        const strongest = row.strongest || {};
        return `
          <tr>
            <td><strong>${row.name}</strong><br><span>${fmt(row.count, 0)} 檔｜漲 ${fmt(row.advancers, 0)} / 跌 ${fmt(row.decliners, 0)}</span></td>
            <td class="${pctClass(row.avg_change_percent)}">${fmt(row.avg_change_percent)}%</td>
            <td>${fmt(row.breadth, 0)}%</td>
            <td>${fmt(row.avg_volume_ratio)}</td>
            <td>${leader.name || "-"} ${leader.code || ""}</td>
            <td>${strongest.name || "-"} ${strongest.code || ""} ${strongest.change_percent === undefined ? "" : fmt(strongest.change_percent) + "%"}</td>
            <td>${row.stance}<br><span>${row.action}</span></td>
          </tr>
        `;
      }).join("");
      target.innerHTML = `
        <div class="trade-callout">
          <span>${report.date || "最新資料"}｜共 ${fmt(summary.groups, 0)} 個類股/主題</span>
          <strong>盤後類股報告｜強勢 ${fmt(summary.strong_groups, 0)} 組｜轉弱 ${fmt(summary.weak_groups, 0)} 組</strong>
          <p>依各產業當日平均漲跌、上漲家數占比、20 日動能、量比與龍頭股整理，作為隔日早盤觀察順序。</p>
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>類股</th><th>均漲跌</th><th>上漲占比</th><th>量比</th><th>龍頭</th><th>最強股</th><th>盤後判讀</th></tr></thead>
          <tbody>${rowHtml(top) || "<tr><td colspan='7'>目前沒有類股資料。</td></tr>"}</tbody>
        </table></div>
        <div class="strategy-guide" style="margin-top:12px">
          <div><strong>量能焦點</strong><span>${volume.slice(0, 4).map(row => `${row.name} ${fmt(row.avg_volume_ratio)}`).join("｜") || "無資料"}</span></div>
          <div><strong>弱勢警戒</strong><span>${weak.slice(0, 4).map(row => `${row.name} ${fmt(row.avg_change_percent)}%`).join("｜") || "無資料"}</span></div>
          <div><strong>隔日策略</strong><span>強勢類股等回測，不追開盤急拉；弱勢類股先檢查是否跌破支撐與停損。</span></div>
          <div><strong>資料定位</strong><span>這裡改為類股報告；AI 智選、強勢排行與量能焦點仍集中在股票探索。</span></div>
        </div>
      `;
    }
    async function addWatchlistCode() {
      const code = document.getElementById("watchlistCode").value.trim();
      if (currentUserKey) {
        const data = await getJson(`/api/user/watchlist/add?user_key=${encodeURIComponent(currentUserKey)}&code=${encodeURIComponent(code)}`);
        renderMajors(document.getElementById("watchMajorsTable"), data.watchlist || []);
        document.getElementById("watchlistHint").textContent = `已加入 ${code}。這是你的個人觀察名單，已同步到推播。`;
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
      document.getElementById("watchlistHint").textContent = `已加入 ${code}，目前觀察 ${(data.watchlist || []).length} 檔，已同步到本機推播。`;
    }
    async function removeWatchlistCode(code) {
      if (currentUserKey) {
        const data = await getJson(`/api/user/watchlist/remove?user_key=${encodeURIComponent(currentUserKey)}&code=${encodeURIComponent(code)}`);
        renderMajors(document.getElementById("watchMajorsTable"), data.watchlist || []);
        document.getElementById("watchlistHint").textContent = `已移除 ${code}。這是你的個人觀察名單，已同步到推播。`;
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
      document.getElementById("watchlistHint").textContent = `已移除 ${code}，目前觀察 ${(data.watchlist || []).length} 檔，已同步到本機推播。`;
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
      const panelInput = document.getElementById("stockPageCode");
      if (panelInput) panelInput.value = code;
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
        hintEl.textContent = "請先建立個人設定。朋友也可以直接到 Telegram 對 bot 輸入 /start 自動建立。";
        return;
      }
      keyEl.textContent = user.user_key;
      document.getElementById("telegramChatId").value = user.telegram_chat_id || "";
      statusEl.textContent = user.telegram_enabled ? `已啟用：${user.telegram_chat_id}` : "尚未啟用";
      hintEl.textContent = `${user.display_name} 的個人設定已啟用。可在 Telegram 對 bot 輸入「綁定 ${user.user_key}」完成推播綁定；同一個瀏覽器會自動記住。`;
    }
    async function createUserProfile() {
      const name = document.getElementById("notifyName").value.trim() || "朋友";
      const codes = await currentWatchlistCodesForSync();
      const data = await getJson(`/api/user/create?name=${encodeURIComponent(name)}`);
      currentUserKey = data.user.user_key;
      localStorage.setItem(userKeyStorageKey, currentUserKey);
      updateNotifyUi(data.user);
      await syncUserWatchlistCodes(codes, true);
      document.getElementById("notifyHint").textContent = `${data.user.display_name} 的個人設定已建立，並已把目前網頁關注名單同步到推播名單。`;
      await fetchWatch();
    }
    async function saveUserTelegram() {
      if (!currentUserKey) throw new Error("請先建立個人設定。");
      const chatId = document.getElementById("telegramChatId").value.trim();
      const codes = await currentWatchlistCodesForSync();
      const data = await getJson(`/api/user/telegram/save?user_key=${encodeURIComponent(currentUserKey)}&chat_id=${encodeURIComponent(chatId)}`);
      updateNotifyUi(data.user);
      const syncData = await syncUserWatchlistCodes(codes, true);
      document.getElementById("notifyHint").textContent = `${data.user.display_name} 的 Telegram 已儲存。${syncData.message || "已同步目前網頁關注名單到推播名單。"}`;
    }
    async function sendUserTelegramTest() {
      if (!currentUserKey) throw new Error("請先建立個人設定。");
      const data = await getJson(`/api/user/telegram/test?user_key=${encodeURIComponent(currentUserKey)}`);
      document.getElementById("notifyHint").textContent = data.message || "測試推播已送出。";
    }
    async function fetchAdminUsers() {
      const token = document.getElementById("adminToken").value.trim();
      if (!token) throw new Error("請輸入管理員 token。");
      const data = await getJson(`/api/admin/users?token=${encodeURIComponent(token)}`);
      if (data.error) throw new Error(data.error);
      const s = data.summary || {};
      renderDl(document.getElementById("adminUserSummary"), [
        ["朋友帳號", `${fmt(s.total, 0)} 位`],
        ["已啟用 Telegram", `${fmt(s.telegram_enabled, 0)} 位`],
        ["已有觀察名單", `${fmt(s.with_watchlist, 0)} 位`],
      ]);
      const rows = data.users || [];
      document.getElementById("adminUsersTable").innerHTML = `
        <thead><tr><th>名稱</th><th>Telegram</th><th>觀察檔數</th><th>觀察名單</th><th>建立時間</th><th>更新時間</th></tr></thead>
        <tbody>${rows.map(row => `
          <tr>
            <td title="${row.user_key}">${row.display_name}</td>
            <td>${row.telegram_enabled ? "已啟用" : "未啟用"} ${row.telegram_chat_id || ""}</td>
            <td>${fmt(row.watchlist_count, 0)}</td>
            <td>${(row.watchlist_codes || []).join(", ") || "-"}</td>
            <td>${row.created_at || "-"}</td>
            <td>${row.updated_at || "-"}</td>
          </tr>
        `).join("") || "<tr><td colspan='6'>目前沒有朋友帳號。</td></tr>"}</tbody>
      `;
    }
    async function setAdminTelegramWebhook() {
      const token = document.getElementById("adminToken").value.trim();
      const url = document.getElementById("telegramWebhookUrl").value.trim();
      if (!token) throw new Error("請輸入管理員 token。");
      if (!url) throw new Error("請輸入 Telegram webhook URL。");
      const data = await getJson(`/api/admin/telegram-webhook?token=${encodeURIComponent(token)}&url=${encodeURIComponent(url)}`);
      if (data.error) throw new Error(data.error);
      const info = data.webhook_info || {};
      renderDl(document.getElementById("adminUserSummary"), [
        ["Webhook", info.ok ? "已設定" : "設定失敗"],
        ["URL", (info.result && info.result.url) || url],
        ["待處理訊息", `${fmt(info.result && info.result.pending_update_count, 0)} 則`],
        ["最後錯誤", (info.result && info.result.last_error_message) || "無"],
      ]);
      document.getElementById("notifyHint").textContent = "Telegram webhook 已更新。請到新 bot 輸入 /start 測試。";
    }
    let realtimeTimer = null;
    let premarketTimer = null;
    let selectedRealtimeCode = "";
    let latestRealtimeRows = [];
    async function realtimeCodesFromWatchlist() {
      const manual = document.getElementById("realtimeCodes").value.trim();
      const data = await getWatchlistData();
      const codes = (data.codes || []).join(",");
      if (!manual && codes) {
        document.getElementById("realtimeCodes").value = codes;
        return codes;
      }
      return manual || codes;
    }
    async function fetchRealtime() {
      document.getElementById("realtimeNotice").textContent = "正在載入觀察名單報價與 AI 盯盤...";
      document.getElementById("aiMonitorSummary").innerHTML = `<div class="trade-callout"><strong>AI 盯盤載入中</strong><p>正在整理觀察名單、盤中狀態與風控訊號。</p></div>`;
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
        getJson(`/api/realtime?codes=${encodeURIComponent(codes)}&_=${Date.now()}`),
        getJson(`/api/ai-monitor?_=${Date.now()}`),
      ]);
      const quotes = data.quotes || [];
      renderRealtime(document.getElementById("realtimeTable"), quotes);
      renderRealtimeMonitor(monitor);
      const alerts = data.large_order_alerts || [];
      const alertText = alerts.length ? ` 偵測到 ${alerts.length} 筆即時大單。` : "";
      document.getElementById("realtimeNotice").textContent = `${data.message || "即時看盤資料已更新。"} 顯示 ${quotes.length} 檔。${alertText}`;
      if (quotes.length) {
        const nextCode = quotes.some(row => row.code === selectedRealtimeCode) ? selectedRealtimeCode : quotes[0].code;
        await selectRealtimeTrend(nextCode);
      } else {
        document.getElementById("realtimeTrendSummary").innerHTML = "";
        document.getElementById("realtimeTrendChart").innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">目前沒有報價資料</text>`;
        document.getElementById("realtimeTrendNotice").textContent = "請確認觀察名單股票代號。";
      }
    }
    function renderRealtimeMonitor(monitor) {
      const m = (monitor && monitor.summary) || {};
      const isIntraday = !!m.is_intraday;
      if (!isIntraday) {
        document.getElementById("aiMonitorSummary").innerHTML = `
          <div class="trade-callout">
            <strong>AI 盤中盯盤未啟動</strong>
            <p>${m.message || "AI 盯盤只在台股盤中 09:00-13:30 顯示。"}</p>
          </div>`;
        renderAiMonitor(document.getElementById("aiMonitorTable"), []);
        return;
      }
      document.getElementById("aiMonitorSummary").innerHTML = `
        <dl>
          <dt>模式</dt><dd>${m.session_label || "盤中盯盤"}</dd>
          <dt>盯盤狀態</dt><dd>${m.stance || "資料不足"}</dd>
          <dt>風控</dt><dd>${fmt(m.urgent, 0)} 檔</dd>
          <dt>偏多</dt><dd>${fmt(m.positive, 0)} 檔</dd>
          <dt>觀察</dt><dd>${fmt(m.watch, 0)} 檔</dd>
          <dt>時間</dt><dd>${m.now || "-"}</dd>
        </dl>`;
      renderAiMonitor(document.getElementById("aiMonitorTable"), monitor.items || []);
    }
    async function selectRealtimeTrend(code) {
      selectedRealtimeCode = code;
      document.querySelectorAll(".realtime-row").forEach(row => {
        row.classList.toggle("selected", row.dataset.code === code);
      });
      const selectedRow = latestRealtimeRows.find(row => row.code === code);
      if (selectedRow) renderRealtimeBoard(selectedRow);
      const chart = document.getElementById("realtimeTrendChart");
      const notice = document.getElementById("realtimeTrendNotice");
      if (chart) chart.innerHTML = `<text x="50%" y="50%" text-anchor="middle" class="chart-label">正在更新 ${code} 走勢...</text>`;
      if (notice) notice.textContent = `正在更新 ${code} 走勢與儀表板...`;
      const data = await getJson(`/api/realtime-trend?code=${encodeURIComponent(code)}`);
      renderRealtimeTrend(data);
    }
    function startRealtime() {
      stopRealtime();
      runAction(fetchRealtime, "正在刷新即時報價...");
      realtimeTimer = setInterval(() => runAction(fetchRealtime, "自動刷新即時報價..."), 15000);
      setStatus("已啟動 15 秒自動刷新。");
    }
    function ensureRealtimeAutoRefresh() {
      if (realtimeTimer) return;
      realtimeTimer = setInterval(() => runAction(fetchRealtime, "自動刷新即時報價..."), 15000);
    }
    function stopRealtime(silent = false) {
      if (realtimeTimer) clearInterval(realtimeTimer);
      realtimeTimer = null;
      if (!silent) setStatus("已停止自動刷新。");
    }
    function ensurePremarketAutoRefresh() {
      if (premarketTimer) return;
      premarketTimer = setInterval(() => runAction(() => fetchPremarket(false), "自動更新盤前分析..."), 180000);
    }
    function stopPremarket() {
      if (premarketTimer) clearInterval(premarketTimer);
      premarketTimer = null;
    }
    function showPage(pageId, navItem) {
      if (pageId === "signalsPage" || pageId === "rankingPage") pageId = "hubPage";
      if (pageId !== "realtimePage") stopRealtime(true);
      if (pageId !== "premarketPage") stopPremarket();
      activatePage(pageId);
      if (navItem) navItem.classList.add("active");
      if (pageId === "strategyPage") runAction(fetchStrategy, cloudWebMode ? "正在載入 AI 回測..." : "正在更新 AI 經理人決策...");
      if (pageId === "premarketPage") {
        runAction(() => fetchPremarket(true), "正在載入盤前分析...");
        ensurePremarketAutoRefresh();
      }
      if (pageId === "realtimePage") {
        runAction(fetchRealtime, "正在載入即時看盤...");
        ensureRealtimeAutoRefresh();
      }
      if (pageId === "watchPage") runAction(fetchWatch, "正在載入盤後看盤資料...");
      if (pageId === "signalsPage") runAction(fetchSignals, "正在載入訊號排行...");
      if (pageId === "rankingPage") runAction(fetchScan, "正在載入市場排行榜...");
      if (pageId === "hubPage") runAction(fetchHub, "正在載入股票探索...");
      if (pageId === "stockPage") runAction(fetchStock, "正在載入個股資料...");
    }
    function setupPanelControls() {
      document.querySelectorAll(".page section").forEach((section, index) => {
        const heading = section.querySelector(":scope > h2");
        const page = section.closest(".page");
        if (!heading || section.dataset.panelReady === "1") return;
        if (page && ["watchPage", "premarketPage", "realtimePage"].includes(page.id)) return;
        section.dataset.panelReady = "1";
        section.classList.add("info-panel", "panel-compact");
        const titleText = heading.textContent.trim();
        heading.textContent = "";
        const title = document.createElement("span");
        title.className = "section-title";
        title.textContent = titleText;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "panel-toggle";
        button.textContent = "+";
        button.title = "展開";
        button.setAttribute("aria-expanded", "false");
        button.addEventListener("click", event => {
          event.stopPropagation();
          togglePanel(section, button);
        });
        heading.addEventListener("click", () => togglePanel(section, button));
        heading.append(title, button);
      });
    }
    function togglePanel(section, button) {
      const expanded = !section.classList.contains("panel-expanded");
      section.classList.toggle("panel-expanded", expanded);
      section.classList.toggle("panel-compact", !expanded);
      button.textContent = expanded ? "收合" : "+";
      button.title = expanded ? "收合" : "展開";
      button.setAttribute("aria-expanded", expanded ? "true" : "false");
      if (expanded) {
        section.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }
    function collapsePanel(section, button) {
      section.classList.remove("panel-expanded");
      section.classList.add("panel-compact");
      if (button) {
        button.textContent = "+";
        button.title = "展開";
        button.setAttribute("aria-expanded", "false");
      }
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
    const hubCodeInput = document.getElementById("hubCode");
    if (hubCodeInput) {
      hubCodeInput.addEventListener("keydown", event => {
        if (event.key === "Enter") openHubStock();
      });
    }
    document.getElementById("stockPageCode").addEventListener("keydown", event => {
      if (event.key === "Enter") searchStockFromPanel();
    });
    setupPanelControls();
    initPublicConfig()
      .then(() => fetchStatus())
      .then(() => fetchStock())
      .then(() => setStatus(cloudWebMode ? "準備就緒。排行與 AI 回測會在切換頁面時載入。" : "準備就緒。排行與 AI 經理人會在切換頁面時載入。"))
      .catch(error => {
        setStatus(`錯誤：${error.message}`);
        alert(error.message);
      });
  </script>
</body>
</html>
"""
