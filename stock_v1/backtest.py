import sqlite3
from collections import defaultdict

from .signals import passes_risk_filter, risk_adjusted_score, score_stock


def backtest_signals(
    conn: sqlite3.Connection,
    top_n: int = 10,
    horizon: int = 5,
    step: int = 5,
    max_days: int | None = 260,
    histories: dict | None = None,
    risk_filter: bool = True,
    adjusted_rank: bool = True,
) -> dict:
    histories = histories or load_histories(conn)
    usable_histories = {
        code: item for code, item in histories.items() if len(item["rows"]) >= 80 + horizon
    }

    dates = sorted(
        {
            item["rows"][idx]["date"]
            for item in usable_histories.values()
            for idx in range(79, len(item["rows"]) - horizon)
        }
    )
    if max_days:
        dates = dates[-max_days:]
    dates = dates[:: max(1, step)]

    picks = []
    by_label = defaultdict(list)

    for date_value in dates:
        daily = []
        for item in usable_histories.values():
            rows = item["rows"]
            idx = item["date_index"].get(date_value)
            if idx is None or idx < 79 or idx + horizon >= len(rows):
                continue
            signal = score_stock(item["stock"], rows[: idx + 1])
            if not signal:
                continue
            if risk_filter and not passes_risk_filter(signal):
                continue
            signal["risk_adjusted_score"] = risk_adjusted_score(signal)
            entry = rows[idx]["close"]
            exit_price = rows[idx + horizon]["close"]
            if not entry:
                continue
            future_return = (exit_price / entry - 1) * 100
            daily.append({**signal, "future_return": future_return})

        daily.sort(key=lambda row: _rank_key(row, adjusted_rank), reverse=True)
        for pick in daily[:top_n]:
            picks.append(pick)
            by_label[pick["signal"]].append(pick["future_return"])

    return _build_result(picks, by_label, len(dates), top_n, horizon, step)


def print_backtest_report(result: dict) -> None:
    print("Signal backtest")
    print(f"Tested dates: {result['tested_dates']}")
    print(f"Samples: {result['samples']}")
    print(f"Top N: {result['top_n']}")
    print(f"Horizon: {result['horizon']} trading days")
    print(f"Step: every {result['step']} trading days")
    print(f"Average return: {_fmt(result['avg_return'])}%")
    print(f"Median return: {_fmt(result['median_return'])}%")
    print(f"Win rate: {_fmt(result['win_rate'])}%")
    print(f"Average win: {_fmt(result['avg_win'])}%")
    print(f"Average loss: {_fmt(result['avg_loss'])}%")
    print(f"Best return: {_fmt(result['best_return'])}%")
    print(f"Worst return: {_fmt(result['worst_return'])}%")
    print("")
    print("By signal")
    print("signal   samples  avg_return%  median%  win_rate%")
    print("-------  -------  -----------  -------  ---------")
    for label, stats in result["by_signal"].items():
        print(
            f"{label:<7}  {stats['samples']:>7}  "
            f"{_fmt(stats['avg_return']):>11}  {_fmt(stats['median_return']):>7}  "
            f"{_fmt(stats['win_rate']):>9}"
        )


def optimize_backtest(
    conn: sqlite3.Connection,
    top_values: list[int] | None = None,
    horizons: list[int] | None = None,
    step: int = 5,
    max_days: int | None = 260,
    risk_filter: bool = True,
    adjusted_rank: bool = True,
) -> list[dict]:
    top_values = top_values or [5, 10, 20]
    horizons = horizons or [5, 10, 20]
    histories = load_histories(conn)
    max_horizon = max(horizons)
    dates = sorted(
        {
            item["rows"][idx]["date"]
            for item in histories.values()
            for idx in range(79, len(item["rows"]) - max_horizon)
        }
    )
    if max_days:
        dates = dates[-max_days:]
    dates = dates[:: max(1, step)]

    ranked_by_date = _rank_dates(
        histories,
        dates,
        horizons,
        risk_filter=risk_filter,
        adjusted_rank=adjusted_rank,
    )
    results = []
    for horizon in horizons:
        for top_n in top_values:
            picks = []
            by_label = defaultdict(list)
            for daily in ranked_by_date:
                selected = [item for item in daily if horizon in item["future_returns"]][:top_n]
                for item in selected:
                    pick = {**item, "future_return": item["future_returns"][horizon]}
                    picks.append(pick)
                    by_label[pick["signal"]].append(pick["future_return"])
            results.append(_build_result(picks, by_label, len(dates), top_n, horizon, step))
    return results


def print_optimization_report(results: list[dict]) -> None:
    print("Signal optimization backtest")
    print("top  horizon  samples  avg%    median%  win%    avg_win%  avg_loss%  worst%")
    print("---  -------  -------  ------  -------  ------  --------  ---------  ------")
    for result in results:
        print(
            f"{result['top_n']:>3}  {result['horizon']:>7}  {result['samples']:>7}  "
            f"{_fmt(result['avg_return']):>6}  {_fmt(result['median_return']):>7}  "
            f"{_fmt(result['win_rate']):>6}  {_fmt(result['avg_win']):>8}  "
            f"{_fmt(result['avg_loss']):>9}  {_fmt(result['worst_return']):>6}"
        )
    best = _best_result(results)
    if best:
        print("")
        print(
            "Suggested setting: "
            f"Top {best['top_n']} / {best['horizon']}D "
            f"(avg {_fmt(best['avg_return'])}%, median {_fmt(best['median_return'])}%, "
            f"win {_fmt(best['win_rate'])}%, worst {_fmt(best['worst_return'])}%)"
        )


def load_histories(conn: sqlite3.Connection) -> dict:
    stocks = conn.execute("SELECT code, name, market FROM stocks ORDER BY code").fetchall()
    histories = {}
    for stock in stocks:
        rows = conn.execute(
            """
            SELECT date, close, volume
            FROM prices
            WHERE stock_code = ?
            ORDER BY date
            """,
            (stock["code"],),
        ).fetchall()
        clean_rows = [row for row in rows if row["close"] is not None]
        if len(clean_rows) >= 80:
            histories[stock["code"]] = {
                "stock": stock,
                "rows": clean_rows,
                "date_index": {row["date"]: idx for idx, row in enumerate(clean_rows)},
            }
    return histories


def compare_risk_filter(
    conn: sqlite3.Connection,
    top_n: int = 5,
    horizons: list[int] | None = None,
    step: int = 5,
    max_days: int | None = 260,
) -> list[dict]:
    horizons = horizons or [5, 10, 20]
    results = []
    for use_filter in [False, True]:
        for horizon in horizons:
            result = backtest_signals(
                conn,
                top_n=top_n,
                horizon=horizon,
                step=step,
                max_days=max_days,
                risk_filter=use_filter,
            )
            result["risk_filter"] = use_filter
            results.append(result)
    return results


def print_risk_filter_report(results: list[dict]) -> None:
    print("Risk filter comparison")
    print("filter  horizon  samples  avg%    median%  win%    avg_loss%  worst%")
    print("------  -------  -------  ------  -------  ------  ---------  ------")
    for result in results:
        label = "on" if result.get("risk_filter") else "off"
        print(
            f"{label:<6}  {result['horizon']:>7}  {result['samples']:>7}  "
            f"{_fmt(result['avg_return']):>6}  {_fmt(result['median_return']):>7}  "
            f"{_fmt(result['win_rate']):>6}  {_fmt(result['avg_loss']):>9}  "
            f"{_fmt(result['worst_return']):>6}"
        )


def analyze_features(
    conn: sqlite3.Connection,
    horizon: int = 10,
    step: int = 5,
    max_days: int | None = 260,
) -> list[dict]:
    histories = load_histories(conn)
    dates = sorted(
        {
            item["rows"][idx]["date"]
            for item in histories.values()
            for idx in range(79, len(item["rows"]) - horizon)
        }
    )
    if max_days:
        dates = dates[-max_days:]
    dates = dates[:: max(1, step)]

    buckets = defaultdict(list)
    for date_value in dates:
        for item in histories.values():
            rows = item["rows"]
            idx = item["date_index"].get(date_value)
            if idx is None or idx < 79 or idx + horizon >= len(rows):
                continue
            entry = rows[idx]["close"]
            if not entry:
                continue
            signal = score_stock(item["stock"], rows[: idx + 1])
            if not signal:
                continue
            future_return = (rows[idx + horizon]["close"] / entry - 1) * 100
            for feature in _features(signal):
                buckets[feature].append(future_return)

    rows = []
    for feature, values in buckets.items():
        if len(values) < 100:
            continue
        rows.append(
            {
                "feature": feature,
                "samples": len(values),
                "avg_return": _avg(values),
                "median_return": _median(values),
                "win_rate": _win_rate(values),
                "avg_loss": _avg([value for value in values if value <= 0]),
                "worst_return": min(values) if values else None,
            }
        )
    rows.sort(key=lambda row: ((row["median_return"] or -999), (row["avg_return"] or -999)), reverse=True)
    return rows


def print_feature_report(rows: list[dict]) -> None:
    print("Feature contribution analysis")
    print("feature                    samples  avg%    median%  win%    avg_loss%  worst%")
    print("-------------------------  -------  ------  -------  ------  ---------  ------")
    for row in rows:
        print(
            f"{row['feature']:<25}  {row['samples']:>7}  "
            f"{_fmt(row['avg_return']):>6}  {_fmt(row['median_return']):>7}  "
            f"{_fmt(row['win_rate']):>6}  {_fmt(row['avg_loss']):>9}  "
            f"{_fmt(row['worst_return']):>6}"
        )


def strategy_backtest(
    conn: sqlite3.Connection,
    top_n: int = 5,
    horizon: int = 20,
    step: int = 5,
    max_days: int | None = 260,
    initial_capital: float = 100.0,
) -> dict:
    histories = load_histories(conn)
    dates = sorted(
        {
            item["rows"][idx]["date"]
            for item in histories.values()
            for idx in range(79, len(item["rows"]) - horizon)
        }
    )
    if max_days:
        dates = dates[-max_days:]
    dates = dates[:: max(1, step)]
    ranked_by_date = _rank_dates(histories, dates, [horizon], risk_filter=True, adjusted_rank=True)

    capital = initial_capital
    curve = []
    trades = []
    wins = 0

    for date_value, daily in zip(dates, ranked_by_date):
        selected = [item for item in daily if horizon in item["future_returns"]][:top_n]
        if not selected:
            curve.append({"date": date_value, "capital": capital, "period_return": 0})
            continue
        returns = [item["future_returns"][horizon] for item in selected]
        period_return = sum(returns) / len(returns)
        capital *= 1 + period_return / 100
        wins += 1 if period_return > 0 else 0
        curve.append({"date": date_value, "capital": capital, "period_return": period_return})
        for item in selected:
            trades.append(
                {
                    "date": date_value,
                    "code": item["code"],
                    "name": item["short_name"],
                    "score": item["score"],
                    "return": item["future_returns"][horizon],
                }
            )

    period_returns = [row["period_return"] for row in curve if row["period_return"] != 0]
    total_return = (capital / initial_capital - 1) * 100
    return {
        "top_n": top_n,
        "horizon": horizon,
        "step": step,
        "tested_dates": len(dates),
        "periods": len(period_returns),
        "trades": len(trades),
        "initial_capital": initial_capital,
        "final_capital": capital,
        "total_return": total_return,
        "avg_period_return": _avg(period_returns),
        "median_period_return": _median(period_returns),
        "win_rate": _win_rate(period_returns),
        "max_drawdown": _max_drawdown([row["capital"] for row in curve]),
        "best_period": max(period_returns) if period_returns else None,
        "worst_period": min(period_returns) if period_returns else None,
        "curve": curve,
        "recent_trades": trades[-20:],
    }


def print_strategy_report(result: dict) -> None:
    print("Strategy backtest")
    print(f"Top N: {result['top_n']}")
    print(f"Horizon: {result['horizon']} trading days")
    print(f"Rebalance step: {result['step']} trading days")
    print(f"Tested dates: {result['tested_dates']}")
    print(f"Periods: {result['periods']}")
    print(f"Trades: {result['trades']}")
    print(f"Initial capital: {_fmt(result['initial_capital'])}")
    print(f"Final capital: {_fmt(result['final_capital'])}")
    print(f"Total return: {_fmt(result['total_return'])}%")
    print(f"Average period return: {_fmt(result['avg_period_return'])}%")
    print(f"Median period return: {_fmt(result['median_period_return'])}%")
    print(f"Win rate: {_fmt(result['win_rate'])}%")
    print(f"Max drawdown: {_fmt(result['max_drawdown'])}%")
    print(f"Best period: {_fmt(result['best_period'])}%")
    print(f"Worst period: {_fmt(result['worst_period'])}%")


def realistic_strategy_backtest(
    conn: sqlite3.Connection,
    max_positions: int = 5,
    horizon: int = 20,
    step: int = 5,
    max_days: int | None = 260,
    initial_capital: float = 100.0,
    cost_bps: float = 20.0,
    start_date: str | None = None,
) -> dict:
    histories = load_histories(conn)
    dates = sorted(
        {
            item["rows"][idx]["date"]
            for item in histories.values()
            for idx in range(79, len(item["rows"]) - horizon)
        }
    )
    if start_date:
        dates = [date_value for date_value in dates if date_value >= start_date]
    if max_days:
        dates = dates[-max_days:]
    rebalance_dates = dates[:: max(1, step)]
    ranked_by_date = _rank_dates(histories, rebalance_dates, [horizon], risk_filter=True, adjusted_rank=True)

    cash = initial_capital
    positions = []
    closed_trades = []
    entries = []
    curve = []
    cost_rate = cost_bps / 10000

    for date_value, daily in zip(rebalance_dates, ranked_by_date):
        still_open = []
        for position in positions:
            if position["exit_date"] <= date_value:
                proceeds = position["capital"] * (1 + position["return"] / 100) * (1 - cost_rate)
                cash += proceeds
                closed_trades.append({**position, "proceeds": proceeds})
            else:
                still_open.append(position)
        positions = still_open

        slots = max_positions - len(positions)
        held_codes = {position["code"] for position in positions}
        if slots > 0:
            candidates = [
                item for item in daily
                if item["code"] not in held_codes and horizon in item["future_returns"]
            ][:slots]
            allocation = cash / slots if slots else 0
            for item in candidates:
                if allocation <= 0:
                    continue
                entry_capital = allocation * (1 - cost_rate)
                cash -= allocation
                positions.append(
                    {
                        "entry_date": date_value,
                        "exit_date": _future_date(histories[item["code"]]["rows"], date_value, horizon),
                        "code": item["code"],
                        "name": item["short_name"],
                        "score": item["score"],
                        "entry_price": item.get("entry_price"),
                        "exit_price": item.get("future_prices", {}).get(horizon),
                        "capital": entry_capital,
                        "return": item["future_returns"][horizon],
                        "entry_value": allocation,
                    }
                )
                entries.append({**positions[-1]})

        marked_value = cash + sum(
            position["capital"] * (1 + position["return"] / 100)
            for position in positions
        )
        curve.append({"date": date_value, "capital": marked_value})

    latest_date = max((item["rows"][-1]["date"] for item in histories.values() if item["rows"]), default=None)
    if latest_date:
        still_open = []
        for position in positions:
            if position["exit_date"] <= latest_date:
                proceeds = position["capital"] * (1 + position["return"] / 100) * (1 - cost_rate)
                cash += proceeds
                closed_trades.append({**position, "proceeds": proceeds})
            else:
                still_open.append(position)
        positions = still_open
        final_marked_value = cash + sum(
            position["capital"] * (1 + position["return"] / 100)
            for position in positions
        )
        if curve:
            if curve[-1]["date"] == latest_date:
                curve[-1]["capital"] = final_marked_value
            else:
                curve.append({"date": latest_date, "capital": final_marked_value})

    final_capital = curve[-1]["capital"] if curve else initial_capital
    trade_returns = [trade["return"] for trade in closed_trades]
    return {
        "max_positions": max_positions,
        "horizon": horizon,
        "step": step,
        "cost_bps": cost_bps,
        "start_date": start_date,
        "tested_dates": len(rebalance_dates),
        "trades": len(closed_trades),
        "initial_capital": initial_capital,
        "final_capital": final_capital,
        "total_return": (final_capital / initial_capital - 1) * 100,
        "win_rate": _win_rate(trade_returns),
        "avg_trade_return": _avg(trade_returns),
        "median_trade_return": _median(trade_returns),
        "max_drawdown": _max_drawdown([row["capital"] for row in curve]),
        "best_trade": max(trade_returns) if trade_returns else None,
        "worst_trade": min(trade_returns) if trade_returns else None,
        "open_positions": len(positions),
        "open_position_details": positions,
        "curve": curve,
        "all_trades": closed_trades,
        "recent_trades": closed_trades[-20:],
        "recent_entries": entries[-20:],
    }


def high_win_strategy_backtest(
    conn: sqlite3.Connection,
    horizon: int = 5,
    step: int = 5,
    max_days: int | None = 180,
    top_n: int = 3,
) -> dict:
    histories = load_histories(conn)
    dates = sorted(
        {
            item["rows"][idx]["date"]
            for item in histories.values()
            for idx in range(79, len(item["rows"]) - horizon)
        }
    )
    if max_days:
        dates = dates[-max_days:]
    dates = dates[:: max(1, step)]
    ranked_by_date = _rank_dates(histories, dates, [horizon], risk_filter=True, adjusted_rank=True)
    picks = []
    for date_value, daily in zip(dates, ranked_by_date):
        selected = [
            item for item in daily
            if _passes_high_win_profile(item) and horizon in item["future_returns"]
        ][:top_n]
        for item in selected:
            picks.append(
                {
                    "entry_date": date_value,
                    "exit_date": _future_date(histories[item["code"]]["rows"], date_value, horizon),
                    "code": item["code"],
                    "name": item["short_name"],
                    "score": item["score"],
                    "entry_price": item.get("entry_price"),
                    "exit_price": item.get("future_prices", {}).get(horizon),
                    "return": item["future_returns"][horizon],
                    "rsi_14": item.get("rsi_14"),
                    "return_20d": item.get("return_20d"),
                    "volume_ratio": item.get("volume_ratio"),
                }
            )
    returns = [item["return"] for item in picks]
    return {
        "name": "高勝率保守模式",
        "horizon": horizon,
        "step": step,
        "top_n": top_n,
        "trades": len(picks),
        "win_rate": _win_rate(returns),
        "avg_return": _avg(returns),
        "median_return": _median(returns),
        "best_return": max(returns) if returns else None,
        "worst_return": min(returns) if returns else None,
        "recent_trades": picks[-12:],
        "rules": [
            "只挑 AI 分數 >= 82",
            "RSI 介於 45 到 65，避開過熱追價",
            "20 日漲幅介於 3% 到 18%",
            "量比介於 0.9 到 2.0，避開爆量失真",
            "持有 5 個交易日，最多取前 3 檔",
        ],
    }


def _passes_high_win_profile(item: dict) -> bool:
    score = item.get("score") or 0
    rsi14 = item.get("rsi_14")
    ret20 = item.get("return_20d")
    volx = item.get("volume_ratio")
    if score < 82:
        return False
    if rsi14 is None or not (45 <= rsi14 <= 65):
        return False
    if ret20 is None or not (3 <= ret20 <= 18):
        return False
    if volx is None or not (0.9 <= volx <= 2.0):
        return False
    return True


def print_realistic_strategy_report(result: dict) -> None:
    print("Realistic strategy backtest")
    print(f"Max positions: {result['max_positions']}")
    print(f"Horizon: {result['horizon']} trading days")
    print(f"Rebalance step: {result['step']} trading days")
    print(f"Cost: {result['cost_bps']} bps per buy/sell")
    print(f"Tested dates: {result['tested_dates']}")
    print(f"Closed trades: {result['trades']}")
    print(f"Open positions at end: {result['open_positions']}")
    print(f"Initial capital: {_fmt(result['initial_capital'])}")
    print(f"Final capital: {_fmt(result['final_capital'])}")
    print(f"Total return: {_fmt(result['total_return'])}%")
    print(f"Trade win rate: {_fmt(result['win_rate'])}%")
    print(f"Average trade return: {_fmt(result['avg_trade_return'])}%")
    print(f"Median trade return: {_fmt(result['median_trade_return'])}%")
    print(f"Max drawdown: {_fmt(result['max_drawdown'])}%")
    print(f"Best trade: {_fmt(result['best_trade'])}%")
    print(f"Worst trade: {_fmt(result['worst_trade'])}%")
    print("")
    print("Recent closed trades")
    print("entry       exit        code  name        score  return%  capital")
    print("----------  ----------  ----  ----------  -----  -------  -------")
    for trade in result["recent_trades"][-10:]:
        print(
            f"{trade['entry_date']:<10}  {trade['exit_date']:<10}  {trade['code']:<4}  "
            f"{trade['name']:<10}  {trade['score']:>5}  {_fmt(trade['return']):>7}  "
            f"{_fmt(trade['capital']):>7}"
        )
    if result["open_position_details"]:
        print("")
        print("Open positions")
        print("entry       planned exit  code  name        score  expected%  capital")
        print("----------  ------------  ----  ----------  -----  ---------  -------")
        for pos in result["open_position_details"]:
            print(
                f"{pos['entry_date']:<10}  {pos['exit_date']:<12}  {pos['code']:<4}  "
                f"{pos['name']:<10}  {pos['score']:>5}  {_fmt(pos['return']):>9}  "
                f"{_fmt(pos['capital']):>7}"
            )


def _rank_dates(
    histories: dict,
    dates: list[str],
    horizons: list[int],
    risk_filter: bool = True,
    adjusted_rank: bool = True,
) -> list[list[dict]]:
    ranked = []
    for date_value in dates:
        daily = []
        for item in histories.values():
            rows = item["rows"]
            idx = item["date_index"].get(date_value)
            if idx is None or idx < 79:
                continue
            future_returns = {}
            future_prices = {}
            entry = rows[idx]["close"]
            if not entry:
                continue
            for horizon in horizons:
                if idx + horizon < len(rows):
                    future_price = rows[idx + horizon]["close"]
                    future_prices[horizon] = future_price
                    future_returns[horizon] = (future_price / entry - 1) * 100
            if not future_returns:
                continue
            signal = score_stock(item["stock"], rows[: idx + 1])
            if signal and risk_filter and not passes_risk_filter(signal):
                continue
            if signal:
                signal["risk_adjusted_score"] = risk_adjusted_score(signal)
                daily.append({**signal, "entry_price": entry, "future_prices": future_prices, "future_returns": future_returns})
        daily.sort(key=lambda row: _rank_key(row, adjusted_rank), reverse=True)
        ranked.append(daily)
    return ranked


def _build_result(
    picks: list[dict],
    by_label: dict,
    tested_dates: int,
    top_n: int,
    horizon: int,
    step: int,
) -> dict:
    returns = [pick["future_return"] for pick in picks]
    return {
        "tested_dates": tested_dates,
        "samples": len(picks),
        "top_n": top_n,
        "horizon": horizon,
        "step": step,
        "avg_return": _avg(returns),
        "median_return": _median(returns),
        "win_rate": _win_rate(returns),
        "avg_win": _avg([value for value in returns if value > 0]),
        "avg_loss": _avg([value for value in returns if value <= 0]),
        "best_return": max(returns) if returns else None,
        "worst_return": min(returns) if returns else None,
        "by_signal": {
            label: {
                "samples": len(values),
                "avg_return": _avg(values),
                "median_return": _median(values),
                "win_rate": _win_rate(values),
            }
            for label, values in sorted(by_label.items())
        },
        "recent_examples": picks[-10:],
    }


def _rank_key(row: dict, adjusted_rank: bool) -> tuple:
    score_key = row.get("risk_adjusted_score", row["score"]) if adjusted_rank else row["score"]
    return (score_key, row["score"], row["return_20d"] or -9999)


def _future_date(rows: list[sqlite3.Row], date_value: str, horizon: int) -> str:
    for idx, row in enumerate(rows):
        if row["date"] == date_value:
            return rows[min(idx + horizon, len(rows) - 1)]["date"]
    return date_value


def _features(signal: dict) -> list[str]:
    features = []
    ret20 = signal.get("return_20d")
    ret60 = signal.get("return_60d")
    rsi14 = signal.get("rsi_14")
    volx = signal.get("volume_ratio")
    score = signal.get("score")
    adjusted = signal.get("risk_adjusted_score", score)

    if score is not None and score >= 75:
        features.append("score >= 75")
    if adjusted is not None and adjusted >= 75:
        features.append("adjusted >= 75")
    if "above SMA20" in signal.get("reasons", []):
        features.append("above SMA20")
    if "above SMA60" in signal.get("reasons", []):
        features.append("above SMA60")
    if any(reason.startswith("MACD positive") for reason in signal.get("reasons", [])):
        features.append("MACD positive")
    if signal.get("new_high_60"):
        features.append("new 60D high")
    if ret20 is not None:
        if 3 <= ret20 <= 15:
            features.append("ret20 3-15")
        if 15 < ret20 <= 28:
            features.append("ret20 15-28")
        if ret20 > 28:
            features.append("ret20 > 28")
    if ret60 is not None:
        if 5 <= ret60 <= 25:
            features.append("ret60 5-25")
        if 25 < ret60 <= 55:
            features.append("ret60 25-55")
        if ret60 > 55:
            features.append("ret60 > 55")
    if rsi14 is not None:
        if 45 <= rsi14 <= 60:
            features.append("RSI 45-60")
        if 60 < rsi14 <= 70:
            features.append("RSI 60-70")
        if rsi14 > 70:
            features.append("RSI > 70")
    if volx is not None:
        if 1.15 <= volx <= 1.8:
            features.append("volume 1.15-1.8")
        if 1.8 < volx <= 2.8:
            features.append("volume 1.8-2.8")
        if volx > 2.8:
            features.append("volume > 2.8")
    return features


def _best_result(results: list[dict]) -> dict | None:
    candidates = [result for result in results if result["samples"]]
    if not candidates:
        return None
    return max(candidates, key=_stability_score)


def _stability_score(result: dict) -> float:
    avg_return = result["avg_return"] or 0
    median_return = result["median_return"] or 0
    win_rate = result["win_rate"] or 0
    worst_return = result["worst_return"] or 0
    return avg_return + median_return * 2 + (win_rate - 50) * 0.1 + worst_return * 0.15


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _win_rate(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value > 0) / len(values) * 100


def _max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    peak = values[0]
    max_dd = 0.0
    for value in values:
        peak = max(peak, value)
        if peak:
            drawdown = (value / peak - 1) * 100
            max_dd = min(max_dd, drawdown)
    return max_dd


def _fmt(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"
