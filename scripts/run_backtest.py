#!/usr/bin/env python
"""Run a backtest from the CLI.

    uv run python scripts/run_backtest.py --strategy sma_crossover \
      --symbols SPY --start 2020-01-01 --end 2024-12-31

Defaults to a realistic cost model. `--zero-cost` exists for debugging engine
mechanics and prints a warning — a zero-cost result is not evidence about a
strategy (docs/BACKTESTING.md).

Reads bars from the database rather than the vendor: a backtest has to be
reproducible, and re-fetching means today's answer can differ from yesterday's
because the vendor restated something. Run `scripts/backfill_bars.py` first.

Thin on purpose. Argument handling, wiring the adapters and reporting live
here; the event loop, the cost models and the metrics live in `atp_core`, where
they can be tested without a database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from atp_core.backtest.costs import ZeroCostModel, alpaca_equities_default
from atp_core.backtest.engine import BacktestConfig, BacktestEngine, FixedQtySizer
from atp_core.config import get_settings
from atp_core.domain import Timeframe
from atp_core.errors import ATPError
from atp_core.logging import configure as configure_logging
from atp_core.logging import get_logger
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.risk.engine import RiskEngine
from atp_core.strategy import examples as _examples  # noqa: F401 — populates the registry
from atp_core.strategy import registry

if TYPE_CHECKING:
    from atp_core.domain import Bar

log = get_logger(__name__)

#: The metrics worth putting in front of a human, in the order docs/BACKTESTING.md
#: 'Reading the result' discusses them. The full set is in `--out`.
_HEADLINE = (
    ("total_return", "Total return", "pct"),
    ("cagr", "CAGR", "pct"),
    ("sharpe", "Sharpe", "num"),
    ("sortino", "Sortino", "num"),
    ("calmar", "Calmar", "num"),
    ("volatility", "Volatility (ann.)", "pct"),
    ("max_drawdown", "Max drawdown", "pct"),
    ("max_drawdown_duration_days", "  ...lasting (days)", "int"),
    ("num_trades", "Trades", "int"),
    ("win_rate", "Win rate", "pct"),
    ("profit_factor", "Profit factor", "num"),
    ("expectancy", "Expectancy / trade", "money"),
    ("avg_win", "Average win", "money"),
    ("avg_loss", "Average loss", "money"),
    ("largest_win", "Largest win", "money"),
    ("largest_loss", "Largest loss", "money"),
    ("avg_holding_period_hours", "Average hold (hours)", "num"),
    ("exposure_pct", "Time in market", "pct"),
    ("turnover", "Turnover (x equity)", "num"),
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strategy", required=True)
    p.add_argument("--symbols", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--timeframe", default="1d")
    p.add_argument("--cash", type=float, default=100_000)
    p.add_argument("--params", default="{}", help="JSON strategy params")
    p.add_argument(
        "--qty",
        type=int,
        default=100,
        help=(
            "shares per entry. A placeholder: real sizing is risk-based and "
            "arrives with the risk engine (docs/RISK.md 'Position sizing')"
        ),
    )
    p.add_argument("--zero-cost", action="store_true", help="debugging only")
    p.add_argument("--out", default=None, help="write full results to JSON")
    return p.parse_args(argv)


def _parse_day(value: str, field: str) -> datetime:
    """A calendar day at UTC midnight. Naive input is rejected at the boundary
    rather than silently assumed to mean UTC (rule §1.2)."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise SystemExit(f"--{field} must be YYYY-MM-DD, got {value!r}") from exc


def _format(value: object, kind: str) -> str:
    if isinstance(value, float) and math.isinf(value):
        # Real, and it means too few trades rather than a perfect strategy.
        return "     infinite"
    if kind == "pct":
        return f"{float(value):>13.2%}"  # type: ignore[arg-type]
    if kind == "money":
        return f"{float(value):>13,.2f}"  # type: ignore[arg-type]
    if kind == "int":
        return f"{int(value):>13,}"  # type: ignore[call-overload]
    return f"{float(value):>13.3f}"  # type: ignore[arg-type]


def _print_report(
    strategy: str,
    symbols: list[str],
    result: Any,
    metrics: dict[str, Any],
    fees: Decimal,
) -> None:
    print(f"\n{'═' * 58}")
    print(f"  {strategy}  ·  {', '.join(symbols)}  ·  {result.config.timeframe.value}")
    print(f"{'═' * 58}")
    print(f"  {'Starting equity':<32}{float(result.portfolio.starting_equity):>13,.2f}")
    print(f"  {'Ending equity':<32}{float(result.portfolio.equity):>13,.2f}")
    print(f"  {'Fees and commissions':<32}{float(fees):>13,.2f}")
    print(
        f"  {'Signals / orders / filled':<32}"
        f"{
            f'{len(result.signals)} / {len(result.orders)} / '
            f'{sum(1 for o in result.orders if o.filled_qty > 0)}':>13}"
    )
    print(f"{'─' * 58}")
    for key, label, kind in _HEADLINE:
        print(f"  {label:<32}{_format(metrics[key], kind)}")
    print(f"{'═' * 58}")

    if result.warnings:
        print(f"\n{len(result.warnings)} warning(s) during the run:")
        for warning in result.warnings[:10]:
            print(f"  {warning}")
        if len(result.warnings) > 10:
            print(f"  ... and {len(result.warnings) - 10} more")


def _jsonable(value: object) -> object:
    """Infinity is a legitimate metric value and not legal JSON.

    `json.dumps` would emit a bare `Infinity`, which most parsers reject — so a
    file written here would fail to load in exactly the tools meant to read it.
    """
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        return None
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


async def _load_bars(
    database_url: str, symbols: list[str], timeframe: Timeframe, start: datetime, end: datetime
) -> dict[str, list[Bar]]:
    engine = create_engine(database_url)
    try:
        repository = PostgresBarRepository(create_session_factory(engine))
        return {s: await repository.get_bars(s, timeframe, start, end) for s in symbols}
    finally:
        await engine.dispose()


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Everything judgeable from the arguments alone is judged before the config
    # layer loads, so a mistyped timeframe says so rather than surfacing as
    # whatever a machine with no .env happens to complain about first.
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("--symbols is empty")

    try:
        timeframe = Timeframe(args.timeframe)
    except ValueError:
        supported = ", ".join(t.value for t in Timeframe)
        raise SystemExit(f"--timeframe must be one of: {supported}") from None

    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--params must be valid JSON: {exc}") from None
    if not isinstance(params, dict):
        raise SystemExit(f"--params must be a JSON object, got {type(params).__name__}")

    if args.qty <= 0:
        raise SystemExit(f"--qty must be positive, got {args.qty}")
    if args.cash <= 0:
        raise SystemExit(f"--cash must be positive, got {args.cash}")

    start = _parse_day(args.start, "start")
    end = _parse_day(args.end, "end")
    if start >= end:
        raise SystemExit(f"--start must be before --end ({start.date()} >= {end.date()})")

    try:
        strategy_cls = registry.get(args.strategy)
    except ATPError as exc:
        raise SystemExit(str(exc)) from None

    try:
        settings = get_settings()
    except ValidationError as exc:
        raise SystemExit(
            f"Configuration is invalid — check .env against .env.example:\n\n{exc}"
        ) from None

    configure_logging(settings.log_level, settings.log_format)

    try:
        strategy = strategy_cls(params)
    except ATPError as exc:
        raise SystemExit(f"strategy rejected its params: {exc}") from None

    bars = await _load_bars(settings.database_url, symbols, timeframe, start, end)
    missing = [s for s, series in bars.items() if not series]
    if missing:
        raise SystemExit(
            f"No stored bars for {', '.join(missing)} in {start.date()} → {end.date()}. "
            f"Backfill first: scripts/backfill_bars.py --symbols {','.join(missing)} "
            f"--start {start.date()}"
        )

    # Three things this run is NOT evidence about, said before the numbers
    # rather than after, because a number a human has already read is a number
    # they have already believed. After the data check, so a run that cannot
    # start does not first explain how it would have been caveated.
    if args.zero_cost:
        print("WARNING: zero-cost model — results are NOT evidence about this strategy.")
    print(
        f"NOTE: sizing every entry at {args.qty} shares. Real sizing is risk-based "
        "and equalises risk per trade; until the risk engine lands, treat the "
        "return as a property of this share count (docs/RISK.md)."
    )
    print(
        "NOTE: no pre-trade risk rules are active — the rule chain is Phase 3. "
        "Orders are still routed through RiskEngine, but nothing refuses them."
    )

    log.info(
        "backtest.starting",
        strategy=strategy.name,
        symbols=len(symbols),
        timeframe=timeframe.value,
        bars=sum(len(s) for s in bars.values()),
    )

    engine = BacktestEngine(
        strategy=strategy,
        config=BacktestConfig(
            symbols=symbols,
            start=start,
            end=end,
            timeframe=timeframe,
            starting_cash=Decimal(str(args.cash)),
        ),
        cost_model=ZeroCostModel() if args.zero_cost else alpaca_equities_default(),
        # An explicit empty chain: `default_rules()` raises until Phase 3, and
        # an unguarded engine should be something a caller asked for in writing.
        risk_engine=RiskEngine(settings.risk, rules=[]),
        position_sizer=FixedQtySizer(Decimal(args.qty)),
    )

    try:
        result = engine.run(bars)
    except ATPError as exc:
        raise SystemExit(f"backtest failed: {exc}") from None

    fees = sum((o.total_fees for o in result.orders), Decimal(0))
    _print_report(strategy.name, symbols, result, result.metrics, fees)

    if result.metrics["num_trades"] < 30:
        print(
            f"\nOnly {result.metrics['num_trades']} trades. Under ~30 the statistics "
            "above mean very little (docs/BACKTESTING.md 'Reading the result')."
        )
    if result.metrics["sharpe"] > 3:
        print(
            f"\nSharpe of {result.metrics['sharpe']:.2f} on a simple strategy is a bug "
            "until proven otherwise. Check fill timing first, then data alignment."
        )

    if args.out:
        report = result.to_report()
        report["equity_curve"] = [[ts.isoformat(), str(eq)] for ts, eq in result.equity_curve]
        with Path(args.out).open("w", encoding="utf-8") as handle:
            json.dump(_jsonable(report), handle, indent=2, allow_nan=False)
        print(f"\nFull results written to {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
