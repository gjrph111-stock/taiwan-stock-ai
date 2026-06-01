import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .config import DEFAULT_DB_PATH
from .indicators import macd, pct_change, rsi, sma, volume_ratio
from .names import short_name
from .signals import rank_signals, risk_adjusted_score, score_stock
from .web import _fetch_yahoo_snapshot, api_scan, api_status, api_strategy, api_watch, split_telegram_message


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "notify.json"


def build_after_hours_message(db_path: Path = DEFAULT_DB_PATH, limit: int = 6) -> str:
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    status = api_status(db_path)
    scan = api_scan(db_path, limit=max(limit, 8))
    watch = api_watch(db_path)
    industry = watch.get("industry_after_report") or {}
    top_groups = industry.get("top_groups") or []
    volume_groups = industry.get("volume_focus") or []
    weak_groups = industry.get("weak_groups") or []
    top_return = scan.get("top_return_20d") or []
    top_volume = scan.get("top_volume_expansion") or []
    watch_items = build_watchlist_advice(db_path, limit=limit)
    twii = _safe_yahoo_snapshot("^TWII")
    otc = _safe_yahoo_snapshot("^TWOII")
    market_tone = _after_hours_market_tone(twii, scan, top_groups)
    lines = [
        f"{now.year}年{now.month}月{now.day}日（{_weekday_short(now)}）台股收盤訊息",
        "",
        "【主要指數表現】",
        "",
        market_tone,
        "",
        _format_index_line("加權指數", twii, fallback_date=status.get("last_date")),
        _format_index_line("櫃買指數", otc),
        "",
        _after_hours_breadth_text(scan),
        "",
        "【熱門個股與族群表現細節】",
        "",
        *_after_hours_hot_stock_lines(watch_items, top_return, top_volume, limit=limit),
        "",
        f"強勢族群：{_format_group_list(top_groups)}",
        f"量能族群：{_format_group_list(volume_groups)}",
        f"弱勢警戒：{_format_group_list(weak_groups)}",
        "",
        "【市場主要驅動因素】",
        "",
        f"1. {_after_hours_driver_ai(top_groups, top_return)}",
        "",
        f"2. {_after_hours_driver_flow(scan, top_volume)}",
        "",
        f"3. {_after_hours_driver_structure(scan, weak_groups)}",
        "",
        "【總結與後市展望】",
        "",
        _after_hours_outlook(twii, scan, top_groups),
        "",
        "目前市場焦點仍在：",
        "1. AI供應鏈、半導體、記憶體、散熱、光通訊等主流族群是否延續量價。",
        "2. 外資與法人資金是否續抱權值股與高成交族群。",
        "3. 指數高檔震盪時，獲利了結賣壓與關鍵均線支撐是否守住。",
        "",
        "提醒：盤後訊息是收盤後研究摘要，不是直接下單指令；隔日仍需搭配盤前早訊、開盤量價與停損紀律。",
    ]
    return "\n".join(lines)


def build_daily_message(db_path: Path = DEFAULT_DB_PATH, limit: int = 5) -> str:
    status = api_status(db_path)
    scan = api_scan(db_path, limit)
    strategy = api_strategy(db_path).get("market_context", {})
    watch_items = build_watchlist_advice(db_path, limit=limit)

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


def _weekday_short(value: datetime) -> str:
    return ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][value.weekday()]


def _safe_yahoo_snapshot(symbol: str) -> dict | None:
    try:
        return _fetch_yahoo_snapshot(symbol)
    except Exception:
        return None


def _format_index_line(name: str, snapshot: dict | None, fallback_date: str | None = None) -> str:
    if not snapshot:
        suffix = f"資料日期 {fallback_date}，" if fallback_date else ""
        return f"{name}：{suffix}免費指數資料暫缺，請以交易所收盤資料補驗。"
    change = _num(snapshot.get("change"))
    pct = _num(snapshot.get("change_percent"))
    price = _num(snapshot.get("price"))
    direction = "上漲" if change and change > 0 else "下跌" if change and change < 0 else "持平"
    return f"{name}：{direction}{abs(change or 0):,.2f} 點（{pct:+.2f}%），收在 {price:,.2f} 點。"


def _after_hours_market_tone(twii: dict | None, scan: dict, groups: list[dict]) -> str:
    pct = _num((twii or {}).get("change_percent"))
    summary = scan.get("summary") or {}
    count = max(1, int(scan.get("count") or 1))
    above20_pct = (summary.get("above_sma20") or 0) / count * 100
    leader = groups[0].get("name") if groups else "主流族群"
    if pct is not None and pct >= 1:
        return f"台股今日收盤展現強勢多頭氣勢，指數收紅並由 {leader} 領軍，市場風險偏好維持高檔。"
    if pct is not None and pct <= -1:
        return f"台股今日收盤承壓，指數明顯回落，盤面轉為風控優先，需觀察 {leader} 是否仍有承接。"
    if above20_pct >= 55:
        return f"台股今日呈現類股輪動格局，雖指數震盪，站上20日線家數仍維持相對健康，{leader} 是盤面焦點。"
    return f"台股今日收盤偏整理，市場資金集中在少數題材與量能族群，{leader} 仍是隔日觀察重點。"


def _after_hours_breadth_text(scan: dict) -> str:
    summary = scan.get("summary") or {}
    count = scan.get("count") or 0
    return (
        f"整體市場廣度：{summary.get('above_sma20', 0)} / {count} 檔站上20日線，"
        f"{summary.get('above_sma60', 0)} 檔站上60日線，{summary.get('new_high_60', 0)} 檔創60日新高。"
    )


def _after_hours_hot_stock_lines(watch_items: list[dict], top_return: list[dict], top_volume: list[dict], limit: int = 6) -> list[str]:
    lines = []
    used = set()
    for item in watch_items[: min(limit, 5)]:
        used.add(item.get("code"))
        score = f"AI {item.get('score')}" if item.get("score") is not None else item.get("trend", "觀察")
        lines.append(f"{item.get('name')}（{item.get('code')}）：{score}，{item.get('summary', '')}。{item.get('advice', '')}")
    for row in top_return[:3]:
        if row.get("code") in used:
            continue
        used.add(row.get("code"))
        lines.append(
            f"{row.get('short_name') or row.get('name')}（{row.get('code')}）：20日漲幅 {float(row.get('return_20d') or 0):.2f}%，屬盤後強勢追蹤名單。"
        )
    if top_volume:
        names = "、".join(f"{row.get('short_name') or row.get('name')} 量比 {float(row.get('volume_ratio') or 0):.2f}" for row in top_volume[:4])
        lines.append(f"量能焦點：{names}，隔日需觀察是否延續成交量。")
    return lines or ["今日沒有足夠個股資料可列入熱門名單，先觀察大盤與類股輪動。"]


def _format_group_list(groups: list[dict]) -> str:
    if not groups:
        return "暫無明確族群。"
    return "、".join(
        f"{row.get('name')}（均漲跌 {float(row.get('avg_change_percent') or 0):.2f}%）"
        for row in groups[:5]
    )


def _after_hours_driver_ai(groups: list[dict], top_return: list[dict]) -> str:
    group = groups[0].get("name") if groups else "AI與半導體相關族群"
    stock = (top_return[0].get("short_name") or top_return[0].get("name")) if top_return else "強勢股"
    return f"AI與主流題材延續：{group} 表現較強，{stock} 等強勢股帶動市場人氣。"


def _after_hours_driver_flow(scan: dict, top_volume: list[dict]) -> str:
    summary = scan.get("summary") or {}
    volume_text = "、".join(f"{row.get('short_name') or row.get('name')}" for row in top_volume[:3]) or "高量能股"
    return f"資金集中度提高：60日新高 {summary.get('new_high_60', 0)} 檔，量能集中在 {volume_text}，顯示資金仍在尋找主流。"


def _after_hours_driver_structure(scan: dict, weak_groups: list[dict]) -> str:
    summary = scan.get("summary") or {}
    weak_text = _format_group_list(weak_groups[:2])
    return f"多頭結構與風險並存：站上月線 {summary.get('above_sma20', 0)} 檔，但弱勢族群仍有 {weak_text}，高檔需防獲利了結。"


def _after_hours_outlook(twii: dict | None, scan: dict, groups: list[dict]) -> str:
    pct = _num((twii or {}).get("change_percent"))
    group_text = _format_group_list(groups[:2])
    if pct is not None and pct >= 1:
        headline = "今天台股可用「主流族群領漲、量價偏強、風險偏好升溫」來形容。"
    elif pct is not None and pct <= -1:
        headline = "今天台股可用「賣壓升溫、資金防守、等待止穩」來形容。"
    else:
        headline = "今天台股可用「指數震盪、族群輪動、資金挑股」來形容。"
    return f"{headline} 後市仍以 {group_text} 為主軸，若隔日量能延續，多頭結構可維持；若開盤無量或急拉不過高，需留意短線獲利了結。"


def _num(value) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num == num else None


def build_watchlist_advice(db_path: Path = DEFAULT_DB_PATH, limit: int | None = None) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_watchlist(conn)
        rows = conn.execute(
            """
            SELECT w.code, s.name, s.market
            FROM watchlist w
            JOIN stocks s ON s.code = w.code
            ORDER BY COALESCE(w.sort_order, 999999), w.created_at, w.code
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
    existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(watchlist)").fetchall()}
    if "sort_order" not in existing_columns:
        conn.execute("ALTER TABLE watchlist ADD COLUMN sort_order INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notify_watchlist_order ON watchlist(sort_order, created_at)")
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
    bot_token = os.environ.get("STOCK_V1_TELEGRAM_BOT_TOKEN", "").strip() or telegram.get("bot_token")
    chat_id = os.environ.get("STOCK_V1_TELEGRAM_CHAT_ID", "").strip() or telegram.get("chat_id")
    if not bot_token or not chat_id or "PASTE_" in bot_token or "PASTE_" in chat_id:
        raise ValueError("Telegram bot_token/chat_id is not configured in config/notify.json or environment variables.")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    results = []
    for part in split_telegram_message(message):
        payload = {
            "chat_id": chat_id,
            "text": part[:3900],
            "disable_web_page_preview": True,
        }
        results.append(_post_json(url, payload))
    return {"sent": len(results), "results": results}


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
