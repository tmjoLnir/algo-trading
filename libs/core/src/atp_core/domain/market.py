"""Market data value objects: instruments, bars, quotes, trades.

All immutable. All prices `Decimal` (rule §1.1), all timestamps tz-aware UTC
(rule §1.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from atp_core.domain.enums import Timeframe


def _require_utc(ts: datetime, field: str) -> None:
    if ts.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware (rule §1.2), got naive {ts!r}")
    if ts.utcoffset() != UTC.utcoffset(None):
        raise ValueError(f"{field} must be UTC, got offset {ts.utcoffset()}")


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradable symbol and the venue rules that constrain orders on it."""

    symbol: str
    exchange: str = "NASDAQ"
    currency: str = "USD"
    tick_size: Decimal = Decimal("0.01")
    lot_size: Decimal = Decimal("1")
    fractionable: bool = False
    shortable: bool = True

    def __post_init__(self) -> None:
        if self.symbol != self.symbol.upper():
            raise ValueError(f"symbol must be uppercase, got {self.symbol!r}")

    def round_price(self, price: Decimal) -> Decimal:
        """Snap to the venue tick. Sending an off-tick price is a rejection."""
        return (price / self.tick_size).quantize(Decimal("1")) * self.tick_size


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV candle.

    `ts` is the bar's OPEN time; the bar covers [ts, ts + timeframe). A strategy
    is handed a bar only once that window has fully elapsed — see
    docs/BACKTESTING.md on lookahead bias.

    `close` is the raw traded price; `adj_close` is adjusted for splits and
    dividends. Backtest on adjusted, trade on raw (CLAUDE.md §5).
    """

    symbol: str
    ts: datetime
    timeframe: Timeframe
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    adj_close: Decimal | None = None
    vwap: Decimal | None = None
    trade_count: int | None = None

    def __post_init__(self) -> None:
        _require_utc(self.ts, "Bar.ts")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(
                f"inconsistent OHLC for {self.symbol} @ {self.ts}: "
                f"O={self.open} H={self.high} L={self.low} C={self.close}"
            )

    @property
    def close_ts(self) -> datetime:
        """When this bar finished — the earliest a strategy may act on it."""
        from datetime import timedelta

        return self.ts + timedelta(seconds=self.timeframe.seconds)

    @property
    def typical_price(self) -> Decimal:
        return (self.high + self.low + self.close) / 3

    @property
    def range(self) -> Decimal:
        return self.high - self.low


@dataclass(frozen=True, slots=True)
class Quote:
    """Top of book. The reference for marking positions and estimating fills."""

    symbol: str
    ts: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal = Decimal(0)
    ask_size: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        _require_utc(self.ts, "Quote.ts")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> Decimal:
        """Spread in basis points of mid — a liquidity guard worth checking
        before submitting a market order into a thin book."""
        mid = self.mid
        return (self.spread / mid) * 10_000 if mid else Decimal(0)


@dataclass(frozen=True, slots=True)
class Trade:
    """A print on the tape."""

    symbol: str
    ts: datetime
    price: Decimal
    size: Decimal
    conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_utc(self.ts, "Trade.ts")
