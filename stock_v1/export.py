import csv
from datetime import datetime
from pathlib import Path


def export_strategy_report(result: dict, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    paths = {
        "summary": output_dir / f"strategy_summary_{stamp}.csv",
        "trades": output_dir / f"strategy_trades_{stamp}.csv",
        "curve": output_dir / f"strategy_curve_{stamp}.csv",
    }

    _write_summary(paths["summary"], result)
    _write_trades(paths["trades"], result)
    _write_curve(paths["curve"], result)
    return paths


def _write_summary(path: Path, result: dict) -> None:
    fields = [
        "max_positions",
        "horizon",
        "step",
        "cost_bps",
        "tested_dates",
        "trades",
        "open_positions",
        "initial_capital",
        "final_capital",
        "total_return",
        "win_rate",
        "avg_trade_return",
        "median_trade_return",
        "max_drawdown",
        "best_trade",
        "worst_trade",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerow({field: result.get(field) for field in fields})


def _write_trades(path: Path, result: dict) -> None:
    fields = [
        "entry_date",
        "exit_date",
        "code",
        "name",
        "score",
        "capital",
        "return",
        "proceeds",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for trade in result.get("all_trades", result.get("recent_trades", [])):
            writer.writerow({field: trade.get(field) for field in fields})


def _write_curve(path: Path, result: dict) -> None:
    fields = ["date", "capital"]
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in result.get("curve", []):
            writer.writerow({field: row.get(field) for field in fields})
