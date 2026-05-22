from collections.abc import Sequence


def sma(values: Sequence[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def pct_change(values: Sequence[float], periods: int) -> float | None:
    if len(values) <= periods:
        return None
    base = values[-periods - 1]
    if base in (None, 0):
        return None
    return (values[-1] / base - 1) * 100


def rsi(values: Sequence[float], window: int = 14) -> float | None:
    if len(values) <= window:
        return None

    gains = []
    losses = []
    recent = values[-window - 1 :]
    for prev, curr in zip(recent, recent[1:]):
        change = curr - prev
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))

    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def ema_series(values: Sequence[float], window: int) -> list[float]:
    if not values:
        return []
    multiplier = 2 / (window + 1)
    ema_values = [values[0]]
    for value in values[1:]:
        ema_values.append((value - ema_values[-1]) * multiplier + ema_values[-1])
    return ema_values


def macd(values: Sequence[float]) -> dict[str, float | None]:
    if len(values) < 35:
        return {"macd": None, "signal": None, "histogram": None}

    ema12 = ema_series(values, 12)
    ema26 = ema_series(values, 26)
    macd_line = [fast - slow for fast, slow in zip(ema12, ema26)]
    signal_line = ema_series(macd_line, 9)
    return {
        "macd": macd_line[-1],
        "signal": signal_line[-1],
        "histogram": macd_line[-1] - signal_line[-1],
    }


def avg_volume(volumes: Sequence[int], window: int) -> float | None:
    if len(volumes) < window:
        return None
    return sum(volumes[-window:]) / window


def volume_ratio(volumes: Sequence[int], short_window: int = 5, long_window: int = 20) -> float | None:
    short = avg_volume(volumes, short_window)
    long = avg_volume(volumes, long_window)
    if short is None or long in (None, 0):
        return None
    return short / long


def is_new_high(values: Sequence[float], window: int = 60) -> bool | None:
    if len(values) < window:
        return None
    return values[-1] >= max(values[-window:])


def fmt(value, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:.{digits}f}"
