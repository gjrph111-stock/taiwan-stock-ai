import sqlite3
from pathlib import Path
from statistics import mean

from .config import DEFAULT_DB_PATH
from .indicators import pct_change, rsi, sma, volume_ratio
from .industry import industry_profile
from .names import short_name
from .signals import risk_adjusted_score, score_stock


def build_fundamental_analysis(db_path: Path = DEFAULT_DB_PATH, code: str = "2330") -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        stock = conn.execute("SELECT * FROM stocks WHERE code = ?", (code.strip(),)).fetchone()
        if not stock:
            return {"error": f"找不到股票代號 {code}。"}
        rows = conn.execute(
            """
            SELECT date, open, high, low, close, volume
            FROM prices
            WHERE stock_code = ? AND close IS NOT NULL
            ORDER BY date
            """,
            (stock["code"],),
        ).fetchall()
        industry_stats = _industry_context(conn, stock, rows)
        industry = industry_profile(stock["code"], stock["name"], stock["industry"])

    if len(rows) < 80:
        return {
            "code": stock["code"],
            "name": short_name(stock["name"]),
            "verdict": "資料不足",
            "sections": [{"title": "資料狀態", "stance": "不足", "points": ["至少需要 80 筆價格資料才能建立基本面研究框架。"]}],
        }

    closes = [float(row["close"]) for row in rows]
    volumes = [float(row["volume"] or 0) for row in rows]
    latest = dict(rows[-1])
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    ret20 = pct_change(closes, 20)
    ret60 = pct_change(closes, 60)
    rsi14 = rsi(closes, 14)
    volx = volume_ratio(volumes)
    signal = score_stock(stock, rows)
    risk_score = risk_adjusted_score(signal) if signal else None
    verdict = _verdict(closes[-1], ma20, ma60, ret20, rsi14, risk_score)
    trend_stats = _trend_stats(rows)
    market_profile = _market_profile(rows, closes, volumes, industry_stats, ret20, ret60, rsi14, volx)
    radar = _radar_metrics(closes, volumes, industry_stats, ret20, ret60, rsi14, volx, risk_score)
    peer_cards = _peer_cards(industry_stats)
    summary = _research_summary(verdict, latest["close"], ma20, ma60, ret20, ret60, rsi14, volx, risk_score, radar, trend_stats, market_profile)

    return {
        "code": stock["code"],
        "name": short_name(stock["name"]),
        "full_name": stock["name"],
        "industry": industry["category"],
        "industry_raw": industry["raw"],
        "industry_profile": industry,
        "date": latest["date"],
        "close": latest["close"],
        "verdict": verdict,
        "radar": radar,
        "summary": summary,
        "market_profile": market_profile,
        "trend_series": _trend_series(rows),
        "peer_cards": peer_cards,
        "yearly_stats": trend_stats["yearly"],
        "sections": [
            _business_model_section(stock, market_profile, industry),
            _moat_section(stock, industry_stats, risk_score),
            _industry_section(stock, industry_stats, ret20, ret60, industry),
            _price_quality_section(trend_stats, radar),
            _peer_section(industry_stats),
            _financial_health_section(market_profile, trend_stats),
            _valuation_section(stock, industry_stats, market_profile),
            _chip_section(market_profile),
            _news_proxy_section(market_profile),
            _scenario_section(verdict, closes[-1], ma20, ma60, ret20, rsi14, volx),
            _growth_section(stock, industry_stats, ret20, ret60),
            _decision_section(verdict, risk_score),
        ],
    }


def _business_model_section(stock: sqlite3.Row, market_profile: dict, industry_profile_data: dict) -> dict:
    industry = industry_profile_data["category"]
    return {
        "title": "商業模式與收入來源",
        "stance": "產業代理",
        "points": [
            f"{short_name(stock['name'])} 屬於 {industry}，先以同產業強弱、成交活躍度與市場定價趨勢作為商業模式代理。",
            f"目前流動性評級為 {market_profile['liquidity_label']}，代表市場對該股的交易關注度。",
            "若後續接入產品線營收與客戶結構，這裡會進一步拆解收入來源與成長品質。",
        ],
    }


def _moat_section(stock: sqlite3.Row, industry_stats: dict, risk_score) -> dict:
    score = 5
    points = ["尚未接入品牌、專利、客戶集中度與市占率資料，護城河先用相對強弱代理。"]
    if risk_score is not None and risk_score >= 75:
        score += 2
        points.append("AI 訊號分數高，表示市場近期願意給予相對強勢定價。")
    if industry_stats.get("relative_strength") == "strong":
        score += 1
        points.append("近 20 日表現優於同產業平均，可暫列為相對競爭力偏強。")
    if industry_stats.get("peer_count", 0) < 5:
        points.append("同業樣本不足，護城河評分需保守看待。")
    score = max(1, min(10, score))
    return {
        "title": "競爭護城河",
        "stance": f"{score}/10",
        "points": points + [
            "待接資料：品牌影響力、轉換成本、成本優勢、專利技術、市占率與主要競爭對手財務比較。",
        ],
    }


def _industry_section(stock: sqlite3.Row, industry_stats: dict, ret20, ret60, industry_profile_data: dict) -> dict:
    industry = industry_profile_data["category"]
    chain = " > ".join(industry_profile_data.get("chain", [])) or industry
    points = [
        f"產業分類：{industry}。",
        f"產業鏈位置：{chain}。",
        f"個股 20 日報酬 {ret20:.2f}%、60 日報酬 {ret60:.2f}%。" if ret20 is not None and ret60 is not None else "報酬資料不足。",
    ]
    if industry_stats.get("peer_count", 0) >= 5:
        points.append(f"同業 20 日平均約 {industry_stats['peer_return_20d']:.2f}%，個股相對強弱：{industry_stats['relative_strength_label']}。")
    else:
        points.append("同業樣本不足，暫以個股趨勢判斷產業位置。")
    points.append("待接資料：產業市場規模、成長率、景氣循環、AI/技術升級與供需變化。")
    return {"title": "產業趨勢", "stance": industry_stats.get("relative_strength_label", "資料有限"), "points": points}


def _financial_health_section(market_profile: dict, trend_stats: dict) -> dict:
    yearly = trend_stats["yearly"]
    positive_years = sum(1 for item in yearly if item["return"] > 0)
    stance = "市場代理偏穩" if market_profile["stability_score"] >= 65 and positive_years >= max(1, len(yearly) // 2) else "需觀察"
    return {
        "title": "五年財務健康",
        "stance": stance,
        "points": [
            f"近年價格年化表現中，正報酬年度 {positive_years}/{len(yearly) or 1}，作為市場對財務品質的代理訊號。",
            f"穩定度分數 {market_profile['stability_score']:.1f}，近一年最大回撤 {trend_stats['max_drawdown']:.2f}%。",
            "真實財報欄位尚未串接，但系統已先用市場代理判斷體質是否被資金持續認同。",
        ],
    }


def _price_quality_section(trend_stats: dict, radar: list[dict]) -> dict:
    yearly = trend_stats["yearly"]
    latest_year = yearly[-1] if yearly else None
    max_drawdown = trend_stats["max_drawdown"]
    stance = "資料有限"
    if latest_year:
        stance = "趨勢健康" if latest_year["return"] >= 0 and max_drawdown > -25 else "波動偏高"
    points = [
        f"近一年最大回撤約 {max_drawdown:.2f}%，用來衡量追高後可能承受的價格壓力。",
        "雷達圖已把趨勢、動能、量能、同業、風控與穩定度轉成 0-100 分，方便快速判斷強弱。",
    ]
    if latest_year:
        points.append(f"{latest_year['year']} 年至目前報酬約 {latest_year['return']:.2f}%，高低區間 {latest_year['low']:.2f}-{latest_year['high']:.2f}。")
    strongest = max(radar, key=lambda item: item["score"]) if radar else None
    weakest = min(radar, key=lambda item: item["score"]) if radar else None
    if strongest and weakest:
        points.append(f"目前最強構面是 {strongest['label']}，最需要追蹤的是 {weakest['label']}。")
    return {"title": "價格品質與波動", "stance": stance, "points": points}


def _peer_section(industry_stats: dict) -> dict:
    peers = industry_stats.get("peer_leaders", [])
    points = []
    if peers:
        points.append(f"同產業樣本 {industry_stats.get('peer_count', 0)} 檔，20 日平均約 {industry_stats.get('peer_return_20d', 0):.2f}%。")
        points.append("同業前段班：" + "、".join(f"{item['code']} {item['name']} {item['return_20d']:.1f}%" for item in peers[:3]))
        points.append("若個股落後同業但產業走強，可列為補漲觀察；若個股強於同業但量能退潮，需防範短線過熱。")
    else:
        points.append("同產業可比資料不足，暫時無法建立同業強弱排行。")
    return {"title": "同業比較", "stance": industry_stats.get("relative_strength_label", "資料有限"), "points": points}


def _valuation_section(stock: sqlite3.Row, industry_stats: dict, market_profile: dict) -> dict:
    heat = market_profile["valuation_heat"]
    if heat >= 75:
        stance = "估值熱度偏高"
    elif heat <= 35:
        stance = "估值熱度偏低"
    else:
        stance = "估值熱度中性"
    return {
        "title": "估值分析",
        "stance": stance,
        "points": [
            f"估值熱度 {heat:.1f}/100，依近一年價格位置、同業強弱與動能溫度推估。",
            f"目前價格位於近一年區間約 {market_profile['range_position']:.1f}% 位置，越接近高檔越需要確認基本面支撐。",
            "尚未使用 EPS/PE/PB/DCF 真實估值，因此結論定位為估值熱度，而不是正式目標價。",
        ],
    }


def _chip_section(market_profile: dict) -> dict:
    return {
        "title": "籌碼與資金代理",
        "stance": market_profile["chip_label"],
        "points": [
            f"近 20 日上漲量占比 {market_profile['up_volume_ratio']:.1f}%，用來觀察資金是否偏向上攻日。",
            f"OBV 斜率代理為 {market_profile['obv_slope_label']}，量能分數 {market_profile['volume_score']:.1f}。",
            "後續接入三大法人買賣超後，此構面會由代理資料升級為真實法人籌碼。",
        ],
    }


def _news_proxy_section(market_profile: dict) -> dict:
    events = market_profile["event_flags"]
    return {
        "title": "消息事件偵測",
        "stance": "有事件" if events else "平穩",
        "points": events or [
            "近 20 日未偵測到明顯跳空、爆量長黑或異常波動。",
            "目前消息面以價格與量能異常作為代理，尚未串接新聞文字來源。",
            "接入新聞 API 後，可進一步做利多/利空分類與事件時間軸。",
        ],
    }


def _scenario_section(verdict: str, close: float, ma20, ma60, ret20, rsi14, volx) -> dict:
    bull = f"多頭：站穩 20 日線 {ma20:.2f} 並放量上攻，可能延續趨勢。" if ma20 else "多頭：等待均線資料完整。"
    bear = f"空頭：跌破 20 日線或 RSI {rsi14:.1f} 過熱後轉弱，需優先風控。" if rsi14 is not None else "空頭：等待 RSI 資料完整。"
    base = f"基本情境：目前收盤 {close:.2f}，20 日報酬 {ret20:.2f}%，量比 {volx:.2f}，結論偏 {verdict}。" if ret20 is not None and volx is not None else f"基本情境：結論偏 {verdict}。"
    return {"title": "多空與基本情境", "stance": verdict, "points": [bull, bear, base]}


def _growth_section(stock: sqlite3.Row, industry_stats: dict, ret20, ret60) -> dict:
    stance = "需追蹤"
    if ret20 is not None and ret60 is not None and ret20 > 10 and ret60 > 20:
        stance = "市場預期升溫"
    return {
        "title": "未來 12-24 個月與 5-10 年成長",
        "stance": stance,
        "points": [
            "短中期先看產業循環、產品價格、訂單能見度與市場資金是否持續認同。",
            "長期成長需接入市場規模、產業成長率、新產品、AI/技術優勢與海外擴張資料。",
            "目前先以 20/60 日趨勢作為市場預期是否升溫的代理訊號。",
        ],
    }


def _decision_section(verdict: str, risk_score) -> dict:
    if verdict in ("偏多", "強勢") and risk_score is not None and risk_score >= 70:
        decision = "可研究買入"
    elif verdict == "轉弱":
        decision = "避免或減碼"
    else:
        decision = "持有/觀察"
    return {
        "title": "是否應該投資",
        "stance": decision,
        "points": [
            f"短期：依目前趨勢與風險分數，結論為 {decision}。",
            "長期：需等待五年財報、估值、護城河與產業資料源補齊後再提高信心。",
            "關鍵催化：營收成長、產業景氣、法人買超、技術面突破與消息面利多。",
            "主要風險：估值過高、財報轉弱、跌破 20 日線、量增不漲與消息面反轉。",
        ],
    }


def _research_summary(verdict: str, close, ma20, ma60, ret20, ret60, rsi14, volx, risk_score, radar: list[dict], trend_stats: dict, market_profile: dict) -> dict:
    radar_avg = mean([item["score"] for item in radar]) if radar else 0
    overall = round(radar_avg, 1)
    strongest = max(radar, key=lambda item: item["score"]) if radar else {"label": "無資料", "score": 0}
    weakest = min(radar, key=lambda item: item["score"]) if radar else {"label": "無資料", "score": 0}
    action = "觀察"
    if verdict in ("強勢", "偏多") and overall >= 70:
        action = "偏多研究"
    elif verdict == "轉弱" or overall < 45:
        action = "降低部位"
    elif verdict == "偏熱":
        action = "等回檔"
    risk_flags = []
    if ma20 is not None and close < ma20:
        risk_flags.append("收盤跌破 20 日線")
    if rsi14 is not None and rsi14 >= 72:
        risk_flags.append("RSI 偏熱")
    if volx is not None and volx < 0.75:
        risk_flags.append("量能低於近期均量")
    if trend_stats["max_drawdown"] <= -25:
        risk_flags.append("近一年回撤較深")
    if not risk_flags:
        risk_flags.append("尚未出現明顯技術風險，仍需追蹤量價變化")
    positives = []
    if ma20 is not None and close > ma20:
        positives.append("站上 20 日線")
    if ma60 is not None and close > ma60:
        positives.append("站上 60 日線")
    if ret20 is not None and ret20 > 0:
        positives.append(f"20 日報酬 {ret20:.2f}%")
    if ret60 is not None and ret60 > 0:
        positives.append(f"60 日報酬 {ret60:.2f}%")
    if not positives:
        positives.append("等待趨勢重新轉強")
    data_quality = 78
    data_gaps = ["EPS/PE 真值", "月營收", "法人買賣超", "新聞文字情緒"]
    return {
        "overall_score": overall,
        "action": action,
        "verdict": verdict,
        "strongest": strongest,
        "weakest": weakest,
        "data_quality": data_quality,
        "data_gaps": data_gaps,
        "positives": positives[:4],
        "risk_flags": risk_flags[:4],
        "checkpoints": [
            "收盤是否守住 20 日線",
            "量比是否回到 1 以上",
            "是否強於同產業平均",
            f"估值熱度是否維持在 {market_profile['valuation_heat']:.0f} 分以下",
        ],
    }


def _market_profile(rows: list[sqlite3.Row], closes, volumes, industry_stats, ret20, ret60, rsi14, volx) -> dict:
    recent = rows[-20:] if len(rows) >= 20 else rows
    high_252 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    low_252 = min(closes[-252:]) if len(closes) >= 252 else min(closes)
    close = closes[-1]
    range_position = (close - low_252) / (high_252 - low_252) * 100 if high_252 != low_252 else 50
    avg_volume_20 = mean([float(row["volume"] or 0) for row in recent]) if recent else 0
    liquidity_label = "高" if avg_volume_20 >= 10_000_000 else "中" if avg_volume_20 >= 1_000_000 else "低"
    up_volume = 0.0
    total_volume = 0.0
    obv = 0.0
    obv_points = []
    for prev, row in zip(rows[-61:-1], rows[-60:]):
        volume = float(row["volume"] or 0)
        total_volume += volume
        if float(row["close"]) >= float(prev["close"]):
            up_volume += volume
            obv += volume
        else:
            obv -= volume
        obv_points.append(obv)
    up_volume_ratio = up_volume / total_volume * 100 if total_volume else 50
    obv_slope = obv_points[-1] - obv_points[0] if len(obv_points) >= 2 else 0
    obv_slope_label = "上升" if obv_slope > 0 else "下降" if obv_slope < 0 else "持平"
    volume_score = 50 + (_clamp((volx or 1) - 1, -0.6, 1.2) / 1.2) * 35
    stability_score = 55
    if len(closes) >= 60:
        changes = [abs(closes[i] / closes[i - 1] - 1) for i in range(len(closes) - 59, len(closes)) if closes[i - 1]]
        avg_move = mean(changes) * 100 if changes else 2
        stability_score = 100 - _clamp(avg_move, 0, 6) * 10
    peer_bonus = 10 if industry_stats.get("relative_strength") == "strong" else -8 if industry_stats.get("relative_strength") == "weak" else 0
    valuation_heat = _clamp(range_position * 0.48 + (ret20 or 0) * 0.9 + (ret60 or 0) * 0.28 + peer_bonus + ((rsi14 or 50) - 50) * 0.35, 0, 100)
    chip_score = _clamp((up_volume_ratio * 0.45) + (volume_score * 0.35) + (15 if obv_slope > 0 else -5), 0, 100)
    chip_label = "資金偏多" if chip_score >= 68 else "籌碼中性" if chip_score >= 45 else "資金偏弱"
    event_flags = _event_flags(rows)
    return {
        "range_position": range_position,
        "avg_volume_20": avg_volume_20,
        "liquidity_label": liquidity_label,
        "up_volume_ratio": up_volume_ratio,
        "obv_slope_label": obv_slope_label,
        "volume_score": _clamp(volume_score, 0, 100),
        "stability_score": _clamp(stability_score, 0, 100),
        "valuation_heat": valuation_heat,
        "chip_score": chip_score,
        "chip_label": chip_label,
        "event_flags": event_flags,
    }


def _event_flags(rows: list[sqlite3.Row]) -> list[str]:
    flags = []
    recent = rows[-20:] if len(rows) >= 20 else rows
    volumes = [float(row["volume"] or 0) for row in rows[-40:]]
    avg_volume = mean(volumes) if volumes else 0
    for prev, row in zip(recent[:-1], recent[1:]):
        prev_close = float(prev["close"])
        open_price = float(row["open"] or row["close"])
        close = float(row["close"])
        high = float(row["high"] or row["close"])
        low = float(row["low"] or row["close"])
        volume = float(row["volume"] or 0)
        gap = (open_price / prev_close - 1) * 100 if prev_close else 0
        intraday = (close / open_price - 1) * 100 if open_price else 0
        span = (high / low - 1) * 100 if low else 0
        if gap >= 3 and volume > avg_volume * 1.3:
            flags.append(f"{row['date']} 高開放量，需確認是否有利多或追價風險。")
        elif gap <= -3 and volume > avg_volume * 1.3:
            flags.append(f"{row['date']} 低開放量，需追蹤是否有利空或停損賣壓。")
        if intraday <= -4 and volume > avg_volume * 1.4:
            flags.append(f"{row['date']} 放量長黑，短線籌碼可能鬆動。")
        if span >= 7:
            flags.append(f"{row['date']} 盤中波動超過 7%，適合列入事件追蹤。")
    return flags[-4:]


def _industry_context(conn: sqlite3.Connection, stock: sqlite3.Row, rows: list[sqlite3.Row]) -> dict:
    industry = stock["industry"]
    if not industry or len(rows) < 21:
        return {"peer_count": 0, "relative_strength": "limited", "relative_strength_label": "資料有限"}
    latest_date = rows[-1]["date"]
    peer_rows = conn.execute(
        """
        SELECT p.stock_code, p.date, p.close
        FROM prices p
        JOIN stocks s ON s.code = p.stock_code
        WHERE s.industry = ? AND p.date <= ?
        ORDER BY p.stock_code, p.date
        """,
        (industry, latest_date),
    ).fetchall()
    by_code: dict[str, list[float]] = {}
    for row in peer_rows:
        by_code.setdefault(row["stock_code"], []).append(float(row["close"]))
    peer_returns = [pct_change(values, 20) for values in by_code.values() if len(values) >= 21]
    peer_returns = [value for value in peer_returns if value is not None]
    own_return = pct_change([float(row["close"]) for row in rows], 20)
    if not peer_returns or own_return is None:
        return {"peer_count": len(peer_returns), "relative_strength": "limited", "relative_strength_label": "資料有限"}
    avg = sum(peer_returns) / len(peer_returns)
    diff = own_return - avg
    if diff >= 10:
        relative = ("strong", "強於同業")
    elif diff <= -10:
        relative = ("weak", "落後同業")
    else:
        relative = ("neutral", "接近同業")
    names = {
        row["code"]: short_name(row["name"])
        for row in conn.execute("SELECT code, name FROM stocks WHERE industry = ?", (industry,)).fetchall()
    }
    peer_leaders = []
    for code, values in by_code.items():
        if len(values) < 21:
            continue
        ret = pct_change(values, 20)
        if ret is None:
            continue
        peer_leaders.append({"code": code, "name": names.get(code, code), "return_20d": ret})
    peer_leaders.sort(key=lambda item: item["return_20d"], reverse=True)
    return {
        "peer_count": len(peer_returns),
        "peer_return_20d": avg,
        "relative_strength": relative[0],
        "relative_strength_label": relative[1],
        "peer_leaders": peer_leaders[:5],
    }


def _verdict(close: float, ma20, ma60, ret20, rsi14, risk_score) -> str:
    if ma20 is not None and close < ma20:
        return "轉弱"
    if risk_score is not None and risk_score >= 78 and ret20 is not None and ret20 > 8:
        return "強勢"
    if ma20 is not None and ma60 is not None and close > ma20 > ma60:
        return "偏多"
    if rsi14 is not None and rsi14 > 78:
        return "偏熱"
    return "中性"


def _trend_series(rows: list[sqlite3.Row]) -> list[dict]:
    recent = rows[-252:] if len(rows) > 252 else rows
    if not recent:
        return []
    base = float(recent[0]["close"]) or 1
    out = []
    for row in recent:
        close = float(row["close"])
        out.append({
            "date": row["date"],
            "close": close,
            "indexed": close / base * 100,
            "volume": float(row["volume"] or 0),
        })
    return out


def _trend_stats(rows: list[sqlite3.Row]) -> dict:
    yearly: dict[str, list[sqlite3.Row]] = {}
    closes = [float(row["close"]) for row in rows]
    for row in rows:
        yearly.setdefault(str(row["date"])[:4], []).append(row)
    yearly_stats = []
    for year, items in sorted(yearly.items()):
        if len(items) < 2:
            continue
        first = float(items[0]["close"])
        last = float(items[-1]["close"])
        highs = [float(item["high"] or item["close"]) for item in items]
        lows = [float(item["low"] or item["close"]) for item in items]
        vols = [float(item["volume"] or 0) for item in items]
        yearly_stats.append({
            "year": year,
            "return": (last / first - 1) * 100 if first else 0,
            "high": max(highs),
            "low": min(lows),
            "avg_volume": mean(vols) if vols else 0,
        })
    peak = closes[0]
    max_drawdown = 0.0
    for close in closes:
        peak = max(peak, close)
        if peak:
            max_drawdown = min(max_drawdown, (close / peak - 1) * 100)
    return {"yearly": yearly_stats[-5:], "max_drawdown": max_drawdown}


def _radar_metrics(closes, volumes, industry_stats, ret20, ret60, rsi14, volx, risk_score) -> list[dict]:
    close = closes[-1]
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    high_60 = max(closes[-60:]) if len(closes) >= 60 else max(closes)
    low_60 = min(closes[-60:]) if len(closes) >= 60 else min(closes)
    range_pos = (close - low_60) / (high_60 - low_60) * 100 if high_60 != low_60 else 50
    trend_score = 50
    if ma20 and close > ma20:
        trend_score += 18
    if ma60 and ma20 and ma20 > ma60:
        trend_score += 18
    if ret60 is not None:
        trend_score += _clamp(ret60, -20, 40) / 40 * 14
    momentum_score = 50
    if ret20 is not None:
        momentum_score += _clamp(ret20, -15, 25)
    if rsi14 is not None:
        momentum_score += 15 - abs(rsi14 - 58) * 0.45
    volume_score = 50 + (_clamp((volx or 1) - 1, -0.6, 1.2) / 1.2) * 35
    peer_score = 55
    if industry_stats.get("relative_strength") == "strong":
        peer_score = 82
    elif industry_stats.get("relative_strength") == "weak":
        peer_score = 35
    risk_control = 100 - max(0, range_pos - 78) * 1.1
    if rsi14 and rsi14 > 75:
        risk_control -= 12
    stability = 55
    if len(closes) >= 60:
        changes = [abs(closes[i] / closes[i - 1] - 1) for i in range(len(closes) - 59, len(closes)) if closes[i - 1]]
        avg_move = mean(changes) * 100 if changes else 2
        stability = 100 - _clamp(avg_move, 0, 6) * 10
    return [
        {"label": "趨勢", "score": round(_clamp(trend_score, 0, 100), 1)},
        {"label": "動能", "score": round(_clamp(momentum_score, 0, 100), 1)},
        {"label": "量能", "score": round(_clamp(volume_score, 0, 100), 1)},
        {"label": "同業", "score": round(_clamp(peer_score, 0, 100), 1)},
        {"label": "風控", "score": round(_clamp(risk_control if risk_score is None else (risk_control + risk_score) / 2, 0, 100), 1)},
        {"label": "穩定", "score": round(_clamp(stability, 0, 100), 1)},
    ]


def _peer_cards(industry_stats: dict) -> list[dict]:
    return industry_stats.get("peer_leaders", [])[:5]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
