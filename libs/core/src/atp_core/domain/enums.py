"""Domain enumerations.

These mirror the vocabulary a broker uses. Adapters translate between these and
venue-specific strings; nothing outside `brokers/` should know Alpaca's spelling.
"""

from __future__ import annotations

from enum import StrEnum


class RunMode(StrEnum):
    """How the platform is executing.

    The same strategy code runs in all three. Only the bound adapters differ,
    which is what makes a backtested strategy trustworthy in paper and live.
    """

    BACKTEST = "backtest"  # simulated clock, historical bars, simulated fills
    PAPER = "paper"  # real clock, live data, simulated money
    LIVE = "live"  # real clock, live data, REAL MONEY


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """+1 for buy, -1 for sell — for converting to a signed position delta."""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"  # becomes a market order when stop_price trades
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"


class TimeInForce(StrEnum):
    DAY = "day"  # cancelled at session close
    GTC = "gtc"  # good til cancelled
    IOC = "ioc"  # fill what you can immediately, cancel the rest
    FOK = "fok"  # all or nothing, immediately
    OPG = "opg"  # opening auction
    CLS = "cls"  # closing auction


class OrderStatus(StrEnum):
    """Order lifecycle. Transitions are enforced by `execution.state`."""

    PENDING_RISK = "pending_risk"  # created, not yet risk-approved
    REJECTED_RISK = "rejected_risk"  # our own risk engine refused it
    PENDING_SUBMIT = "pending_submit"  # approved, not yet acknowledged by broker
    SUBMITTED = "submitted"  # broker has it
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"  # broker refused it
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_open(self) -> bool:
        """Still working at the venue — counts toward exposure."""
        return self in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}


_TERMINAL = frozenset(
    {
        OrderStatus.REJECTED_RISK,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)


class SignalAction(StrEnum):
    """What a strategy wants to happen. Deliberately not an order — the risk
    engine and position sizer turn intent into a concrete order."""

    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"  # close whatever position exists
    SCALE_IN = "scale_in"
    SCALE_OUT = "scale_out"
    HOLD = "hold"


class StopType(StrEnum):
    FIXED_PCT = "fixed_pct"  # x% below entry
    FIXED_AMOUNT = "fixed_amount"  # absolute price distance
    TRAILING_PCT = "trailing_pct"  # x% below the high-water mark
    ATR = "atr"  # n × Average True Range — volatility-adaptive
    TIME = "time"  # exit after n bars regardless of price
    CHANDELIER = "chandelier"  # highest-high − n × ATR


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def seconds(self) -> int:
        return _TIMEFRAME_SECONDS[self]


_TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
}


class StrategyState(StrEnum):
    DRAFT = "draft"
    BACKTESTING = "backtesting"
    PAPER = "paper"
    LIVE = "live"
    PAUSED = "paused"
    HALTED = "halted"  # stopped by the risk engine, needs human clearance
