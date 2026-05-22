import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from datetime import date, datetime, time, timezone

from .config import REQUEST_HEADERS


def fetch_daily_prices(
    stock_code: str,
    yahoo_symbol: str,
    start_date: date,
    end_date: date,
) -> list[dict]:
    period1 = int(datetime.combine(start_date, time.min, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime.combine(end_date, time.min, tzinfo=timezone.utc).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    params = {
        "period1": period1,
        "period2": period2,
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }

    payload = _download_json(url, params)
    result = payload.get("chart", {}).get("result") or []
    if not result:
        error = payload.get("chart", {}).get("error")
        raise RuntimeError(f"Yahoo returned no result for {yahoo_symbol}: {error}")

    data = result[0]
    timestamps = data.get("timestamp") or []
    quote = (data.get("indicators", {}).get("quote") or [{}])[0]
    adjclose = (data.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose", [])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows = []
    for index, ts in enumerate(timestamps):
        close = _get(quote, "close", index)
        if close is None:
            continue
        rows.append(
            {
                "stock_code": stock_code,
                "date": datetime.fromtimestamp(ts, timezone.utc).date().isoformat(),
                "open": _get(quote, "open", index),
                "high": _get(quote, "high", index),
                "low": _get(quote, "low", index),
                "close": close,
                "adj_close": adjclose[index] if index < len(adjclose) else close,
                "volume": _get(quote, "volume", index),
                "source": "yahoo",
                "updated_at": now,
            }
        )
    return rows


def _get(quote: dict, field: str, index: int):
    values = quote.get(field) or []
    return values[index] if index < len(values) else None


def _download_json(url: str, params: dict) -> dict:
    full_url = f"{url}?{urlencode(params)}"
    request = Request(full_url, headers=REQUEST_HEADERS)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
    except (HTTPError, URLError) as exc:
        raise RuntimeError(str(exc)) from exc
    return json.loads(raw.decode("utf-8"))
