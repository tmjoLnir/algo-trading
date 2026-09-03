"""The account-wide ceilings, as one validated value object.

Every one of these was a `RISK_*` environment variable until this module
existed, and the move is the same one `atp_core.worker.config` made for the ten
trading parameters, for the same three reasons. An `.env` file needs shell
access on the host to change, records nothing about who changed it, and cannot
be read by the API — so "why did that order get refused, and who set the
ceiling it hit" had no answer beyond asking whoever deployed last. They are now
stored beside the worker's configuration, edited on the dashboard, and every
save is one audit row naming the operator and the numbers on both sides.

**These are ceilings, not targets.** A strategy may configure something
tighter; it can never configure something looser. They are the last line of
defence before a bug becomes a loss, which is why the validation below refuses
a value rather than clamping it — a limit that silently became something other
than what was typed is worse than one that would not save.

**Two processes enforce these, and they pick the values up at different
moments.** That is not new and it is worth stating plainly, because moving the
values out of `.env` changed one half of it:

- The **worker** builds its `RiskEngine` once, at start (`trading.build_live`),
  so an edit binds it only at its next restart. The dashboard already has the
  machinery to say so — the saved row carries a revision, the worker publishes
  the revision it booted with, and the screen renders the difference.
- The **API** builds a router per request (`atp_api.execution.build_router`),
  so a manual order placed from the dashboard is measured against the row as
  saved, immediately.

Before this module both halves were frozen until both processes restarted, so
the API half is strictly more responsive than it was. The screen says which is
which rather than leaving a reader to assume they agree.

**Why here and not in `atp_core.worker`.** `WorkerConfig` is what one worker
trades; these bind every order this platform places, including one an operator
types into the dashboard while no worker is running. They travel in the same
stored row because they are saved by the same person in the same act — but the
risk package is what owns the rules, and `RiskRule.check` takes one of these.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from atp_core.errors import ConfigError

#: The most of the account one symbol may become. One, not more: a position
#: larger than the whole account is not a limit an operator meant to type.
MAX_POSITION_CEILING = Decimal("1")

#: The most gross exposure this will store. Four is Reg-T's intraday margin
#: multiple, so it is the widest number a US equities account can legitimately
#: run at; the default is 1.00, which is no leverage at all.
MAX_GROSS_CEILING = Decimal("4")

#: A daily loss limit is a fraction of equity, so it cannot exceed all of it.
MAX_DAILY_LOSS_CEILING = Decimal("1")

#: A stop is a distance below entry expressed as a fraction, so a value at or
#: above 1 is a price level below zero — a stop that can never be hit, which is
#: the same as having no stop at all.
MAX_STOP_LOSS = Decimal("1")

#: A take-profit target *can* legitimately exceed 100% of entry, so this is a
#: typo guard rather than a rule: ten times entry is a misplaced decimal point,
#: not a plan.
MAX_TAKE_PROFIT = Decimal("10")


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Account-wide hard ceilings.

    Frozen for the reason `WorkerConfig` is: a running evaluation must not have
    its limits change underneath it halfway through. A saved edit produces a new
    object that the next request — or the next worker start — reads.

    Every default here is the value the environment variable it replaced
    shipped with in `.env.example`, so a deployment that upgrades and saves
    nothing behaves exactly as it did.
    """

    #: The most of the account one symbol may become.
    max_position_pct: Decimal = Decimal("0.10")
    #: Total exposure across every position. 1.00 is no leverage.
    max_gross_exposure_pct: Decimal = Decimal("1.00")
    #: The day's drawdown at which trading halts, measured against the equity
    #: the session opened on.
    max_daily_loss_pct: Decimal = Decimal("0.03")
    #: Runaway-loop protection. Counted in the worker's own process, on the
    #: attempt rather than on the fill.
    max_orders_per_minute: int = 30
    #: More open positions than one person can watch is its own risk.
    max_open_positions: int = 20
    #: A quote older than this is not a quiet market, it is a dead feed.
    #: Necessarily tighter than `WorkerConfig.max_silence_seconds`, which is how
    #: long the *feed* may go quiet: a single symbol can legitimately go a
    #: minute without printing, but an order must not be priced off a quote that
    #: old.
    max_quote_age_seconds: int = 30
    #: A fallback, not a recommendation: docs/RISK.md is explicit that a fixed
    #: percentage stop is too tight on a volatile name and too loose on a dull
    #: one, and that ATR-based stops are the default.
    default_stop_loss_pct: Decimal = Decimal("0.02")
    default_take_profit_pct: Decimal = Decimal("0.06")

    def __post_init__(self) -> None:
        """Refuse anything that is not a limit, at construction.

        Raises `ConfigError` with a sentence naming the field and what is wrong
        with it, because all three callers put that sentence in front of a
        person: the API as a 400 body, the worker as the reason it would not
        start, and `make check-env` as a line about a row that will not load.

        The rules live here rather than at either end so that the API and the
        worker cannot disagree — a ceiling the dashboard accepts and the worker
        refuses would save cleanly and then kill the process at its next start,
        which is the worst of the three available behaviours.
        """
        self._check_fraction("max_position_pct", self.max_position_pct, MAX_POSITION_CEILING)
        self._check_fraction(
            "max_gross_exposure_pct", self.max_gross_exposure_pct, MAX_GROSS_CEILING
        )
        self._check_fraction("max_daily_loss_pct", self.max_daily_loss_pct, MAX_DAILY_LOSS_CEILING)
        self._check_fraction(
            "default_stop_loss_pct",
            self.default_stop_loss_pct,
            MAX_STOP_LOSS,
            # The one exclusive bound here. A stop 100% below entry is the
            # price zero, which is not a stop that can be hit — and 1 is a
            # plausible typo for 0.1 in a box whose other rows accept it.
            exclusive=True,
        )
        self._check_fraction(
            "default_take_profit_pct", self.default_take_profit_pct, MAX_TAKE_PROFIT
        )
        self._check_count("max_orders_per_minute", self.max_orders_per_minute)
        self._check_count("max_open_positions", self.max_open_positions)
        self._check_count("max_quote_age_seconds", self.max_quote_age_seconds)

        # A single symbol allowed to exceed the whole book's ceiling is not
        # dangerous — the tighter rule wins, and `max_gross_exposure` would
        # refuse what `max_position_size` let through — but it is incoherent,
        # and the operator who typed it believes they have a position limit
        # they do not have. Refused here rather than left to be discovered in a
        # rejection reason.
        if self.max_position_pct > self.max_gross_exposure_pct:
            raise ConfigError(
                f"max_position_pct of {self.max_position_pct} lets one symbol exceed "
                f"max_gross_exposure_pct of {self.max_gross_exposure_pct}, which caps the whole "
                "book — the gross limit would refuse first, so the position limit you typed is "
                "not the one in force"
            )

    @staticmethod
    def _check_fraction(
        name: str, value: Decimal, ceiling: Decimal, *, exclusive: bool = False
    ) -> None:
        """A fraction of equity: positive, finite, and no wider than `ceiling`.

        Zero is refused rather than read as "off". A zero position limit
        refuses every order and a zero daily-loss limit halts on the first
        cent — both are configurations that stop the platform dead, and an
        operator who wants that has a kill switch that says so on the screen.

        `exclusive` is for the one bound that must not be reached rather than
        not exceeded: a stop expressed as a fraction *of* the entry price is a
        level at zero when the fraction is 1, and a level below zero past it.
        """
        if not value.is_finite():
            raise ConfigError(f"{name} must be a finite number, got {value}")
        if value <= 0:
            raise ConfigError(
                f"{name} must be greater than zero, got {value} — zero is not 'no limit', "
                "it is a limit nothing can satisfy"
            )
        if value > ceiling or (exclusive and value == ceiling):
            raise ConfigError(
                f"{name} of {value} is {value:%} of the value it measures; this refuses "
                f"anything {'at or above' if exclusive else 'above'} {ceiling:%}. "
                "Fractions are written as 0.10 for 10%."
            )

    @staticmethod
    def _check_count(name: str, value: int) -> None:
        """A count or a duration: at least one of whatever it counts."""
        if value < 1:
            raise ConfigError(
                f"{name} must be at least 1, got {value} — zero would refuse every order "
                "rather than lifting the limit"
            )


#: What the platform runs on when nothing has been saved. Identical to the
#: values `.env.example` shipped as `RISK_*`, so an existing deployment that
#: upgrades and saves nothing keeps exactly the ceilings it had.
DEFAULT_RISK_LIMITS = RiskLimits()


@dataclass(frozen=True, slots=True)
class LimitField:
    """One entry box on the dashboard, carrying its own prose.

    The same argument `SelectOption` makes for the stop dropdown: a number
    labelled `max_gross_exposure_pct` tells a reader nothing about what 1.00
    means, and docs/RISK.md's answer belongs on the screen where the number is
    typed rather than one document away.

    `unit` is what the form renders beside the box — `fraction` gets a percent
    hint, the rest get their noun — and `maximum` is the same ceiling
    `__post_init__` enforces, sent so the browser refuses before the server has
    to. It is a convenience and never the authority: the value object is.
    """

    name: str
    label: str
    unit: str
    help: str
    #: `None` for a count, which is bounded below only.
    maximum: Decimal | None = None


#: Every ceiling, in the order the risk chain checks them — which is the order
#: `_CEILINGS` in the risk router lists them and therefore the order an
#: operator has already read them in on the risk panel. The two defaults come
#: last because they are fallbacks rather than limits.
RISK_LIMIT_FIELDS: tuple[LimitField, ...] = (
    LimitField(
        "max_position_pct",
        "Max position",
        "fraction",
        "The most of the account one symbol may become. 0.10 is 10%. Enforced by "
        "max_position_size, which refuses the whole order rather than trimming it.",
        MAX_POSITION_CEILING,
    ),
    LimitField(
        "max_gross_exposure_pct",
        "Max gross exposure",
        "fraction",
        "Total exposure across every position, long and short. 1.00 is fully invested "
        "and no leverage, which is the default; above 1 is margin.",
        MAX_GROSS_CEILING,
    ),
    LimitField(
        "max_daily_loss_pct",
        "Max daily loss",
        "fraction",
        "Trading halts for the day once the book is this far below the equity the "
        "session opened on. 0.03 is 3%. Exits are never blocked by it — a rule that "
        "refused an exit would trap you in the losing position.",
        MAX_DAILY_LOSS_CEILING,
    ),
    LimitField(
        "max_open_positions",
        "Max open positions",
        "positions",
        "More concurrent positions than one person can watch is its own risk, "
        "independent of what any one of them is worth.",
    ),
    LimitField(
        "max_orders_per_minute",
        "Max orders per minute",
        "orders/min",
        "Runaway-loop protection. Counted on the attempt rather than the fill, so a "
        "strategy looping on rejections trips it.",
    ),
    LimitField(
        "max_quote_age_seconds",
        "Max quote age",
        "seconds",
        "An order priced off a quote older than this is refused: at that point it is "
        "not a quiet market, it is a dead feed. Keep it well below the worker's feed "
        "silence budget, which times a whole watchlist rather than one symbol.",
    ),
    LimitField(
        "default_stop_loss_pct",
        "Default stop loss",
        "fraction",
        "The fallback protective stop for a strategy that names none, as a fraction "
        "below entry — 0.02 is 2%, and it must stay below 1, since a whole entry price "
        "is a stop at zero. A fallback and not a recommendation: docs/RISK.md prefers "
        "ATR stops, because a fixed percentage is too tight on a volatile name and too "
        "loose on a dull one.",
        MAX_STOP_LOSS,
    ),
    LimitField(
        "default_take_profit_pct",
        "Default take profit",
        "fraction",
        "The fallback profit target, same reasoning. 0.06 is 6% above entry.",
        MAX_TAKE_PROFIT,
    ),
)


def parse_limit_decimal(raw: str | Decimal | int | float, *, field_name: str) -> Decimal:
    """A `Decimal` from whatever the wire carried, refusing junk by name.

    Via `str` for a float on the off chance one reaches here: these fractions
    are multiplied by equity to produce the ceiling an order is measured
    against, and `Decimal(0.1)` is not `Decimal("0.1")` (rule §1.1).
    """
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"{field_name} is not a number: {raw!r}") from exc
