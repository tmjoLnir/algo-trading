#!/usr/bin/env python
"""Backfill historical bars.

    uv run python scripts/backfill_bars.py --symbols AAPL,MSFT --start 2020-01-01

Idempotent — safe to re-run over a range you already have. Rate-limited to stay
under the vendor's 200 req/min, and paginates to exhaustion (silently taking the
first page is a data gap that looks like data).
"""

from __future__ import annotations

import argparse
import asyncio


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", required=True, help="comma-separated tickers")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--timeframe", default="1d")
    p.add_argument("--adjusted", action="store_true", default=True)
    p.add_argument("--verify", action="store_true", help="report gaps afterwards")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    raise NotImplementedError(
        "Fetch via AlpacaHistoricalProvider, upsert via BarRepository, then "
        "run find_gaps() when --verify. See docs/DATA.md."
    )


if __name__ == "__main__":
    asyncio.run(main())
