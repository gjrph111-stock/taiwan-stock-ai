import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha1
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from .config import REQUEST_HEADERS


_NEWS_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_SECONDS = 900


def fetch_stock_news(code: str, name: str = "", limit: int = 6) -> dict:
    key = f"{code}:{name}:{limit}"
    now = time.time()
    cached = _NEWS_CACHE.get(key)
    if cached and now - cached[0] < _CACHE_SECONDS:
        return cached[1]

    query = quote_plus(f"{code} {name} 股票 台股")
    url = f"https://news.google.com/rss/search?q={query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        request = Request(url, headers=REQUEST_HEADERS)
        with urlopen(request, timeout=4) as response:
            payload = response.read()
        root = ET.fromstring(payload)
        items = []
        for item in root.findall(".//item")[:limit]:
            title = _text(item, "title")
            source = item.find("source")
            items.append(
                {
                    "title": title,
                    "link": _text(item, "link"),
                    "published": _text(item, "pubDate"),
                    "source": source.text if source is not None and source.text else "Google News",
                    "sentiment": _sentiment(title),
                }
            )
        result = {
            "code": code,
            "name": name,
            "status": "ok" if items else "empty",
            "message": "已掃描最新新聞標題。" if items else "目前沒有抓到相關新聞。",
            "items": items,
        }
    except Exception as exc:
        result = {
            "code": code,
            "name": name,
            "status": "error",
            "message": f"新聞來源暫時無法連線：{exc}",
            "items": [],
        }
    _NEWS_CACHE[key] = (now, result)
    return result


def fetch_market_news(
    keywords: list[str] | None = None,
    limit_per_keyword: int = 5,
    max_items: int = 30,
    max_age_hours: int | None = 36,
) -> dict:
    queries = keywords or [
        "台股 重大 新聞",
        "台積電 半導體 AI 供應鏈",
        "美股 科技股 費半 台股",
        "台幣 匯率 利率 台股",
        "地緣政治 關稅 台股",
        "上市公司 重大訊息 台股",
    ]
    key = f"market:{'|'.join(queries)}:{limit_per_keyword}:{max_items}:{max_age_hours}"
    now = time.time()
    cached = _NEWS_CACHE.get(key)
    if cached and now - cached[0] < _CACHE_SECONDS:
        return cached[1]

    items: list[dict] = []
    failures: list[str] = []
    seen: set[str] = set()
    for query in queries:
        try:
            fetched = _fetch_google_news(query, limit_per_keyword)
        except Exception as exc:
            failures.append(f"{query}: {exc}")
            continue
        for item in fetched:
            if max_age_hours is not None and not _is_recent(item.get("published", ""), max_age_hours):
                continue
            identity = headline_id(item)
            if identity in seen:
                continue
            seen.add(identity)
            title = item.get("title", "")
            impact = classify_news_impact(title)
            item.update(
                {
                    "id": identity,
                    "query": query,
                    "sentiment": _sentiment(title),
                    "impact": impact["impact"],
                    "impact_score": impact["score"],
                    "category": impact["category"],
                    "action": impact["action"],
                }
            )
            items.append(item)
    items.sort(key=lambda row: (row.get("impact_score") or 0, row.get("published") or ""), reverse=True)
    result = {
        "status": "ok" if items else "empty",
        "message": "已掃描市場重大新聞。" if items else "目前沒有抓到市場新聞。",
        "items": items[:max_items],
        "failures": failures[:6],
    }
    _NEWS_CACHE[key] = (now, result)
    return result


def headline_id(item: dict) -> str:
    seed = f"{item.get('link') or ''}|{item.get('title') or ''}"
    return sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:20]


def classify_news_impact(title: str) -> dict:
    text = title or ""
    high_terms = [
        "地震",
        "停電",
        "戰爭",
        "制裁",
        "關稅",
        "禁令",
        "暴跌",
        "重挫",
        "破產",
        "財測下修",
        "下修",
        "法說",
        "併購",
        "重大訊息",
        "停工",
        "資安",
        "出口管制",
    ]
    positive_terms = ["上修", "新高", "接單", "擴產", "優於預期", "買超", "利多", "突破", "強攻"]
    macro_terms = ["Fed", "聯準會", "利率", "通膨", "匯率", "台幣", "美元", "費半", "Nasdaq", "美股"]
    score = 40
    category = "一般消息"
    if any(term in text for term in high_terms):
        score += 35
        category = "突發風險"
    if any(term in text for term in positive_terms):
        score += 18
        category = "利多催化"
    if any(term in text for term in macro_terms):
        score += 12
        category = "總經/海外連動"
    if any(term in text for term in ["台積", "鴻海", "聯發科", "輝達", "NVIDIA", "AI", "半導體", "記憶體"]):
        score += 10
        if category == "一般消息":
            category = "台股權值/產業"
    score = max(0, min(100, score))
    if score >= 75:
        impact = "高"
        action = "立即檢查相關持股、期貨與開盤量價，必要時先降風險。"
    elif score >= 55:
        impact = "中"
        action = "列入盤前/盤中觀察，等待價格與量能確認。"
    else:
        impact = "低"
        action = "保留追蹤，暫不因單一新聞改變部位。"
    return {"score": score, "impact": impact, "category": category, "action": action}


def _fetch_google_news(query: str, limit: int) -> list[dict]:
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=6) as response:
        payload = response.read()
    root = ET.fromstring(payload)
    items = []
    for item in root.findall(".//item")[:limit]:
        source = item.find("source")
        items.append(
            {
                "title": _text(item, "title"),
                "link": _text(item, "link"),
                "published": _text(item, "pubDate"),
                "source": source.text if source is not None and source.text else "Google News",
            }
        )
    return items


def _is_recent(published: str, max_age_hours: int) -> bool:
    if not published:
        return False
    try:
        dt = parsedate_to_datetime(published)
    except (TypeError, ValueError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt.astimezone(timezone.utc) <= timedelta(hours=max_age_hours)


def _text(item: ET.Element, tag: str) -> str:
    child = item.find(tag)
    return child.text.strip() if child is not None and child.text else ""


def _sentiment(title: str) -> str:
    positive = ["成長", "新高", "看好", "利多", "突破", "買超", "上修", "強攻", "獲利"]
    negative = ["衰退", "下修", "利空", "賣超", "虧損", "重挫", "調降", "風險", "減碼"]
    score = sum(word in title for word in positive) - sum(word in title for word in negative)
    if score > 0:
        return "偏多"
    if score < 0:
        return "偏空"
    return "中性"
