"""What a worker trades and what the platform will let it risk, as one object.

Every one of these was an environment variable until this module existed, and
the move is not cosmetic. An `.env` file is read once at process start, is not
readable by the API, is not versioned, and records nothing about who wrote a
value — so "why is it risking 2% a trade" had no answer beyond asking whoever
had shell access last. Here they are one validated object with a revision, a
timestamp and an author, stored where the dashboard can edit it and the audit
log can remember it.

**Validation lives here, not at either end.** The API refuses a bad edit and
the worker refuses to start on a bad row, and both refusals must agree — a
stop period the API accepts and the worker rejects is a configuration that
saves cleanly and then kills the process on the next restart, which is the
worst of the three possible behaviours. So the rules are written once, in
`__post_init__`, and both callers get them by constructing the object.

**The risk ceilings travel with it.** `RiskLimits` was the other half of the
`.env` trading configuration and it moved here for the same three reasons, into
the same row, saved by the same request. One save, one revision, one audit
entry: an operator who widens a stop and lifts the position limit in one sitting
made one decision, and a post-mortem should read it as one. The ceilings
themselves live in `atp_core.risk.limits`, because the risk package owns the
rules that enforce them and they bind orders this worker never placed — see that
module for which process picks up an edit when.

**What is deliberately NOT here.** `ATP_RUN_MODE`, `ATP_ALLOW_LIVE_TRADING`,
the broker credentials and the datastore URLs stay in `Settings`. Those say
what the process *is*; changing one is a deploy, and putting the first two
behind a web form would make the whole live-trading ratchet a single request.
`allow_live_orders` is the one live control that does live here, and it is the
only field the API demands a password for — see the API's `PUT /worker/config`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal, get_args

from atp_core.errors import ConfigError
from atp_core.risk.limits import DEFAULT_RISK_LIMITS, STORED_DECIMAL_PLACES, RiskLimits

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

#: The sizing methods `PositionSizeSpec` accepts. Restated as a `Literal` rather
#: than imported from it because this is the *stored* vocabulary: a value that
#: has been written to a row must stay loadable, so the two lists are kept in
#: step by `tests/unit/test_worker_config.py` rather than by an import that
#: would silently widen this one the day the spec grows a method.
type SizingMethod = Literal[
    "fixed_qty", "fixed_notional", "equity_pct", "risk_pct", "volatility_target"
]

#: The stop families `StopType` names, same reasoning.
type StopTypeName = Literal[
    "fixed_pct", "fixed_amount", "trailing_pct", "atr", "time", "chandelier"
]

#: A ticker as every column in this platform spells one: uppercase, and inside
#: the `String(20)` the bar and order tables give it. Identical to the strategies
#: router's, and for the same reason — `Instrument` refuses a lowercase symbol
#: one layer down, so accepting `spy` here only defers the failure to the first
#: subscription.
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,19}$")

#: Longest watchlist this accepts. Not a market-data limit — Alpaca's IEX feed
#: takes far more — but a typo guard: a paste that turns into three hundred
#: symbols spends one connection on a universe nobody chose, which is the
#: accident an empty default already exists to prevent.
MAX_SYMBOLS = 100

#: The widest a strategy name can be, because `strategies.id` is `String(36)`
#: and a strategy's id is its name.
MAX_STRATEGY_NAME = 36


@dataclass(frozen=True, slots=True)
class SelectOption:
    """One entry in a dashboard dropdown.

    Carries its own prose. A `<select>` of six stop types tells a reader
    nothing about which to pick, and docs/RISK.md's argument for ATR over a
    fixed percentage is exactly the kind of thing that should be on the screen
    where the choice is made rather than one document away.
    """

    value: str
    label: str
    help: str


#: How orders are sized, in the order a reader should consider them: the
#: recommended one first.
SIZING_METHODS: tuple[SelectOption, ...] = (
    SelectOption(
        "risk_pct",
        "Risk % of equity",
        "Size so that hitting the stop loses this fraction of equity. docs/RISK.md's "
        "default at 0.01 — it keeps risk per trade constant across instruments of "
        "wildly different volatility. Needs a stop to measure against.",
    ),
    SelectOption(
        "equity_pct",
        "% of equity",
        "Spend this fraction of equity on the position, regardless of where the stop is. "
        "Simple, and risks more on a volatile name than a dull one.",
    ),
    SelectOption(
        "fixed_notional",
        "Fixed cash amount",
        "Spend this many units of the quote currency on every entry.",
    ),
    SelectOption(
        "fixed_qty",
        "Fixed quantity",
        "Buy this many shares every time, whatever they cost.",
    ),
    SelectOption(
        "volatility_target",
        "Volatility target",
        "Size to a target portfolio volatility. The value is a fraction of equity.",
    ),
)

#: The protective stop armed on every entry.
STOP_TYPES: tuple[SelectOption, ...] = (
    SelectOption(
        "atr",
        "ATR multiple",
        "Distance is this many Average True Ranges from entry. docs/RISK.md's "
        "recommendation over a fixed percentage, which is too tight on a volatile "
        "name and too loose on a dull one. Uses the period below.",
    ),
    SelectOption(
        "chandelier",
        "Chandelier",
        "Highest high since entry, less this many ATRs. A trailing stop that "
        "ratchets up behind a winner. Uses the period below.",
    ),
    SelectOption(
        "fixed_pct",
        "Fixed %",
        "Distance is this fraction below entry — 0.02 is 2%.",
    ),
    SelectOption(
        "trailing_pct",
        "Trailing %",
        "This fraction below the high-water mark rather than below entry.",
    ),
    SelectOption(
        "fixed_amount",
        "Fixed price distance",
        "Distance is this many units of the quote currency below entry.",
    ),
    SelectOption(
        "time",
        "Time exit",
        "Exit after this many bars regardless of price. The multiplier is unused.",
    ),
)

#: The stop families whose number is a *multiple* rather than a distance. The
#: same field carries both, because giving each its own would let an operator
#: fill in the one the configured type ignores — the reasoning
#: `resolve_stop_config` has always carried, now stated where the field is.
MULTIPLIER_STOPS = frozenset({"atr", "chandelier"})

#: The stop families that read a lookback period. `time` reads it as a bar
#: count; ATR and chandelier as the ATR window. The rest ignore it.
PERIOD_STOPS = frozenset({"atr", "chandelier", "time"})

#: The stop families whose number is a fraction of the entry price. Bounded
#: below 1 for the reason `_check_stop` states.
FRACTIONAL_STOPS = frozenset({"fixed_pct", "trailing_pct"})

#: A backstop on the sizing value, mirroring `PositionSizeSpec.MAX_RISK_PCT` and
#: its argument: docs/RISK.md gives 0.5–2% risk per trade, so an order of
#: magnitude past that is a misplaced decimal point rather than a choice. Kept
#: here as well as there so the dashboard refuses it at the point of typing
#: rather than at the worker's next boot.
MAX_FRACTIONAL_SIZING = Decimal("0.10")

#: Methods whose `value` is a fraction of equity and therefore cannot exceed 1.
FRACTIONAL_SIZING = frozenset({"equity_pct", "risk_pct", "volatility_target"})


#: The accepted values, unpacked from the aliases above so the two cannot drift.
#: A `type` statement holds its right-hand side lazily, hence `__value__`.
_SIZING_VALUES: frozenset[str] = frozenset(get_args(SizingMethod.__value__))
_STOP_VALUES: frozenset[str] = frozenset(get_args(StopTypeName.__value__))


def _fail(message: str) -> ConfigError:
    return ConfigError(message)


def _check_storable(name: str, value: Decimal) -> None:
    """Refuse a number the row cannot hold at the precision it was typed.

    The same guard `RiskLimits._check_fraction` applies, for the same reason and
    on the same row: `NUMERIC(20, 8)` *rounds* rather than refusing, and
    `_to_stored` rebuilds this object from what came back — so a value accepted
    here and rounded on the way in can land outside its own bound and make the
    row unloadable by everything that reads it, including the endpoint that
    would repair it. `sizing_value = 0.000000001` is accepted as positive,
    stores as zero, and then fails "must be positive" on every read forever.

    Applied to these two as well as to the ceilings because a half-guarded row
    is still a brickable row, and both fields are reached from the same form.
    """
    if not value.is_finite():
        raise _fail(f"{name} must be a finite number, got {value}")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > STORED_DECIMAL_PLACES:
        raise _fail(
            f"{name} of {value} has more than {STORED_DECIMAL_PLACES} decimal places, which is "
            "the precision it is stored at — the value that came back would not be the value "
            "you typed"
        )


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """What one worker trades, and how.

    Frozen: a running worker's configuration must not change under it halfway
    through an evaluation. A saved edit produces a *new* object, which the
    worker picks up on its next start — see `RunningWorkerConfig` for how the
    dashboard tells a saved config from the one actually in force.

    Every default here is the same default the environment variable it replaced
    had, and the two that are empty are empty on purpose:

    - **no symbols** — a worker that invented a universe would spend the one
      market-data connection on a watchlist nobody chose;
    - **no strategy** — a worker that started trading because it was deployed,
      rather than because somebody chose to, is the accident this prevents.
    """

    #: The market-data watchlist. Uppercase tickers, deduplicated, order kept.
    symbols: tuple[str, ...] = ()
    #: How long the feed may go quiet *during a session* before the watchdog
    #: halts trading. Should be looser than `RiskLimits.max_quote_age_seconds`,
    #: which is how stale a quote may be when an order is priced against it: a
    #: symbol can legitimately go a minute without printing. A convention rather
    #: than an invariant, and unenforced — see that field for why.
    max_silence_seconds: int = 60
    #: Registry name of the strategy to trade. Empty means this worker places no
    #: orders — it still ingests and still runs its schedule.
    strategy: str = ""
    #: Parameters for that strategy. Empty means the strategy's own defaults.
    #: A `dict` on a frozen object: treat it as read-only, and build a new
    #: config rather than mutating it.
    strategy_params: dict[str, Any] = field(default_factory=dict)
    sizing_method: SizingMethod = "risk_pct"
    sizing_value: Decimal = Decimal("0.01")
    stop_type: StopTypeName = "atr"
    stop_multiplier: Decimal = Decimal("2")
    stop_period: int = 14
    #: The **third** live lock. `ATP_RUN_MODE=live` and
    #: `ATP_ALLOW_LIVE_TRADING=true` between them say this process may trade real
    #: money; this says this unattended loop may place the orders. Paper ignores
    #: it. The API demands the operator's password to turn it on, which is what
    #: keeps it a decision rather than a click (ADR 0009's argument, applied to
    #: the one field here that can lose real money).
    allow_live_orders: bool = False
    #: The account-wide ceilings every order is measured against — this
    #: worker's, and equally an order an operator types into the dashboard
    #: while no worker is running. Nested rather than flattened into this class
    #: so that the one thing a reader must not get wrong stays obvious: these
    #: eight are *limits*, refused at the boundary, while the fields above are
    #: *intent*. `atp_core.risk.limits` is where they are defined and validated.
    risk: RiskLimits = DEFAULT_RISK_LIMITS

    def __post_init__(self) -> None:
        """Refuse anything a worker could not run, at construction.

        Raises `ConfigError` with a sentence naming the field and what is wrong
        with it, because both callers put that sentence in front of a person:
        the API as a 400 body, the worker as the reason it would not start.
        """
        self._check_symbols()
        self._check_strategy()
        self._check_risk()
        self._check_sizing()
        self._check_stop()
        if self.max_silence_seconds < 1:
            raise _fail(
                f"max silence must be at least 1 second, got {self.max_silence_seconds} — "
                "zero would halt trading on the first quiet moment"
            )

    def _check_risk(self) -> None:
        """That the nested ceilings are ceilings at all.

        `RiskLimits.__post_init__` has already refused anything out of range by
        the time one exists, so this is only the type check a `dict` decoded
        from a row or a JSON body would otherwise slip past — arriving as a
        mapping whose `max_position_pct` reads as an attribute error nine
        layers down, inside a rule, on the first order of the day.
        """
        if not isinstance(self.risk, RiskLimits):
            raise _fail(
                "risk limits must be a RiskLimits, got "
                f"{type(self.risk).__name__} — build one so its own bounds are checked"
            )

    def _check_symbols(self) -> None:
        if len(self.symbols) > MAX_SYMBOLS:
            raise _fail(
                f"{len(self.symbols)} symbols is more than the {MAX_SYMBOLS} this accepts; "
                "a watchlist that long is usually a paste rather than a choice"
            )
        seen: set[str] = set()
        for symbol in self.symbols:
            if not SYMBOL.match(symbol):
                raise _fail(
                    f"{symbol!r} is not a ticker this platform can store: uppercase letters, "
                    "digits, dot and dash, at most 20 characters"
                )
            if symbol in seen:
                raise _fail(f"{symbol} appears twice in the watchlist")
            seen.add(symbol)

    def _check_strategy(self) -> None:
        if len(self.strategy) > MAX_STRATEGY_NAME:
            raise _fail(
                f"strategy name is {len(self.strategy)} characters; the strategies table "
                f"stores {MAX_STRATEGY_NAME}"
            )
        if not isinstance(self.strategy_params, dict):
            raise _fail(
                "strategy parameters must be a JSON object, got "
                f"{type(self.strategy_params).__name__}"
            )
        try:
            json.dumps(self.strategy_params)
        except (TypeError, ValueError) as exc:
            raise _fail(f"strategy parameters are not storable as JSON: {exc}") from exc

    def _check_sizing(self) -> None:
        if self.sizing_method not in _SIZING_VALUES:
            raise _fail(
                f"unknown sizing method {self.sizing_method!r}; one of {sorted(_SIZING_VALUES)}"
            )
        if self.sizing_value <= 0:
            raise _fail(f"sizing value must be positive, got {self.sizing_value}")
        _check_storable("sizing value", self.sizing_value)
        if self.sizing_method in FRACTIONAL_SIZING and self.sizing_value > MAX_FRACTIONAL_SIZING:
            # The same backstop `PositionSizeSpec` applies, refused here so it is
            # refused at the moment of typing rather than at the next boot.
            raise _fail(
                f"{self.sizing_method} of {self.sizing_value} is {self.sizing_value:%} of equity "
                f"per trade; this refuses anything above {MAX_FRACTIONAL_SIZING:%}, because "
                "docs/RISK.md's range is 0.5 to 2% and a decimal point in the wrong place is the "
                "mistake worth catching here"
            )

    def _check_stop(self) -> None:
        if self.stop_type not in _STOP_VALUES:
            raise _fail(f"unknown stop type {self.stop_type!r}; one of {sorted(_STOP_VALUES)}")
        if self.stop_multiplier <= 0:
            raise _fail(
                f"stop multiplier must be positive, got {self.stop_multiplier} — a stop at or "
                "through the entry price would be hit immediately"
            )
        # `fixed_pct` and `trailing_pct` read this field as a fraction, so 2
        # would be a stop 200% below entry — a level price cannot reach, which
        # is a stop that does not exist. `fixed_amount` is a price distance and
        # `time` ignores the field entirely, so neither is bounded here.
        if self.stop_type in FRACTIONAL_STOPS and self.stop_multiplier >= 1:
            raise _fail(
                f"{self.stop_type} reads the multiplier as a fraction, so "
                f"{self.stop_multiplier} means {self.stop_multiplier:%} below entry — "
                "a level price cannot reach. Use 0.02 for 2%."
            )
        _check_storable("stop multiplier", self.stop_multiplier)
        if self.stop_period < 1:
            raise _fail(f"stop period must be at least 1 bar, got {self.stop_period}")

    @property
    def trades(self) -> bool:
        """Whether this configuration asks for orders at all.

        Only the first of the worker's locks — the others need a run mode and
        credentials, which are `Settings`' business. `trading.decide` is where
        all of them are read together.
        """
        return bool(self.strategy)

    def with_symbols(self, symbols: Iterable[str]) -> WorkerConfig:
        """A copy watching `symbols`, normalised the way a stored row holds them."""
        return replace(self, symbols=normalise_symbols(symbols))


#: What a worker runs on when nothing has been saved: no watchlist, no strategy,
#: and docs/RISK.md's defaults for everything that has one — the ceilings
#: included, via `DEFAULT_RISK_LIMITS`. Identical to the values `.env.example`
#: shipped, so an existing deployment that upgrades and saves nothing behaves
#: exactly as it did.
DEFAULT_WORKER_CONFIG = WorkerConfig()


@dataclass(frozen=True, slots=True)
class StoredWorkerConfig:
    """A saved configuration, with the provenance the row carries.

    `revision` is the whole reason this wrapper exists. A worker reads the
    config at start and cannot see later edits, so the dashboard has to be able
    to say "what you saved is not what is running" — and it can only say that
    by comparing the number the worker published against the number in the
    table. A timestamp would not do: two saves in the same second are
    indistinguishable, and a clock that goes backwards makes the comparison lie.
    """

    config: WorkerConfig
    #: Monotonic, starting at 1 for the first save. Zero is reserved for
    #: `RunningWorkerConfig` to mean "this worker booted before anything was
    #: ever saved, and is running the defaults".
    revision: int
    updated_at: datetime
    #: The signed-in operator who saved it. Never a process — every write to
    #: this row comes from `PUT /worker/config`, which has a session.
    updated_by: str


def normalise_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    """Uppercase, strip, drop blanks, and keep the first of any duplicate.

    Order is preserved because it is the order the operator typed, and a
    watchlist that reshuffles itself on save reads as the platform having
    changed something it did not.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in symbols:
        symbol = raw.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return tuple(out)


def parse_symbol_list(raw: str) -> tuple[str, ...]:
    """A comma-separated watchlist, as a text box hands it over."""
    return normalise_symbols(raw.split(","))


def parse_strategy_params(raw: str) -> dict[str, Any]:
    """Parse a JSON object typed into a text area, refusing anything else.

    Empty is `{}` — the strategy's own defaults. A typo is refused rather than
    falling back to them, because running on defaults an operator does not think
    are in force is the quietest way to trade the wrong thing.
    """
    text = raw.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _fail(f"strategy parameters are not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise _fail(f"strategy parameters must be a JSON object, got {type(parsed).__name__}")
    return parsed


def parse_decimal(raw: str | Decimal | int | float, *, field_name: str) -> Decimal:
    """A `Decimal` from whatever the wire carried, refusing junk by name.

    Via `str` for a float, on the off chance one ever reaches here: money and
    quantities are `Decimal` end to end (rule §1.1) and `Decimal(0.1)` is not
    `Decimal("0.1")`.
    """
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise _fail(f"{field_name} is not a number: {raw!r}") from exc


def strategy_options(registered: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The strategy dropdown, from the registry.

    Read off the classes rather than off instances, for the reason
    `registry.default_params` gives: a `Strategy` validates its params at
    construction, so building one to ask what it is called would fail for
    exactly the classes whose required params make the question interesting.

    The empty option is not here. "No strategy" is not a strategy, and the
    screen renders it as its own choice with its own sentence — the one that
    says this worker will ingest and schedule but place no orders.
    """
    from atp_core.strategy.registry import default_params

    options: list[dict[str, Any]] = []
    for name, cls in sorted(registered.items()):
        doc = (cls.__doc__ or "").strip().split("\n\n")[0]
        options.append(
            {
                "value": name,
                "label": name,
                "help": " ".join(doc.split()),
                "params_schema": getattr(cls, "params_schema", {}),
                "default_params": default_params(cls),
            }
        )
    return options
