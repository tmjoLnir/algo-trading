"""Exception hierarchy.

Everything raised on purpose derives from `ATPError`, so callers can distinguish
a domain refusal from a genuine bug. Never put a credential in a message.
"""

from __future__ import annotations


class ATPError(Exception):
    """Base for every deliberate error in the platform."""


# ── configuration ───────────────────────────────────────────────────────────
class ConfigError(ATPError): ...


class MissingBrokerCredentialsError(ConfigError):
    """A live/paper broker adapter was asked for without an API key.

    Raised where the credential is actually needed — building the adapter —
    rather than where configuration is *read*. `Settings` used to refuse to
    validate at all in this case, which meant the API and the worker could not
    import, so a missing key presented as a process that would not start rather
    than as a broker that could not be built. See `Settings.broker_configured`.
    """


# ── persistence ─────────────────────────────────────────────────────────────
class PersistenceError(ATPError): ...


class DatabaseUnavailableError(PersistenceError):
    """The database could not be reached, or dropped the connection mid-request.

    Raised by `atp_core.persistence.db` in place of whatever the driver threw,
    and deliberately NOT raised for a statement that failed. The distinction is
    the whole point of the type: "Postgres would not let this process in" and
    "this query is wrong" are the same shape of traceback and opposite kinds of
    problem — the first is an outage somebody has to go and fix, the second is a
    bug in this repository — and a caller that cannot tell them apart reports
    both as the second. Which is what the API did: an operator watching every
    panel answer `500 Internal Server Error` was being told, by a platform that
    knew better, that it had broken.

    The driver's own exception is the `__cause__`. It is not folded into this
    message and it never reaches an HTTP response: a connection error is free to
    quote the DSN it failed to connect with, and the DSN carries the password
    (CLAUDE.md §1.6). The type name is safe and is most of the diagnosis
    anyway — `InvalidPasswordError` says what went wrong without saying what the
    password is.
    """

    def __init__(self, cause: BaseException) -> None:
        #: The driver exception's class name — `InvalidPasswordError`,
        #: `ConnectionRefusedError`. Named rather than reconstructed from
        #: `__cause__` so a log line can carry it without walking the chain.
        self.cause_type = type(cause).__name__
        super().__init__(f"the database is unreachable ({self.cause_type})")


# ── market data ─────────────────────────────────────────────────────────────
class DataError(ATPError): ...


class DataGapError(DataError):
    """A required stretch of history is missing.

    Fail loudly rather than backtesting over a hole — a gap silently treated as
    "no price movement" produces a flattering, fictional equity curve.
    """


class StaleDataError(DataError):
    """The latest quote is older than the freshness budget. Do not trade on it."""


class UnadjustedDataError(DataError):
    """Bars were supplied without the adjusted closes a backtest prices off.

    Raised rather than falling back to the raw close, because the fallback is
    silent and its symptom is a plausible number: an unapplied 4:1 split reads
    as a position losing 75% overnight, and an unapplied 1:8 reverse split reads
    as one earning 700%. Neither announces itself in a result — see CLAUDE.md §5
    and docs/adr/0017-backtests-price-off-adjusted-closes.md.
    """


# ── broker ──────────────────────────────────────────────────────────────────
class BrokerError(ATPError): ...


class BrokerConnectionError(BrokerError):
    """Transport failure. Retryable — but only with the same client_order_id."""


class OrderRejectedError(BrokerError):
    """The venue refused the order (buying power, halted symbol, bad price)."""


class InsufficientFundsError(OrderRejectedError):
    """The venue refused the order for want of buying power.

    **A subclass of `OrderRejectedError` rather than a sibling of it**, and that
    relationship is load-bearing rather than taxonomic. It was a sibling, so
    `OrderRouter._route`'s `except OrderRejectedError` did not catch it — and on
    day 3 of the paper week a wash-trade rejection that the adapter had
    misclassified as this class escaped a router that handles refusals perfectly
    well, unwound `submit_protective_orders`, `_protect` and `on_fill_event`,
    ended the trade-updates consumer, and killed the worker
    (docs/paper-week/day-3-review.md, B2).

    Running out of money *is* the venue saying no. Every caller that handles a
    "no" must handle this one, and a type relationship is the only place that
    can guarantee it — a second `except` clause somewhere is a thing the next
    call site forgets.
    """


# ── risk ────────────────────────────────────────────────────────────────────
class RiskError(ATPError): ...


class KillSwitchUnavailableError(RiskError):
    """The halt could not be recorded.

    Raised rather than returned, and never swallowed. `engage` is the platform's
    stop button: a caller that believes it halted trading and did not is the one
    outcome worse than an exception, because every later decision is taken on
    the assumption that the book is frozen.

    **`halt_stands` is the field a handler must branch on**, because two opposite
    states raise this and an operator acts differently on each. True means every
    contended round found the key occupied: the store answered, a halt is
    standing, and what failed to land is this call's reason and its impugnment.
    False means the last round found the key cleared under us: nothing was
    written and no halt may be in force. Reporting the second as the first tells
    someone to walk away from a live account, which is the more dangerous of the
    two errors — so it is carried as a field rather than left for a caller to
    parse out of the message.
    """

    def __init__(self, message: str, *, halt_stands: bool) -> None:
        self.halt_stands = halt_stands
        super().__init__(message)


class RiskLimitBreachedError(RiskError):
    """A pre-trade check failed. Expected in normal operation, not a bug."""

    def __init__(self, rule: str, detail: str) -> None:
        self.rule = rule
        self.detail = detail
        super().__init__(f"risk rule '{rule}' blocked the order: {detail}")


class KillSwitchEngagedError(RiskError):
    """Trading is halted platform-wide. Requires explicit human clearance."""


# ── strategy / backtest ─────────────────────────────────────────────────────
class StrategyError(ATPError): ...


class InvalidRuleError(StrategyError):
    """A declarative rule spec failed validation."""


class StrategyExistsError(StrategyError):
    """A strategy is already stored under this name.

    Its own type rather than a generic integrity failure, because the caller's
    response differs: a duplicate name is the author's to fix and belongs in
    front of them as a 409, while an integrity error nothing anticipated is a
    bug and belongs in a log. Names are not incidental here — a strategy's name
    IS its primary key, and every `Signal.strategy_id` carries it — so two
    strategies sharing one would merge their attribution rather than collide
    visibly.
    """


class BacktestError(ATPError): ...


class LookaheadError(BacktestError):
    """A strategy tried to read data it could not have known at decision time.

    Raised by the backtest engine's guard. This is never a false positive worth
    suppressing — it means the backtest's results are not real.
    """


# ── execution ───────────────────────────────────────────────────────────────
class ExecutionError(ATPError): ...


class ReconciliationDivergedError(ExecutionError):
    """The stored book and the broker's disagree, and no code here can repair it.

    Its own class so that the one caller who can do something sensible with it —
    `StrategyRunner.run` — can tell it from a bug. Refusing to trade against a
    book the broker contradicts is right, is docs/SAFETY.md's whole posture, and
    is not what changed.

    *Exiting the process* on one is what changed. A divergence is identical on
    every boot, nothing in the loop repairs it, and `restart: unless-stopped`
    carries no attempt cap — so day 3 of the paper week died on this condition
    **129 consecutive times**, a median 16.9 seconds apart, paging a human on
    each one (docs/paper-week/day-3-review.md, B2 and F1). A guard that cannot
    be satisfied must not be a restart loop.
    """


class InvalidStateTransitionError(ExecutionError):
    def __init__(self, from_status: str, to_status: str) -> None:
        super().__init__(f"illegal order transition {from_status} → {to_status}")


class ReconciliationError(ExecutionError):
    """Our view of positions/orders disagrees with the broker's.

    Always page a human: continuing to trade against a wrong picture of the book
    is worse than stopping.
    """
