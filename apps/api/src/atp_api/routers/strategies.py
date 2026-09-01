"""Strategy CRUD and lifecycle — requirement #1.

Promotion is a one-way ratchet: draft → backtest → paper → live. A strategy
cannot skip a stage. The API enforces it because the discipline is worth more
than the convenience, and because "just this once" is how untested code reaches
a live account.

**Creating is built; the rest of the ratchet is not**, and the line is drawn
where the ratchet's own logic draws it. A create puts a strategy on the *first*
rung, which grants it nothing: `draft` is where a booting worker and
`scripts/seed.py` already leave one, and `NewStrategy` has no `state` field
through which a caller could ask for anything else. Every later rung is a
promotion, and promotion to `live` additionally requires a completed backtest on
record, a minimum paper-trading period, `ATP_ALLOW_LIVE_TRADING=true` and an
audit entry naming a human (docs/SAFETY.md). A promote endpoint that skipped a
check it could not perform would be the ratchet with its pawl removed, which is
worse than no endpoint at all — so it stays a stub while editing and pausing do.

**What creating unblocks, that nothing else could.** A declarative rule set was
unrunnable in one specific way: `POST /api/v1/backtests` has resolved a stored
rule set into a run since #96, and nothing could put a rule set at the start of
that path. `StrategyRecord` — all `ensure` accepts, because it is what a booting
worker knows — has no `ruleset` field, and the adapter wrote that column as a
hard-coded `None`. So the run side landed before the authoring side, and the
platform's whole declarative half was reachable only by writing SQL by hand.
`NewStrategy` is the write type that closes it (`strategy/ports.py`).

Creating a **coded** strategy's row is the smaller half and is now the smaller
half in a second sense: that row is the foreign key `backtest_runs.strategy_id`
needs, and its absence used to be the 409 `POST /backtests` answered on a clean
database — the one that made queueing a backtest require configuring a *trading*
worker first. That endpoint writes the row itself now, so creating a coded
strategy here is for putting a *description*, a universe and non-default params
on the record before anything runs, rather than for unblocking a backtest.

**What the reads answer, that nothing else does.** Two questions, and the second
is the one worth building for:

- Which strategies are stored? That is the `strategies` table — written by
  `StrategyRepository.ensure` at every session open, by the create below, by
  `scripts/seed.py`, and by `POST /backtests` for a registered class it is
  queueing the first run of. A worker's boot is only the first of the four, so
  a row is no longer evidence that anything has *run* the strategy; the absence
  of one still means nothing has.
- Which strategy classes exist in the code but have *never* run? That is the
  registry minus the table, and it is invisible everywhere else. No strategy
  is configured by default, so "I wrote a strategy and nothing is happening" is the
  expected first experience of this platform, and until now no screen could tell
  a reader whether the thing they configured was ever picked up.
"""

from __future__ import annotations

#: Imported at runtime, not behind `if TYPE_CHECKING`: FastAPI resolves a
#: handler's annotations when it wires the graph, and a name that exists only for
#: the type checker raises `NameError` on the first request.
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel, Field, ValidationError

from atp_api.deps import CurrentUser, get_audit_sink, get_clock, get_strategy_repository
from atp_core.audit.ports import Action, AuditEntry, AuditSink
from atp_core.clock import Clock
from atp_core.domain import StrategyState, Timeframe
from atp_core.errors import ATPError, StrategyExistsError
from atp_core.logging import get_logger
from atp_core.strategy import RuleSet, compile_ruleset, registry
from atp_core.strategy import examples as _examples  # noqa: F401 — populates the registry
from atp_core.strategy.ports import NewStrategy, StoredStrategy, StrategyRepository

log = get_logger(__name__)

#: `@register` runs at import time, so a process that has not imported the
#: strategy modules has an empty registry — and would report, with total
#: confidence, that this platform has no strategies. The worker and the backtest
#: script already import `examples` for the same reason; the API had never
#: needed to, because nothing here read the registry until now. Without this
#: line the "never run" list below is every class, and the "available" list is
#: none of them.
router = APIRouter(prefix="/strategies", tags=["strategies"])


#: The longest name this table can store, and it is the primary key's width
#: rather than a policy: `strategies.id` is `String(36)`, a strategy's id is its
#: name, and `signals.strategy_id` and `orders.strategy_id` are foreign keys onto
#: it. A longer name is refused here with that sentence, because the alternative
#: is a `value too long for type character varying(36)` from a driver, on a
#: request that looked entirely reasonable.
MAX_NAME_LENGTH = 36

#: What a name may contain. A strategy id travels in a URL path
#: (`/strategies/{strategy_id}`), in a query string (`?strategy_id=`) and into
#: every log line and audit row that mentions it, so it is held to a token that
#: survives all three. The registered classes — `sma_crossover`, `buy_and_hold`,
#: `rsi_mean_reversion` — are already well inside it.
NAME_CHARACTERS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

#: A ticker, as every other column in the platform spells one: uppercase, and
#: inside the `String(20)` the bar and order tables give it. `Instrument` refuses
#: a lowercase symbol for the same reason, one layer down.
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,19}$")


class StrategyCreate(BaseModel):
    """A strategy as an author sends it.

    Three fields a reader might expect are deliberately absent, and each is
    absent because the platform — not the caller — is the authority on it:

    - **`id`** is the name. Not a generated uuid: `Signal.strategy_id` carries
      `Strategy.name` everywhere, a compiled rule set included, so a row keyed on
      anything else would leave every signal pointing at nothing.
    - **`class_name`** is read off the registry for a coded strategy and is null
      for a rule set. Accepting one would let a row claim a class it is not,
      which the Strategies tab would then render as fact.
    - **`state`** is `draft`, always. Promotion is a separate act with
      preconditions this endpoint cannot check (docs/SAFETY.md), so there is no
      field here to ask for a higher rung with.

    `kind` is a `Literal` rather than a `str`, so an unknown one is a 422 naming
    both. As a bare string it would reach a handler that has exactly two
    branches and would have to invent a third refusal — the same reasoning that
    types the `state` filter below as its enum.
    """

    name: str
    description: str = ""
    kind: Literal["coded", "ruleset"]
    #: For a coded strategy, merged over the class's declared defaults and
    #: validated by its own constructor. Refused for a rule set, whose behaviour
    #: is its spec.
    params: dict[str, Any] = Field(default_factory=dict)
    #: The declarative spec. Required for `kind="ruleset"`, refused for a coded
    #: strategy, and validated against `RuleSet` before it is stored.
    ruleset: dict[str, Any] | None = None
    #: What it is configured to trade. Optional, and for a rule set it is read
    #: from the spec — see `_authored_ruleset` for why a second copy that could
    #: disagree with the spec is refused rather than reconciled.
    universe: list[str] = Field(default_factory=list)
    #: Omitted means `1d` for a coded strategy and the spec's own timeframe for a
    #: rule set. Null rather than a `"1d"` default so those two cases are
    #: distinguishable: a rule set on `1h` and a caller who said nothing must not
    #: look identical to a caller who asked for `1d`.
    timeframe: str | None = None
    #: Refused when non-empty. The column exists and the Strategies tab renders
    #: it, and **nothing in the platform reads it** — sizing and every pre-trade
    #: limit come from `Settings` (docs/RISK.md). Storing a risk block here would
    #: put a limit on screen that nothing enforces, which is the one kind of
    #: silence this domain cannot afford. Kept as a field so the refusal is said
    #: out loud rather than dropped as an unknown key.
    risk_config: dict[str, Any] = Field(default_factory=dict)


class StoredStrategyView(BaseModel):
    """One `strategies` row, as a reader should see it.

    Two fields carry a health warning, and the response says so rather than
    leaving each client to discover it:

    `state` is **not** "is it running now". `ensure` writes `draft` when it
    creates a row and never touches it again, so a strategy a worker has been
    running for a month still reads `draft` — that is the ratchet's first rung
    and nothing has promoted it off. It is the configured lifecycle state, and
    today nothing but a first boot ever sets it.

    Typed `str` and not `StrategyState`, deliberately, and the asymmetry with
    the filter below is the point. This is a **response**: a database that has
    not run the `e2b6d1a70f93` migration still holds `active`, and a row written
    by a newer version may hold a rung this one has never heard of. A response
    model that refused either would turn a readable screen into a 500 over a
    word. Input is the opposite case and is validated.

    `last_started_at` is the `updated_at` column, renamed to what it actually
    means. The same asymmetry: an existing row has only its timestamp bumped, at
    every worker boot. Serving it as `updated_at` would invite every reader to
    conclude somebody edited the strategy this morning.

    **It reads `created_at` for a row no worker has ever started**, because that
    is what the column holds — a create writes both at the same instant, and so
    does a worker's first boot, and one column cannot tell them apart. That was
    already true of every row `scripts/seed.py` writes, and authoring makes it
    ordinary rather than a development-only case. Telling the two apart needs a
    `last_started_at` column of its own, which `ensure` would bump and a create
    would leave null; it is not in this change, and docs/ROADMAP.md carries it
    so the gap is recorded rather than discovered by a reader believing a
    timestamp.
    """

    id: str
    name: str
    description: str
    kind: str
    class_name: str | None
    params: dict[str, Any] = Field(default_factory=dict)
    #: The declarative spec, for a `ruleset` strategy. Null for a coded one,
    #: where the logic is Python and this row cannot describe it.
    ruleset: dict[str, Any] | None
    state: str
    universe: list[str] = Field(default_factory=list)
    timeframe: str
    risk_config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    #: `strategies.updated_at`, named for what it records: a worker booted this.
    last_started_at: datetime


class AvailableStrategyView(BaseModel):
    """A strategy class the code knows about.

    From the registry rather than the database, so this is what *could* run
    rather than what has. `has_run` is the join between the two and is the
    reason both are in one response: separately they are two lists a reader has
    to diff by eye, and the answer they want — "is the thing I wrote being
    picked up?" — is exactly that diff.
    """

    name: str
    class_name: str
    description: str
    #: JSON Schema for `params`. Served because it is the class's own statement
    #: of what it can be configured with; nothing renders a form from it yet.
    params_schema: dict[str, Any] = Field(default_factory=dict)
    #: True when a `strategies` row exists for this name — written by a worker
    #: at a session open, by `POST /strategies`, by `scripts/seed.py`, or by the
    #: first backtest queued for the class. False is the sharper half and the
    #: reason this field exists: the class is in the code and nothing has ever
    #: stored it, which is the ordinary state of a fresh install.
    #:
    #: Named for the question it used to answer exactly — "has a worker run
    #: this?" — which it stopped answering when a queued backtest began writing
    #: the row a run's foreign key needs. Kept rather than renamed, because the
    #: name is in the generated client types and the field's *useful* direction
    #: is unchanged; the dashboard labels a true as "stored" rather than as a
    #: worker having run anything.
    has_run: bool


class StrategiesResponse(BaseModel):
    strategies: list[StoredStrategyView]
    available: list[AvailableStrategyView]
    #: Registered classes with no row at all. Derived here rather than left to
    #: the client, because it is the answer to the question the screen exists
    #: for and every client would compute it identically.
    never_run: list[str] = Field(default_factory=list)


def _to_view(stored: StoredStrategy) -> StoredStrategyView:
    return StoredStrategyView(
        id=stored.id,
        name=stored.name,
        description=stored.description,
        kind=stored.kind,
        class_name=stored.class_name,
        params=dict(stored.params),
        ruleset=dict(stored.ruleset) if stored.ruleset else None,
        state=stored.state,
        universe=list(stored.universe),
        timeframe=stored.timeframe,
        risk_config=dict(stored.risk_config),
        created_at=stored.created_at,
        last_started_at=stored.updated_at,
    )


def _available(known: set[str]) -> list[AvailableStrategyView]:
    """The registry, with each class marked run or not.

    Sorted by name, because the registry is a dict and a screen whose rows moved
    between reads would be unusable. Reading class attributes rather than
    instantiating: a `Strategy` validates its params at construction, so building
    one here to ask its name would fail for every class with required params —
    and asking what a class *is* should not depend on having a valid config for
    it.
    """
    return [
        AvailableStrategyView(
            name=name,
            class_name=cls.__name__,
            description=cls.description,
            params_schema=dict(cls.params_schema),
            has_run=name in known,
        )
        for name, cls in sorted(registry.all_strategies().items())
    ]


@router.get("", response_model=StrategiesResponse)
async def list_strategies(
    strategy_repo: Annotated[StrategyRepository, Depends(get_strategy_repository)],
    state: StrategyState | None = None,
) -> StrategiesResponse:
    """Stored strategies, and the registered classes beside them.

    One response rather than two endpoints, because the useful fact is the
    *difference*: a class in the registry with no row has never been loaded by a
    worker, and with no strategy configured by default that is the ordinary
    state of a fresh install. Making a client fetch both and diff them would
    leave every client computing the same thing, and a screen that showed only
    the table would answer "nothing is running" with an empty list that looks
    identical to "nothing is written".

    `state` filters the stored half only. The registry has no state — a class is
    not draft or live, it is just compiled — so a filtered request still
    reports every available class, and `never_run` is still computed against the
    unfiltered table rather than against the filtered view. Otherwise filtering
    to `paused` would report every running strategy as never run.

    It is the `StrategyState` enum rather than a bare string, so an unrecognised
    value is a 422 naming the six rungs. As a string a typo returned `200` with
    an empty list, which reads as "no strategy is in that state" — indistinguishable
    from the truth, and the wrong half of the two to believe. This is the same
    class of defect the whole change fixes, one layer up: a state vocabulary
    nothing was checking.

    Unscoped by run mode: `strategies` has no such column, because a strategy is
    the same strategy whichever mode runs it. Its *orders* are separable, and
    that is where the distinction belongs.
    """
    all_stored = await strategy_repo.list_all()
    known = {stored.id for stored in all_stored} | {stored.name for stored in all_stored}
    # Compared against `.value`: `state` is a `StrategyState` and `stored.state`
    # is the column's raw string, which may legitimately be a rung this version
    # does not know (see `StoredStrategyView`). `StrategyState` is a `StrEnum`,
    # so `==` against a str works — but only for members, and being explicit
    # here is what keeps that from being an accident.
    shown = all_stored if state is None else [s for s in all_stored if s.state == state.value]

    return StrategiesResponse(
        strategies=[_to_view(stored) for stored in shown],
        available=_available(known),
        never_run=[name for name in sorted(registry.all_strategies()) if name not in known],
    )


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=detail)


def _validated_name(name: str) -> str:
    """The name, or a 400 saying which rule it broke and why the rule exists.

    Three separate refusals rather than one pattern, because "name is invalid"
    sends an author to guess which half of it was wrong. The length one in
    particular is a column width they cannot see.
    """
    if not name:
        raise _bad_request("name is empty")
    if len(name) > MAX_NAME_LENGTH:
        raise _bad_request(
            f"name is {len(name)} characters and the limit is {MAX_NAME_LENGTH}: a "
            "strategy's name is its primary key, and every signal and order carries "
            "it as a foreign key onto that column"
        )
    if not NAME_CHARACTERS.match(name):
        raise _bad_request(
            f"{name!r} is not a usable strategy name: letters, digits, underscore, "
            "dot and dash only, starting with a letter or digit. The name is the id "
            "that travels in URLs, query strings and every log line about it"
        )
    return name


def _validated_universe(symbols: list[str], *, where: str) -> tuple[str, ...]:
    """Tickers, deduplicated, in the order they were given.

    Uppercase is **required** rather than applied, and the asymmetry with
    `POST /backtests` — which upper-cases what it is handed — is deliberate.
    That endpoint normalises a symbol on its way into a query it runs itself.
    This one stores a universe that is matched against `Bar.symbol` later, by
    something else; and for a rule set the copy that decides behaviour is inside
    the spec, which this function cannot rewrite. Quietly upper-casing the row
    while the spec kept `spy` would produce a strategy that reads correct on the
    screen and never trades.
    """
    seen: dict[str, None] = {}
    for raw in symbols:
        symbol = raw.strip()
        if not symbol:
            continue
        if not SYMBOL.match(symbol):
            raise _bad_request(
                f"{symbol!r} in {where} is not a ticker: uppercase letters, digits, "
                "dot and dash, up to 20 characters. A symbol that is not spelled the "
                "way the bar tables spell it matches no bar, so the strategy would "
                "simply never trade"
            )
        seen[symbol] = None
    return tuple(seen)


def _validated_timeframe(timeframe: str) -> str:
    try:
        return Timeframe(timeframe).value
    except ValueError:
        supported = ", ".join(t.value for t in Timeframe)
        raise _bad_request(f"timeframe must be one of: {supported}") from None


def _authored_coded(payload: StrategyCreate, name: str) -> NewStrategy:
    """A row for a registered class.

    The class is **constructed** rather than merely looked up, exactly as
    `POST /backtests` constructs one: a `Strategy` validates its params in its
    constructor, so an impossible pair is a 400 on this request instead of a
    strategy that fails the first time a worker loads it.

    `params` are the class's declared defaults with the request merged over
    them, and that is `registry.default_params`' own argument applied here:
    storing `{}` records a strategy configured with nothing, when what would
    actually run is a crossover on 20 and 50. A reader cannot tell those apart,
    and the second is the truth.

    **One row per class**, and it is not a policy: the name must be the
    registered one, so a second configuration of the same class under a
    different name is not something this endpoint declines to allow — it is
    something that could not work. Every signal the class emits carries its
    registered name, so the second row would be pointed at by nothing and the
    first would collect the decisions of both. Parameterised variants of one
    class are what a backtest's `params` are for; a variant that should be its
    own strategy is a rule set, or a new class.
    """
    if payload.ruleset is not None:
        raise _bad_request(
            f"{name!r} is a coded strategy, so its logic is the registered class and "
            "a `ruleset` here would be a spec nothing reads. Send kind='ruleset' to "
            "author rules instead"
        )

    try:
        strategy_cls = registry.get(name)
    except ATPError as exc:
        # Names every registered strategy, which is what makes a typo
        # self-correcting. The registry is populated by the `examples` import at
        # the top of this module — without it this refuses everything, with
        # total confidence.
        raise _bad_request(str(exc)) from None

    params = {**registry.default_params(strategy_cls), **payload.params}
    try:
        strategy_cls(dict(params))
    except ATPError as exc:
        raise _bad_request(f"strategy rejected its params: {exc}") from None
    except (TypeError, ValueError) as exc:
        raise _bad_request(f"strategy rejected its params: {exc}") from None

    return NewStrategy(
        id=name,
        name=name,
        kind="coded",
        # The author's own words or nothing. The class's `description` is
        # already served in `available[]`, and copying it into the row would
        # leave a second, staler copy of a sentence the code owns.
        description=payload.description.strip(),
        class_name=strategy_cls.__name__,
        params=params,
        ruleset=None,
        universe=_validated_universe(payload.universe, where="universe"),
        timeframe=_validated_timeframe(payload.timeframe or "1d"),
    )


def _authored_ruleset(payload: StrategyCreate, name: str) -> NewStrategy:
    """A row for a declarative spec, refused unless it could actually run.

    Every check here answers now what the alternative answers later from another
    process — a queued backtest that fails, or a worker that raises at its first
    session open. Two are worth arguing for:

    **The spec's name must be the row's name.** `compile_ruleset` copies
    `spec.name` onto the class it builds, and every `Signal` that class emits
    stamps `strategy_id` with it. A row stored under a different name would take
    the foreign key `signals.strategy_id` needs and put it somewhere no signal
    points, so the strategy would run and every decision it made would fail to
    record.

    **A rule set may not take a registered class's name.** `registry.register`
    already refuses a duplicate, in its own words to keep backtest results
    unambiguous; this is that rule across the two namespaces rather than a new
    one. A configured strategy would load the class while `POST /backtests` for
    the same `x` would run the rules, and both would file their signals under
    one `strategy_id` — two strategies whose attribution had silently merged.
    """
    if not payload.ruleset:
        raise _bad_request(
            "kind='ruleset' needs a `ruleset`: a declarative strategy with no rules "
            "is a row nothing can run, and no class can stand in for it"
        )
    if payload.params:
        # Not ignored: a caller who sent params believes they do something.
        raise _bad_request(
            f"a rule set takes no params, got {sorted(payload.params)}. Its behaviour "
            "is the spec — put the numbers in the rules"
        )
    if name in registry.all_strategies():
        raise _bad_request(
            f"{name!r} is already the name of a registered strategy class, so a rule "
            "set cannot take it. Both would emit signals under this one strategy_id "
            "and their attribution would merge"
        )

    try:
        spec = RuleSet.model_validate(payload.ruleset)
    except ValidationError as exc:
        raise _bad_request(f"the rule set is malformed: {exc}") from None

    if spec.name != name:
        raise _bad_request(
            f"the rule set names itself {spec.name!r} and this strategy is {name!r}. "
            "A compiled rule set stamps every signal with the spec's name, so the two "
            "must agree or nothing it decides can be recorded against this row"
        )

    try:
        compile_ruleset(spec)
    except ATPError as exc:
        # Reached by a spec that validates and still cannot run — `RuleSet`
        # checks shape, and this checks that something can execute.
        raise _bad_request(f"the rule set does not compile: {exc}") from None

    universe = _validated_universe(list(spec.universe), where="the rule set's universe")
    requested = _validated_universe(payload.universe, where="universe")
    if requested and set(requested) != set(universe):
        raise _bad_request(
            f"the rule set trades {sorted(universe)} and this asks to store "
            f"{sorted(requested)}. A compiled rule set takes no trade in a symbol "
            "outside its own universe, so the row would advertise symbols the "
            "strategy ignores — omit `universe` and the spec's is used"
        )

    timeframe = spec.timeframe.value
    if payload.timeframe is not None and _validated_timeframe(payload.timeframe) != timeframe:
        raise _bad_request(
            f"the rule set is on {timeframe} and this asks to store "
            f"{payload.timeframe!r}. The spec decides which bars it sees"
        )

    return NewStrategy(
        id=name,
        name=name,
        kind="ruleset",
        # The spec's own description when the author gave none. Unlike the coded
        # case there is no other copy to drift from: the spec is stored in this
        # same row, so the fallback reads from the thing it is describing.
        description=payload.description.strip() or spec.description,
        class_name=None,
        params={},
        ruleset=dict(payload.ruleset),
        universe=universe,
        timeframe=timeframe,
    )


def _authored(payload: StrategyCreate) -> NewStrategy:
    """The request as a row, or a 400 explaining what is wrong with it."""
    name = _validated_name(payload.name.strip())
    if payload.risk_config:
        raise _bad_request(
            f"risk_config is not stored, got {sorted(payload.risk_config)}. Nothing in "
            "the platform reads that column — sizing and every pre-trade limit come "
            "from Settings (docs/RISK.md) — so accepting it would put a limit on the "
            "Strategies tab that no order is ever checked against"
        )
    if payload.kind == "ruleset":
        return _authored_ruleset(payload, name)
    return _authored_coded(payload, name)


@router.post("", response_model=StoredStrategyView, status_code=201)
async def create_strategy(
    payload: StrategyCreate,
    strategy_repo: Annotated[StrategyRepository, Depends(get_strategy_repository)],
    audit: Annotated[AuditSink, Depends(get_audit_sink)],
    clock: Annotated[Clock, Depends(get_clock)],
    actor: CurrentUser,
) -> StoredStrategyView:
    """Store a strategy, at `draft`.

    Validate before storing. A `ruleset` is parsed through `RuleSet` *and
    compiled* here, so a malformed rule fails at creation with a clear message
    rather than at 09:31 next Tuesday inside the worker — and a coded strategy's
    params are put through the class's own constructor for the same reason.

    **The response is `StoredStrategyView`, the same shape `GET /strategies`
    serves**, rather than an echo of the request. Two reasons, and the second is
    the one that made it worth changing: what the caller asked for and what the
    table now holds are not the same object — `state`, the timestamps, a coded
    strategy's `class_name` and its defaulted `params` are all decided here — and
    a create that echoed the request would report none of them. The shape it
    replaced would also have served the raw `updated_at`, under exactly the name
    the read view is careful not to use.

    No `Location` header, deliberately: `GET /strategies/{id}` is still a stub,
    and pointing a client at a `NotImplementedError` is worse than pointing it
    nowhere. The whole row is in this body, which is what a client needs anyway.

    409 on a name that is taken, and the name may well have been taken by
    something nobody authored: a worker writes a row for whatever
    the worker configuration names at its first session open, and `scripts/seed.py`
    writes one per registered class. The refusal says so, because "already
    exists" about a strategy you have never created is otherwise a puzzle.
    """
    new = _authored(payload)

    try:
        stored = await strategy_repo.create(new)
    except StrategyExistsError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"a strategy named {new.name!r} is already stored. A worker writes a "
                "row the first time it loads a strategy, `scripts/seed.py` writes one "
                "per registered class, and queueing a backtest writes one for the "
                "class it runs — so the name may be taken by something you did not "
                "create — see the Strategies tab"
            ),
        ) from exc

    # After the row exists, never before — the same ordering `HALT_ENGAGED` uses
    # and for the same reason: an entry claiming an action that did not take is
    # read as fact by whoever reviews it afterwards. `record` never raises
    # (ADR 0010), so a database that cannot take the entry does not undo the
    # strategy that was created.
    await audit.record(
        AuditEntry(
            at=clock.now(),
            actor=actor,
            action=Action.STRATEGY_CREATED,
            target=stored.id,
            detail={"kind": stored.kind, "universe": list(stored.universe)},
        )
    )
    log.info("strategy.created", strategy=stored.id, kind=stored.kind, actor=actor)
    return _to_view(stored)


@router.get("/available")
async def list_available_strategy_classes() -> list[dict[str, Any]]:
    """Registered strategy classes with their params JSON Schema.

    Still a stub, and now for a reason rather than by omission: the list above
    already carries this, marked with whether each class has ever run. A second
    endpoint serving the same registry would be a second thing to keep in step
    with it, and nothing calls this one. It was described as the frontend's
    configuration-form source, waiting on `POST /strategies`; that endpoint is
    now built and there is still no form — and when one is written it will read
    `available[]`, which already carries every class with its `params_schema`.
    """
    raise NotImplementedError


@router.get("/{strategy_id}", response_model=StoredStrategyView)
async def get_strategy(strategy_id: str) -> StoredStrategyView:
    """One strategy.

    Still a stub: nothing consumes it. The screen reads every strategy in one
    request — this table has one row per strategy that has ever booted — so a
    per-id endpoint would be built, tested and documented with no caller.
    """
    raise NotImplementedError


@router.patch("/{strategy_id}", response_model=StoredStrategyView)
async def update_strategy(strategy_id: str, payload: dict[str, Any]) -> StoredStrategyView:
    """Editing a strategy that is live requires pausing it first — swapping
    parameters underneath open positions leaves them orphaned from the logic
    that opened them."""
    raise NotImplementedError


@router.post("/{strategy_id}/promote")
async def promote_strategy(
    strategy_id: str, target_state: str, confirmed_by: str
) -> StoredStrategyView:
    """Advance a stage.

    Promotion to `live` additionally requires: a completed backtest on record,
    a minimum paper-trading period, `ATP_ALLOW_LIVE_TRADING=true`, and an audit
    entry naming a human. See docs/SAFETY.md.

    Still a stub, and the reason has narrowed again. "A completed backtest on
    record" became checkable when `backtest_runs` got a reader (ADR 0016). The
    audit trail is no longer the blocker either: `create_strategy` above writes
    a lifecycle verb naming the session's user, so the mechanism this needed —
    a verb, an actor from the cookie rather than from the body, a sink that
    never fails the action — is wired and demonstrated.

    What is left is this endpoint's own work, and it is not small: a verb per
    transition, the minimum paper-trading period measured against something
    (nothing today records when a strategy reached `paper`), and the refusal to
    move more than one rung at a time. An endpoint that promoted a strategy to
    live while skipping a check it could not perform would be this ratchet with
    its pawl removed, which is why it waits rather than shipping half.
    """
    raise NotImplementedError


@router.post("/{strategy_id}/pause")
async def pause_strategy(strategy_id: str, close_positions: bool = False) -> StoredStrategyView:
    """Stop generating signals. Existing positions keep their stops unless
    `close_positions` — pausing must not silently strip protection."""
    raise NotImplementedError
