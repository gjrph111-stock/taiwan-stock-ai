import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_DB_PATH
from .indicators import pct_change, rsi, sma, volume_ratio
from .names import short_name


_INDUSTRY_AVG_CACHE: dict[tuple[str, str], float | None] = {}


@dataclass
class MonitorContext:
    latest: dict
    previous: dict
    closes: list[float]
    volumes: list[float]
    ma5: float | None
    ma10: float | None
    ma20: float | None
    rsi14: float | None
    macd_hist: float | None
    premium: float | None
    volume_x: float | None


def build_ai_monitor(db_path: Path = DEFAULT_DB_PATH, limit: int | None = None) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_watchlist(conn)
        stocks = conn.execute(
            """
            SELECT w.code, s.name, s.market
            FROM watchlist w
            JOIN stocks s ON s.code = w.code
            ORDER BY w.created_at, w.code
            """
        ).fetchall()
        if limit:
            stocks = stocks[:limit]
        items = [analyze_stock(conn, stock) for stock in stocks]
    items = [item for item in items if item]
    return {
        "items": items,
        "summary": summarize_monitor(items),
    }


def analyze_stock(conn: sqlite3.Connection, stock: sqlite3.Row) -> dict | None:
    rows = conn.execute(
        """
        SELECT date, open, high, low, close, volume
        FROM prices
        WHERE stock_code = ? AND close IS NOT NULL
        ORDER BY date
        """,
        (stock["code"],),
    ).fetchall()
    if len(rows) < 60:
        return {
            "code": stock["code"],
            "name": short_name(stock["name"]),
            "action": "資料不足",
            "score": 0,
            "risk": 100,
            "summary": "至少需要 60 筆日線資料才能啟動 AI 盯盤。",
            "checks": [],
            "buy_zone": "資料不足",
            "sell_zone": "資料不足",
            "stop": "資料不足",
        }

    ctx = _context(rows)
    score = 50
    risk = 35
    checks = []

    premium_check = _premium_rule(ctx)
    score += premium_check["score"]
    risk += premium_check["risk"]
    checks.append(premium_check["text"])

    macd_check = _macd_rule(ctx)
    score += macd_check["score"]
    risk += macd_check["risk"]
    checks.append(macd_check["text"])

    volume_check = _volume_rule(rows, ctx)
    score += volume_check["score"]
    risk += volume_check["risk"]
    checks.append(volume_check["text"])

    pattern_check = _pattern_rule(rows, ctx)
    score += pattern_check["score"]
    risk += pattern_check["risk"]
    checks.extend(pattern_check["texts"])

    low_open_check = _low_open_rule(ctx)
    if low_open_check:
        score += low_open_check["score"]
        risk += low_open_check["risk"]
        checks.append(low_open_check["text"])

    trade = _trade_points(rows, ctx)
    facets = _analysis_facets(conn, stock, rows, ctx, checks, trade)
    action = _action_label(score, risk, ctx)
    score = max(0, min(100, round(score)))
    risk = max(0, min(100, round(risk)))
    return {
        "code": stock["code"],
        "name": short_name(stock["name"]),
        "date": ctx.latest["date"],
        "close": ctx.latest["close"],
        "open": ctx.latest["open"],
        "premium": ctx.premium,
        "score": score,
        "risk": risk,
        "action": action,
        "summary": _summary(ctx),
        "facets": facets,
        "checks": checks[:6],
        "buy_zone": trade["buy_zone"],
        "sell_zone": trade["sell_zone"],
        "stop": trade["stop"],
        "t_plan": _t_plan(ctx),
    }


def summarize_monitor(items: list[dict]) -> dict:
    if not items:
        return {"stance": "資料不足", "urgent": 0, "watch": 0, "positive": 0}
    urgent = sum(1 for item in items if item["action"] in ("立即風控", "減碼防守"))
    positive = sum(1 for item in items if item["action"] in ("偏多盯盤", "突破追蹤"))
    watch = len(items) - urgent - positive
    if urgent:
        stance = "風控優先"
    elif positive >= max(1, len(items) // 2):
        stance = "偏多盯盤"
    else:
        stance = "精選觀察"
    return {"stance": stance, "urgent": urgent, "watch": watch, "positive": positive}


def format_monitor_message(monitor: dict, limit: int | None = None) -> list[str]:
    items = monitor.get("items", [])
    if limit:
        items = items[:limit]
    summary = monitor.get("summary", {})
    lines = [
        "",
        "AI 盯盤",
        f"狀態：{summary.get('stance', '資料不足')}｜風控 {summary.get('urgent', 0)}｜偏多 {summary.get('positive', 0)}｜觀察 {summary.get('watch', 0)}",
    ]
    if not items:
        lines.append("目前沒有可盯盤的觀察名單。")
        return lines
    for item in items:
        premium = _fmt_pct(item.get("premium"))
        lines.extend(
            [
                "",
                f"{item['code']} {item['name']}｜{item['action']}｜AI {item['score']} / 風險 {item['risk']}",
                f"開盤溢價：{premium}｜{item['summary']}",
                _format_facets(item.get("facets", [])),
                f"買點：{item['buy_zone']}",
                f"賣點：{item['sell_zone']}",
                f"停損：{item['stop']}",
                f"做T：{item['t_plan']}",
            ]
        )
        for check in item.get("checks", [])[:3]:
            lines.append(f"- {check}")
    return lines


def _format_facets(facets: list[dict]) -> str:
    if not facets:
        return "分析面：資料不足"
    selected = []
    for name in ["技術面", "籌碼面", "消息面", "產業面", "三大法人", "風險面"]:
        facet = next((item for item in facets if item["name"] == name), None)
        if facet:
            selected.append(f"{facet['name']} {facet['stance']}")
    return "分析面：" + "｜".join(selected)


def _context(rows: list[sqlite3.Row]) -> MonitorContext:
    latest = dict(rows[-1])
    previous = dict(rows[-2])
    closes = [float(row["close"]) for row in rows if row["close"] is not None]
    volumes = [float(row["volume"] or 0) for row in rows]
    previous_close = float(previous["close"])
    today_open = latest.get("open")
    premium = None
    if today_open is not None and previous_close:
        premium = (float(today_open) - previous_close) / previous_close * 100
    macd_data = _macd_custom(closes, 10, 20, 7)
    return MonitorContext(
        latest=latest,
        previous=previous,
        closes=closes,
        volumes=volumes,
        ma5=sma(closes, 5),
        ma10=sma(closes, 10),
        ma20=sma(closes, 20),
        rsi14=rsi(closes, 14),
        macd_hist=macd_data["histogram"],
        premium=premium,
        volume_x=volume_ratio(volumes),
    )


def _premium_rule(ctx: MonitorContext) -> dict:
    p = ctx.premium
    if p is None:
        return {"score": 0, "risk": 5, "text": "開盤溢價率無資料，暫不判讀主力意圖。"}
    if p < 0:
        return {"score": -25, "risk": 30, "text": f"開盤溢價 {p:.2f}% 為負，依規則視為偏出貨，優先風控。"}
    if p < 1:
        return {"score": -8, "risk": 10, "text": f"開盤溢價 {p:.2f}% 偏低，需觀察是否有量能上攻。"}
    if p <= 3:
        return {"score": -2, "risk": 8, "text": f"開盤溢價 {p:.2f}% 屬弱勢溢價，半小時不放量就降低期待。"}
    if p <= 5:
        return {"score": 10, "risk": -3, "text": f"開盤溢價 {p:.2f}% 屬健康強勢，但衝高過快需分批保護。"}
    return {"score": 15, "risk": 8, "text": f"開盤溢價 {p:.2f}% 大於 5%，主力搶籌感強，但需守住 3% 溢價線。"}


def _macd_rule(ctx: MonitorContext) -> dict:
    close = float(ctx.latest["close"])
    if ctx.ma5 is None or ctx.ma20 is None or ctx.macd_hist is None:
        return {"score": 0, "risk": 5, "text": "MACD(10,20,7) 或均線資料不足。"}
    above_water = ctx.macd_hist > 0
    if close > ctx.ma5 and above_water:
        return {"score": 14, "risk": -8, "text": "站穩 5 日線且 MACD(10,20,7) 水上偏多，屬資金進場訊號。"}
    if close < ctx.ma20:
        return {"score": -18, "risk": 25, "text": "收盤跌破 20 日生命線，依規則轉為最後止損觀察。"}
    if not above_water:
        return {"score": -8, "risk": 12, "text": "MACD(10,20,7) 位於弱勢側，水下金叉也只視為反彈。"}
    return {"score": 4, "risk": 0, "text": "MACD 保持偏多但股價尚未明確站上攻擊線。"}


def _volume_rule(rows: list[sqlite3.Row], ctx: MonitorContext) -> dict:
    if len(ctx.volumes) < 6:
        return {"score": 0, "risk": 5, "text": "成交量資料不足。"}
    recent = ctx.volumes[-4:]
    high20 = max(float(row["high"]) for row in rows[-20:] if row["high"] is not None)
    latest_high = float(ctx.latest["high"])
    volx = ctx.volume_x or 0
    if recent[-1] < recent[-2] < recent[-3]:
        return {"score": 5, "risk": -4, "text": "縮量柱出現，若位於回調段可視為洗盤與拋壓降低。"}
    if recent[-1] > recent[-2] > recent[-3] and latest_high < high20:
        return {"score": -10, "risk": 16, "text": "梯量但未創新高，需防主力拉不動後回調。"}
    if volx >= 2 and ctx.latest["close"] >= ctx.latest["open"]:
        return {"score": 12, "risk": 2, "text": f"倍量陽線，量比 {volx:.2f}，具攻擊性但需確認位置。"}
    if volx >= 2 and ctx.latest["close"] < ctx.latest["open"]:
        return {"score": -15, "risk": 24, "text": f"倍量陰線，量比 {volx:.2f}，需防洗盤或出貨。"}
    return {"score": 0, "risk": 0, "text": f"量比 {volx:.2f}，量能未出現極端警訊。"}


def _pattern_rule(rows: list[sqlite3.Row], ctx: MonitorContext) -> dict:
    texts = []
    score = 0
    risk = 0
    latest = ctx.latest
    prev_close = float(ctx.previous["close"])
    close = float(latest["close"])
    open_ = float(latest["open"]) if latest["open"] is not None else close
    high = float(latest["high"]) if latest["high"] is not None else close
    low = float(latest["low"]) if latest["low"] is not None else close
    body = abs(close - open_) or 0.01
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    ret = (close - prev_close) / prev_close * 100 if prev_close else 0

    if ret >= 9.3:
        score += 12
        texts.append("最近出現漲停基因，符合主升浪候選條件之一。")
    if _had_limit_like(rows, 20):
        score += 8
        texts.append("20 日內曾有漲停或接近漲停，具短線資金記憶。")
    if _consecutive_bullish(rows) >= 4:
        score += 8
        texts.append("連續陽線達 4 根以上，具備啟動前的多方基因。")
    if open_ > prev_close * 1.01 and low > prev_close:
        score += 8
        texts.append("出現向上跳空缺口，多方意圖較明確。")
    if upper >= body * 1.5 and close < high * 0.97:
        score -= 14
        risk += 22
        texts.append("長上影線且收不住高點，需防高位試盤或出貨。")
    if lower >= body * 1.5 and close >= open_:
        score += 7
        risk -= 4
        texts.append("長下影線承接，空方力道有減弱跡象。")
    if high >= prev_close * 1.07 and close < high * 0.97:
        score -= 18
        risk += 24
        texts.append("盤中拉升超過 7% 後回落，屬看跌警訊。")
    if close < ctx.ma20 if ctx.ma20 is not None else False:
        score -= 10
        risk += 14
        texts.append("跌破 20 日線，七不買規則觸發。")
    return {"score": score, "risk": risk, "texts": texts or ["K 線形態未觸發極端多空訊號。"]}


def _low_open_rule(ctx: MonitorContext) -> dict | None:
    p = ctx.premium
    if p is None or p > -3:
        return None
    close = float(ctx.latest["close"])
    open_ = float(ctx.latest["open"])
    if -5 <= p <= -3 and close > ctx.previous["close"]:
        return {"score": 8, "risk": -5, "text": "低開 3-5% 但收盤翻紅，屬弱轉強觀察。"}
    if close < open_:
        return {"score": -20, "risk": 25, "text": "低開後未翻紅且跌破開盤價，依低開策略應果斷防守。"}
    return {"score": -4, "risk": 8, "text": "低開後仍需觀察量能與是否重新站回均線。"}


def _trade_points(rows: list[sqlite3.Row], ctx: MonitorContext) -> dict:
    recent = rows[-20:]
    lows = [float(row["low"]) for row in recent if row["low"] is not None]
    highs = [float(row["high"]) for row in recent if row["high"] is not None]
    close = float(ctx.latest["close"])
    if not lows or not highs:
        return {"buy_zone": "資料不足", "sell_zone": "資料不足", "stop": "資料不足"}
    support_candidates = [min(lows), ctx.ma5, ctx.ma10, ctx.ma20]
    support_values = [float(value) for value in support_candidates if value is not None and float(value) <= close]
    support = max(support_values) if support_values else min(lows)
    resistance_candidates = [max(highs), ctx.ma5, ctx.ma10, ctx.ma20]
    resistance_values = [float(value) for value in resistance_candidates if value is not None and float(value) >= close]
    resistance = min(resistance_values) if resistance_values else max(highs)
    buy_low = support
    buy_high = min(close, support * 1.03)
    sell_low = resistance
    sell_high = max(resistance, close * 1.08)
    stop = min(support * 0.97, ctx.ma20 * 0.99 if ctx.ma20 else support * 0.97)
    return {
        "buy_zone": f"{buy_low:.2f}-{buy_high:.2f}",
        "sell_zone": f"{sell_low:.2f}-{sell_high:.2f}",
        "stop": f"{stop:.2f}",
    }


def _analysis_facets(
    conn: sqlite3.Connection,
    stock: sqlite3.Row,
    rows: list[sqlite3.Row],
    ctx: MonitorContext,
    checks: list[str],
    trade: dict,
) -> list[dict]:
    return [
        _technical_facet(ctx),
        _chip_facet(rows, ctx),
        _news_facet(ctx, checks),
        _industry_facet(conn, stock, ctx),
        _institution_facet(ctx),
        _risk_facet(ctx, trade),
        _fund_flow_facet(rows, ctx),
    ]


def _technical_facet(ctx: MonitorContext) -> dict:
    close = float(ctx.latest["close"])
    if ctx.ma5 and ctx.ma20 and close > ctx.ma5 > ctx.ma20 and (ctx.macd_hist or 0) > 0:
        return _facet("技術面", "偏多", 82, "價格站上 5 日線與 20 日線，MACD(10,20,7) 偏多。")
    if ctx.ma20 and close < ctx.ma20:
        return _facet("技術面", "轉弱", 28, "收盤跌破 20 日生命線，先以風控為主。")
    if ctx.ma5 and close > ctx.ma5:
        return _facet("技術面", "整理偏多", 65, "股價仍在 5 日線上方，但趨勢強度需量能確認。")
    return _facet("技術面", "中性", 50, "均線與 MACD 尚未形成明確方向。")


def _chip_facet(rows: list[sqlite3.Row], ctx: MonitorContext) -> dict:
    recent = ctx.volumes[-5:]
    close = float(ctx.latest["close"])
    open_ = float(ctx.latest["open"]) if ctx.latest["open"] is not None else close
    volx = ctx.volume_x or 0
    if volx >= 2 and close >= open_:
        return _facet("籌碼面", "攻擊", 78, f"倍量陽線，量比 {volx:.2f}，籌碼有主動換手跡象。")
    if volx >= 2 and close < open_:
        return _facet("籌碼面", "出貨疑慮", 30, f"倍量陰線，量比 {volx:.2f}，需防主力出貨或洗盤。")
    if len(recent) >= 4 and recent[-1] < recent[-2] < recent[-3]:
        return _facet("籌碼面", "沉澱", 62, "縮量回調，拋壓暫時降低，但仍需等待攻擊量。")
    if len(recent) >= 4 and recent[-1] > recent[-2] > recent[-3]:
        return _facet("籌碼面", "放量", 58, "量能連續增加，若股價不創高需防梯量轉弱。")
    return _facet("籌碼面", "中性", 50, f"量比 {volx:.2f}，籌碼未出現極端變化。")


def _news_facet(ctx: MonitorContext, checks: list[str]) -> dict:
    p = ctx.premium
    event_hints = []
    if p is not None and abs(p) >= 3:
        event_hints.append(f"開盤溢價 {p:.2f}%")
    if any("跳空缺口" in text for text in checks):
        event_hints.append("跳空缺口")
    if any("倍量" in text for text in checks):
        event_hints.append("倍量異動")
    if event_hints:
        return _facet(
            "消息面",
            "需查證",
            55,
            "量價出現可能由消息帶動的異動：" + "、".join(event_hints) + "。尚未接入即時新聞源，需人工確認公告與新聞。",
        )
    return _facet("消息面", "待資料源", 50, "尚未接入即時新聞與重大訊息資料源，目前以量價異常作為消息反應代理。")


def _industry_facet(conn: sqlite3.Connection, stock: sqlite3.Row, ctx: MonitorContext) -> dict:
    industry = stock["market"]
    row = conn.execute(
        """
        SELECT industry
        FROM stocks
        WHERE code = ?
        """,
        (stock["code"],),
    ).fetchone()
    if row and row["industry"]:
        industry = row["industry"]
    peers = conn.execute(
        """
        SELECT p.stock_code, p.close
        FROM prices p
        JOIN stocks s ON s.code = p.stock_code
        WHERE s.industry IS ? AND p.date = ?
        """,
        (row["industry"] if row else None, ctx.latest["date"]),
    ).fetchall()
    stock_ret20 = pct_change(ctx.closes, 20)
    if not peers or len(peers) < 5:
        return _facet("產業面", "資料有限", 50, f"{industry or '產業'} 同業資料不足，暫以個股趨勢為主。")
    market_ret = _industry_average_return(conn, row["industry"], ctx.latest["date"])
    if market_ret is None or stock_ret20 is None:
        return _facet("產業面", "資料有限", 50, f"{industry} 產業動能資料不足。")
    diff = stock_ret20 - market_ret
    if diff >= 10:
        return _facet("產業面", "強於同業", 72, f"{industry} 內相對強勢，20 日報酬高於同業均值 {diff:.2f}%。")
    if diff <= -10:
        return _facet("產業面", "落後同業", 35, f"{industry} 內相對落後，20 日報酬低於同業均值 {abs(diff):.2f}%。")
    return _facet("產業面", "同步", 55, f"{industry} 表現接近同業，暫無明顯產業超額動能。")


def _institution_facet(ctx: MonitorContext) -> dict:
    close = float(ctx.latest["close"])
    open_ = float(ctx.latest["open"]) if ctx.latest["open"] is not None else close
    volx = ctx.volume_x or 0
    if volx >= 1.5 and close > open_ and ctx.premium and ctx.premium > 1:
        return _facet("三大法人", "疑似偏買", 58, "尚未接入三大法人買賣超；以放量紅 K 與正溢價暫判可能有法人或主力承接。")
    if volx >= 1.5 and close < open_:
        return _facet("三大法人", "疑似偏賣", 42, "尚未接入三大法人買賣超；放量黑 K 暫列賣壓警示。")
    return _facet("三大法人", "待資料源", 50, "尚未接入外資、投信、自營商買賣超資料，後續可接 FinMind/證交所法人資料。")


def _risk_facet(ctx: MonitorContext, trade: dict) -> dict:
    close = float(ctx.latest["close"])
    if ctx.ma20 and close < ctx.ma20:
        return _facet("風險面", "高", 25, f"跌破 20 日生命線，停損參考 {trade['stop']}。")
    if ctx.rsi14 and ctx.rsi14 >= 80:
        return _facet("風險面", "過熱", 35, "RSI 高於 80，需防獲利回吐，賣飛也要接受。")
    if ctx.rsi14 and ctx.rsi14 >= 70:
        return _facet("風險面", "偏熱", 48, "RSI 高於 70，追價需降低單筆部位。")
    return _facet("風險面", "可控", 65, f"尚未觸發高風險條件，停損參考 {trade['stop']}。")


def _fund_flow_facet(rows: list[sqlite3.Row], ctx: MonitorContext) -> dict:
    close = float(ctx.latest["close"])
    prev = float(ctx.previous["close"])
    volx = ctx.volume_x or 0
    if close > prev and volx >= 1.2:
        return _facet("資金面", "流入", 68, f"價漲量增，量比 {volx:.2f}，短線資金偏流入。")
    if close < prev and volx >= 1.2:
        return _facet("資金面", "流出", 38, f"價跌量增，量比 {volx:.2f}，短線資金偏流出。")
    if close > prev and volx < 0.8:
        return _facet("資金面", "惜售", 58, f"價漲但縮量，量比 {volx:.2f}，籌碼可能偏鎖定。")
    return _facet("資金面", "中性", 50, f"量價未見明顯資金方向，量比 {volx:.2f}。")


def _industry_average_return(conn: sqlite3.Connection, industry: str | None, latest_date: str) -> float | None:
    if not industry:
        return None
    cache_key = (industry, latest_date)
    if cache_key in _INDUSTRY_AVG_CACHE:
        return _INDUSTRY_AVG_CACHE[cache_key]
    rows = conn.execute(
        """
        SELECT p.stock_code, p.date, p.close
        FROM prices p
        JOIN stocks s ON s.code = p.stock_code
        WHERE s.industry = ?
          AND p.date <= ?
        ORDER BY p.stock_code, p.date
        """,
        (industry, latest_date),
    ).fetchall()
    by_code: dict[str, list[float]] = {}
    for row in rows:
        by_code.setdefault(row["stock_code"], []).append(float(row["close"]))
    returns = [pct_change(values, 20) for values in by_code.values() if len(values) >= 21]
    returns = [value for value in returns if value is not None]
    result = sum(returns) / len(returns) if returns else None
    _INDUSTRY_AVG_CACHE[cache_key] = result
    return result


def _facet(name: str, stance: str, score: int, detail: str) -> dict:
    return {"name": name, "stance": stance, "score": score, "detail": detail}


def _t_plan(ctx: MonitorContext) -> str:
    close = float(ctx.latest["close"])
    if ctx.ma5 and close > ctx.ma5 and ctx.rsi14 and ctx.rsi14 < 70:
        return "保留底倉，回落 5 日線附近不破可低吸；拉開均價線乖離後再分批高拋。"
    if ctx.rsi14 and ctx.rsi14 >= 75:
        return "短線過熱，做 T 以高拋降成本為主，避免追高補倉。"
    if ctx.ma20 and close < ctx.ma20:
        return "跌破 20 日生命線，不做逆勢 T，先執行風控。"
    return "區間震盪，僅小部位做 T，避免頻繁交易磨損成本。"


def _action_label(score: float, risk: float, ctx: MonitorContext) -> str:
    close = float(ctx.latest["close"])
    if ctx.ma20 and close < ctx.ma20:
        return "立即風控"
    if risk >= 70:
        return "減碼防守"
    if score >= 72 and risk <= 45:
        return "偏多盯盤"
    if score >= 62:
        return "突破追蹤"
    return "觀察等待"


def _summary(ctx: MonitorContext) -> str:
    ret20 = pct_change(ctx.closes, 20)
    return (
        f"收 {float(ctx.latest['close']):.2f} / "
        f"MA5 {ctx.ma5:.2f} / MA20 {ctx.ma20:.2f} / "
        f"RSI {ctx.rsi14:.1f} / 20D {ret20:.2f}%"
    )


def _macd_custom(values: list[float], fast: int, slow: int, signal: int) -> dict:
    ema_fast = _ema(values, fast)
    ema_slow = _ema(values, slow)
    line = [a - b for a, b in zip(ema_fast, ema_slow)]
    signal_line = _ema(line, signal)
    return {
        "macd": line[-1],
        "signal": signal_line[-1],
        "histogram": line[-1] - signal_line[-1],
    }


def _ema(values: list[float], window: int) -> list[float]:
    multiplier = 2 / (window + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append((value - out[-1]) * multiplier + out[-1])
    return out


def _had_limit_like(rows: list[sqlite3.Row], days: int) -> bool:
    recent = rows[-days:]
    for previous, current in zip(recent, recent[1:]):
        if previous["close"] and current["close"] and current["close"] >= previous["close"] * 1.093:
            return True
    return False


def _consecutive_bullish(rows: list[sqlite3.Row]) -> int:
    count = 0
    for row in reversed(rows):
        if row["open"] is not None and row["close"] is not None and row["close"] > row["open"]:
            count += 1
        else:
            break
    return count


def _fmt_pct(value) -> str:
    return "資料不足" if value is None else f"{value:.2f}%"


def _ensure_watchlist(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            code TEXT PRIMARY KEY,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')) ,
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
