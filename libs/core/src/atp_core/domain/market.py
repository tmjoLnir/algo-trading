"""Market data value objects: instruments, bars, quotes, trades.

All immutable. All prices `Decimal` (rule §1.1), all timestamps tz-aware UTC
(rule §1.2).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
        """When this bar finished — the earliest a strategy may act on it.

        **An upper bound for a daily bar, not the session close.** A daily bar
        is stamped at exchange-local midnight, so `ts + 24h` is the next
        midnight rather than the 21:00 UTC close — later than the session
        actually ended, and on the following calendar day. That is safe for the
        one thing this answers, because acting later than permitted cannot
        create lookahead, and it is wrong for anything that reads it as a
        *label*: a daily bar's `close_ts` names a day the market was shut.

        A `Bar` carries no exchange, so it cannot resolve its own session close
        without a calendar — and `domain` imports nothing from its siblings.
        Anything that needs to name the session a bar belongs to should use
        `ts`, which is that name. See ADR 0018.
        """
        from datetime import timedelta

        return self.ts + timedelta(seconds=self.timeframe.seconds)

    @property
    def is_adjusted(self) -> bool:
        """True when this bar's prices already sit in adjusted space.

        The marker is `adj_close == close`, which is what `adjusted()` leaves
        behind and what a symbol with no corporate actions has anyway. It makes
        the conversion idempotent, so a series can be adjusted twice without
        being scaled twice.
        """
        return self.adj_close is not None and self.adj_close == self.close

    def adjusted(self) -> Bar:
        """This bar with every price moved into split/dividend-adjusted space.

        **The whole candle moves, not just the close.** Swapping `close` for
        `adj_close` at each read site would leave a backtest marking positions
        at adjusted prices while filling them at raw opens — a mismatch of the
        split factor between two numbers that must be in the same currency, and
        one that gets worse the further back the bar is. Scaling O/H/L/C by the
        single factor `adj_close / close` keeps the candle internally consistent
        and continuous across the corporate action, which is the whole point.

        Volume moves the other way, because a split changes the share count as
        well as the price: a 4:1 split quarters the price and quadruples the
        shares, so dividing by the same factor holds the bar's traded notional
        fixed. The backtest caps fills at a fraction of bar volume, and a
        participation limit measured in pre-split shares against post-split
        prices is wrong by exactly the split ratio.

        Raises `ValueError` when `adj_close` is unset — the caller decides what
        an unadjusted series means, and for a backtest the answer is to refuse
        the run (`errors.UnadjustedDataError`). Defaulting it to `close` here
        would reintroduce the silent fallback this method exists to remove.

        Ordering is preserved: `Decimal` multiplication is correctly rounded and
        therefore monotonic, so `low <= open,close <= high` still holds of the
        result and `__post_init__` cannot reject what this returns.
        """
        if self.adj_close is None:
            raise ValueError(
                f"{self.symbol} @ {self.ts} has no adj_close, so it cannot be adjusted; "
                f"backfill the range without --raw-only"
            )
        if self.close <= 0:
            raise ValueError(
                f"cannot adjust {self.symbol} @ {self.ts}: the close is {self.close}, "
                f"so the adjustment factor is undefined"
            )
        if self.is_adjusted:
            return self

        factor = self.adj_close / self.close
        return replace(
            self,
            open=self.open * factor,
            high=self.high * factor,
            low=self.low * factor,
            close=self.close * factor,
            volume=self.volume / factor,
            # Its own close rather than the vendor's figure, so the result
            # satisfies `is_adjusted` exactly. The two differ only in the last
            # places of `Decimal`'s 28 significant digits, and carrying a
            # close that is a hair away from `adj_close` would make a second
            # `adjusted()` call scale the bar a second time.
            adj_close=self.close * factor,
            vwap=None if self.vwap is None else self.vwap * factor,
        )

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
