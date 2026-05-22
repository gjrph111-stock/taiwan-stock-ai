import argparse
import os
from pathlib import Path

from . import db
from .backtest import (
    analyze_features,
    backtest_signals,
    compare_risk_filter,
    optimize_backtest,
    print_backtest_report,
    print_feature_report,
    print_optimization_report,
    print_realistic_strategy_report,
    print_risk_filter_report,
    print_strategy_report,
    realistic_strategy_backtest,
    strategy_backtest,
)
from .config import DEFAULT_DB_PATH
from .daily import run_daily
from .export import export_strategy_report
from .notify import DEFAULT_CONFIG_PATH, build_daily_message, load_config, send_line, send_telegram
from .reports import (
    print_indicator_report,
    print_market_scan,
    print_signal_report,
    print_stock_report,
    print_watchlist,
)
from .update import update_prices, update_universe
from .web import run as run_web


def main() -> None:
    parser = argparse.ArgumentParser(description="Taiwan stock data pipeline V1")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("universe", help="Sync TWSE/TPEX stock universe")

    update_parser = subparsers.add_parser("update", help="Update daily prices")
    update_parser.add_argument("--years", type=int, default=3, help="Initial history window")
    update_parser.add_argument("--codes", help="Comma-separated stock codes, e.g. 2330,2317")
    update_parser.add_argument("--pause", type=float, default=0.2, help="Seconds to pause between requests")

    subparsers.add_parser("status", help="Show database status")

    stock_parser = subparsers.add_parser("stock", help="Show one stock summary")
    stock_parser.add_argument("code", help="Stock code, e.g. 2330")

    indicators_parser = subparsers.add_parser("indicators", help="Show technical indicators")
    indicators_parser.add_argument("code", help="Stock code, e.g. 2330")

    scan_parser = subparsers.add_parser("scan", help="Scan market rankings")
    scan_parser.add_argument("--limit", type=int, default=20, help="Rows per ranking table")

    signals_parser = subparsers.add_parser("signals", help="Rank explainable stock signals")
    signals_parser.add_argument("--limit", type=int, default=20, help="Rows to show")

    watchlist_parser = subparsers.add_parser("watchlist", help="Show daily top signal watchlist")
    watchlist_parser.add_argument("--limit", type=int, default=5, help="Rows to show")

    backtest_parser = subparsers.add_parser("backtest", help="Backtest signal ranking")
    backtest_parser.add_argument("--top", type=int, default=10, help="Top N signals per tested date")
    backtest_parser.add_argument("--horizon", type=int, default=5, help="Future trading days")
    backtest_parser.add_argument("--step", type=int, default=5, help="Evaluate every N trading days")
    backtest_parser.add_argument("--max-days", type=int, default=260, help="Recent dates to test")
    backtest_parser.add_argument("--no-risk-filter", action="store_true", help="Disable risk filter")
    backtest_parser.add_argument("--raw-rank", action="store_true", help="Rank by raw score instead of risk-adjusted score")

    optimize_parser = subparsers.add_parser("optimize", help="Compare signal backtest settings")
    optimize_parser.add_argument("--tops", default="5,10,20", help="Comma-separated top N values")
    optimize_parser.add_argument("--horizons", default="5,10,20", help="Comma-separated horizons")
    optimize_parser.add_argument("--step", type=int, default=5, help="Evaluate every N trading days")
    optimize_parser.add_argument("--max-days", type=int, default=260, help="Recent dates to test")
    optimize_parser.add_argument("--no-risk-filter", action="store_true", help="Disable risk filter")
    optimize_parser.add_argument("--raw-rank", action="store_true", help="Rank by raw score instead of risk-adjusted score")

    risk_parser = subparsers.add_parser("risk-filter", help="Compare backtest with and without risk filter")
    risk_parser.add_argument("--top", type=int, default=5, help="Top N signals per tested date")
    risk_parser.add_argument("--horizons", default="5,10,20", help="Comma-separated horizons")
    risk_parser.add_argument("--step", type=int, default=5, help="Evaluate every N trading days")
    risk_parser.add_argument("--max-days", type=int, default=260, help="Recent dates to test")

    features_parser = subparsers.add_parser("features", help="Analyze signal feature contribution")
    features_parser.add_argument("--horizon", type=int, default=10, help="Future trading days")
    features_parser.add_argument("--step", type=int, default=5, help="Evaluate every N trading days")
    features_parser.add_argument("--max-days", type=int, default=260, help="Recent dates to test")

    strategy_parser = subparsers.add_parser("strategy", help="Backtest portfolio strategy from top signals")
    strategy_parser.add_argument("--top", type=int, default=5, help="Top N signals per rebalance")
    strategy_parser.add_argument("--horizon", type=int, default=20, help="Holding period in trading days")
    strategy_parser.add_argument("--step", type=int, default=5, help="Rebalance every N trading days")
    strategy_parser.add_argument("--max-days", type=int, default=260, help="Recent dates to test")

    realistic_parser = subparsers.add_parser("realistic-strategy", help="Backtest realistic position strategy")
    realistic_parser.add_argument("--positions", type=int, default=5, help="Maximum simultaneous positions")
    realistic_parser.add_argument("--horizon", type=int, default=20, help="Holding period in trading days")
    realistic_parser.add_argument("--step", type=int, default=5, help="Check new entries every N trading days")
    realistic_parser.add_argument("--max-days", type=int, default=260, help="Recent dates to test")
    realistic_parser.add_argument("--cost-bps", type=float, default=20.0, help="Cost in bps per buy/sell")
    realistic_parser.add_argument("--export", action="store_true", help="Export summary/trades/curve CSV")
    realistic_parser.add_argument("--output-dir", default="reports", help="CSV output directory")

    web_parser = subparsers.add_parser("web", help="Start local web dashboard")
    web_parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"), help="Dashboard host")
    web_parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")), help="Dashboard port")

    notify_preview_parser = subparsers.add_parser("notify-preview", help="Preview daily push message")
    notify_preview_parser.add_argument("--limit", type=int, default=5, help="Rows per ranking")

    telegram_parser = subparsers.add_parser("notify-telegram", help="Send daily report to Telegram")
    telegram_parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Notification config path")
    telegram_parser.add_argument("--limit", type=int, default=5, help="Rows per ranking")

    line_parser = subparsers.add_parser("notify-line", help="Send daily report to LINE")
    line_parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Notification config path")
    line_parser.add_argument("--limit", type=int, default=5, help="Rows per ranking")

    daily_parser = subparsers.add_parser("daily-run", help="Update data and send daily Telegram report")
    daily_parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Notification config path")
    daily_parser.add_argument("--years", type=int, default=3, help="Initial history window")
    daily_parser.add_argument("--pause", type=float, default=0.2, help="Seconds to pause between requests")
    daily_parser.add_argument("--limit", type=int, default=5, help="Rows per ranking")
    daily_parser.add_argument("--no-send", action="store_true", help="Preview only, do not send Telegram")
    daily_parser.add_argument(
        "--strict-universe",
        action="store_true",
        help="Fail if stock universe sync fails instead of using existing local universe",
    )

    runs_parser = subparsers.add_parser("runs", help="Show recent automation runs")
    runs_parser.add_argument("--limit", type=int, default=10, help="Number of runs to show")

    args = parser.parse_args()
    conn = db.connect(Path(args.db))

    if args.command == "universe":
        count = update_universe(conn)
        print(f"Synced {count} stocks into {args.db}")
        return

    if args.command == "update":
        codes = _parse_codes(args.codes)
        result = update_prices(conn, args.years, codes, args.pause)
        print(
            f"Updated {result['rows']} Yahoo price rows and "
            f"{result.get('official_rows', 0)} official close rows for {result['stocks']} stocks"
        )
        if result["failures"]:
            print(f"Failures: {len(result['failures'])}")
            for failure in result["failures"][:20]:
                print(f"- {failure['code']} {failure['name']}: {failure['error']}")
        return

    if args.command == "status":
        info = db.status(conn)
        print(f"Stocks: {info['stocks']}")
        print(f"Prices: {info['prices']}")
        print(f"Markets: {info['markets']}")
        print(f"Date range: {info['first_date']} -> {info['last_date']}")
        return

    if args.command == "stock":
        print_stock_report(conn, args.code)
        return

    if args.command == "indicators":
        print_indicator_report(conn, args.code)
        return

    if args.command == "scan":
        print_market_scan(conn, args.limit)
        return

    if args.command == "signals":
        print_signal_report(conn, args.limit)
        return

    if args.command == "watchlist":
        print_watchlist(conn, args.limit)
        return

    if args.command == "backtest":
        result = backtest_signals(
            conn,
            args.top,
            args.horizon,
            args.step,
            args.max_days,
            risk_filter=not args.no_risk_filter,
            adjusted_rank=not args.raw_rank,
        )
        print_backtest_report(result)
        return

    if args.command == "optimize":
        results = optimize_backtest(
            conn,
            top_values=_parse_ints(args.tops),
            horizons=_parse_ints(args.horizons),
            step=args.step,
            max_days=args.max_days,
            risk_filter=not args.no_risk_filter,
            adjusted_rank=not args.raw_rank,
        )
        print_optimization_report(results)
        return

    if args.command == "risk-filter":
        results = compare_risk_filter(
            conn,
            top_n=args.top,
            horizons=_parse_ints(args.horizons),
            step=args.step,
            max_days=args.max_days,
        )
        print_risk_filter_report(results)
        return

    if args.command == "features":
        rows = analyze_features(conn, horizon=args.horizon, step=args.step, max_days=args.max_days)
        print_feature_report(rows)
        return

    if args.command == "strategy":
        result = strategy_backtest(
            conn,
            top_n=args.top,
            horizon=args.horizon,
            step=args.step,
            max_days=args.max_days,
        )
        print_strategy_report(result)
        return

    if args.command == "realistic-strategy":
        result = realistic_strategy_backtest(
            conn,
            max_positions=args.positions,
            horizon=args.horizon,
            step=args.step,
            max_days=args.max_days,
            cost_bps=args.cost_bps,
        )
        print_realistic_strategy_report(result)
        if args.export:
            paths = export_strategy_report(result, Path(args.output_dir))
            print("")
            print("Exported CSV files:")
            for label, path in paths.items():
                print(f"{label}: {path}")
        return

    if args.command == "web":
        conn.close()
        run_web(args.host, args.port, Path(args.db))
        return

    if args.command == "notify-preview":
        conn.close()
        print(build_daily_message(Path(args.db), args.limit))
        return

    if args.command == "notify-telegram":
        conn.close()
        message = build_daily_message(Path(args.db), args.limit)
        result = send_telegram(message, load_config(Path(args.config)))
        print(result)
        return

    if args.command == "notify-line":
        conn.close()
        message = build_daily_message(Path(args.db), args.limit)
        result = send_line(message, load_config(Path(args.config)))
        print(result)
        return

    if args.command == "daily-run":
        result = run_daily(
            conn,
            db_path=Path(args.db),
            config_path=Path(args.config),
            years=args.years,
            pause=args.pause,
            limit=args.limit,
            send=not args.no_send,
            skip_universe_on_error=not args.strict_universe,
        )
        print(
            f"Daily run {result['run_id']} finished: {result['status']}, "
            f"rows={result['rows']}, failures={len(result['failures'])}"
        )
        return

    if args.command == "runs":
        for row in db.latest_runs(conn, args.limit):
            print(
                f"#{row['id']} {row['task']} {row['status']} "
                f"{row['started_at']} -> {row['finished_at']} {row['message'] or ''}"
            )
        return


def _parse_codes(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [code.strip() for code in raw.split(",") if code.strip()]


def _parse_ints(raw: str) -> list[int]:
    return [int(value.strip()) for value in raw.split(",") if value.strip()]
