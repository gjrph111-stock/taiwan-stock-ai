import time
import xml.etree.ElementTree as ET
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
