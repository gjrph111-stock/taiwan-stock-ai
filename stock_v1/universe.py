import csv
import io
import json
import re
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import REQUEST_HEADERS, TPEX_COMPANY_URLS, TWSE_COMPANY_URLS


CODE_RE = re.compile(r"^\d{4}$")


def sync_universe() -> list[dict]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stocks = []
    stocks.extend(_fetch_market("TWSE", TWSE_COMPANY_URLS, now))
    stocks.extend(_fetch_market("TPEX", TPEX_COMPANY_URLS, now))
    deduped = {stock["code"]: stock for stock in stocks}
    return sorted(deduped.values(), key=lambda item: item["code"])


def _fetch_market(market: str, urls: list[str], updated_at: str) -> list[dict]:
    errors = []
    for url in urls:
        try:
            text, content_type = _download_text(url)
            records = _parse_response(text, content_type)
            stocks = [_normalize_record(record, market, updated_at) for record in records]
            stocks = [stock for stock in stocks if stock]
            if stocks:
                return stocks
            errors.append(f"{url}: parsed 0 stock records")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError(f"{market} company list failed. " + " | ".join(errors))


def _parse_response(text: str, content_type: str) -> list[dict]:
    clean = text.lstrip("\ufeff").strip()
    if "json" in content_type or clean.startswith("[") or clean.startswith("{"):
        data = json.loads(clean)
        if isinstance(data, dict):
            data = data.get("data", data.get("result", []))
        if not isinstance(data, list):
            raise ValueError("JSON response is not a list")
        return data

    reader = csv.DictReader(io.StringIO(clean))
    return list(reader)


def _download_text(url: str) -> tuple[str, str]:
    request = Request(url, headers=REQUEST_HEADERS)
    try:
        with urlopen(request, timeout=30) as response:
            content_type = response.headers.get("content-type", "")
            raw = response.read()
    except (HTTPError, URLError) as exc:
        raise RuntimeError(str(exc)) from exc
    return raw.decode("utf-8-sig", errors="replace"), content_type


def _normalize_record(record: dict, market: str, updated_at: str) -> dict | None:
    code = _pick(record, ["公司代號", "證券代號", "有價證券代號", "stock_id", "code"])
    name = _pick(record, ["公司名稱", "證券名稱", "有價證券名稱", "stock_name", "name"])
    industry = _pick(record, ["產業別", "industry", "產業類別"])

    if not code or not name:
        return None

    code = str(code).strip()
    name = str(name).strip()
    if not CODE_RE.match(code):
        return None

    suffix = ".TW" if market == "TWSE" else ".TWO"
    return {
        "code": code,
        "name": name,
        "market": market,
        "yahoo_symbol": f"{code}{suffix}",
        "industry": str(industry).strip() if industry else None,
        "updated_at": updated_at,
    }


def _pick(record: dict, candidates: list[str]) -> str | None:
    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    for candidate in candidates:
        value = lowered.get(candidate.lower())
        if value not in (None, ""):
            return value

    for key, value in record.items():
        key_text = str(key)
        if any(candidate in key_text for candidate in candidates):
            if value not in (None, ""):
                return value
    return None
