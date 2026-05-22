import json
from datetime import date, datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import REQUEST_HEADERS


def fetch_official_close_prices(target_date: date) -> list[dict]:
    rows = []
    rows.extend(_fetch_twse(target_date))
    rows.extend(_fetch_tpex(target_date))
    return rows


def _fetch_twse(target_date: date) -> list[dict]:
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    payload = _download_json(
        url,
        {
            "date": target_date.strftime("%Y%m%d"),
            "type": "ALLBUT0999",
            "response": "json",
        },
    )
    table = _find_table(payload, "證券代號", "收盤價")
    if not table:
        return []

    fields = table.get("fields") or []
    rows = []
    for item in table.get("data") or []:
        mapped = dict(zip(fields, item))
        code = (mapped.get("證券代號") or "").strip()
        close = _to_float(mapped.get("收盤價"))
        if not code or close is None:
            continue
        rows.append(
            _row(
                code=code,
                target_date=target_date,
                open_price=_to_float(mapped.get("開盤價")),
                high=_to_float(mapped.get("最高價")),
                low=_to_float(mapped.get("最低價")),
                close=close,
                volume=_to_int(mapped.get("成交股數")),
                source="official_twse",
            )
        )
    return rows


def _fetch_tpex(target_date: date) -> list[dict]:
    url = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
    payload = _download_json(
        url,
        {
            "date": target_date.strftime("%Y/%m/%d"),
            "type": "EW",
            "response": "json",
        },
    )
    table = _find_table(payload, "代號", "收盤")
    if not table:
        return []

    fields = table.get("fields") or []
    rows = []
    for item in table.get("data") or []:
        mapped = dict(zip(fields, item))
        code = (mapped.get("代號") or "").strip()
        close = _to_float(mapped.get("收盤"))
        if not code or close is None:
            continue
        rows.append(
            _row(
                code=code,
                target_date=target_date,
                open_price=_to_float(mapped.get("開盤")),
                high=_to_float(mapped.get("最高")),
                low=_to_float(mapped.get("最低")),
                close=close,
                volume=_to_int(mapped.get("成交股數")),
                source="official_tpex",
            )
        )
    return rows


def _row(
    code: str,
    target_date: date,
    open_price: float | None,
    high: float | None,
    low: float | None,
    close: float,
    volume: int | None,
    source: str,
) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "stock_code": code,
        "date": target_date.isoformat(),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "adj_close": close,
        "volume": volume,
        "source": source,
        "updated_at": now,
    }


def _find_table(payload: dict, *required_fields: str) -> dict | None:
    for table in payload.get("tables") or []:
        fields = table.get("fields") or []
        if all(field in fields for field in required_fields):
            return table
    return None


def _download_json(url: str, params: dict) -> dict:
    full_url = f"{url}?{urlencode(params)}"
    request = Request(full_url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def _to_float(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "---"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value):
    number = _to_float(value)
    return int(number) if number is not None else None
