#!/usr/bin/env python
"""Run a backtest from the CLI.

    uv run python scripts/run_backtest.py --strategy sma_crossover \
      --symbols SPY --start 2020-01-01 --end 2024-12-31

Defaults to a realistic cost model. `--zero-cost` exists for debugging engine
mechanics and prints a warning — a zero-cost result is not evidence about a
strategy (docs/BACKTESTING.md).
"""

from __future__ import annotations

import argparse
import asyncio


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strategy", required=True)
    p.add_argument("--symbols", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--timeframe", default="1d")
    p.add_argument("--cash", type=float, default=100_000)
    p.add_argument("--params", default="{}", help="JSON strategy params")
    p.add_argument("--zero-cost", action="store_true", help="debugging only")
    p.add_argument("--out", default=None, help="write full results to JSON")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    if args.zero_cost:
        print("WARNING: zero-cost model — results are NOT evidence about this strategy.")
    raise NotImplementedError("Load bars, build the strategy, run BacktestEngine, print metrics.")


if __name__ == "__main__":
    asyncio.run(main())
