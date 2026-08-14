#!/usr/bin/env python
"""Seed a development database: reference instruments, a sample strategy, and a
little bar history so the dashboard has something to render.

Development only — refuses to run against a production database URL.
"""

from __future__ import annotations

import asyncio


async def main() -> None:
    raise NotImplementedError(
        "Insert a few instruments (SPY, QQQ, AAPL), the sma_crossover strategy "
        "in draft state, and ~1 year of daily bars."
    )


if __name__ == "__main__":
    asyncio.run(main())
