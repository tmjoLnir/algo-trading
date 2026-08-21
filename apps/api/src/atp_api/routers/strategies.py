"""Strategy CRUD and lifecycle — requirement #1.

Promotion is a one-way ratchet: draft → backtest → paper → live. A strategy
cannot skip a stage. The API enforces it because the discipline is worth more
than the convenience, and because "just this once" is how untested code reaches
a live account.

**The reads are built; the writes are not**, the same split as `orders.py` and
`positions.py` and for a sharper reason here. Creating, editing, promoting and
pausing are the ratchet itself: promotion to `live` additionally requires a
completed backtest on record, a minimum paper-trading period,
`ATP_ALLOW_LIVE_TRADING=true` and an audit entry naming a human
(docs/SAFETY.md). One of those preconditions became checkable with the backtest
queue — `backtest_runs` has a reader now (ADR 0016) — and the other has not: the
audit trail's order-flow and lifecycle verbs are still unwired (ADR 0010), so the
entry naming a human cannot be written. A promote endpoint that skipped the check
it could not perform would be the ratchet with its pawl removed, which is worse
than no endpoint at all.

**What the reads answer, that nothing else does.** Two questions, and the second
is the one worth building for:

- Which strategies has a worker actually run? That is the `strategies` table,
  written by `StrategyRepository.ensure` at every session open.
- Which strategy classes exist in the code but have *never* run? That is the
  registry minus the table, and it is invisible everywhere else. `WORKER_STRATEGY`
  is empty by default, so "I wrote a strategy and nothing is happening" is the
  expected first experience of this platform, and until now no screen could tell
  a reader whether the thing they configured was ever picked up.
"""

from __future__ import annotations

#: Imported at runtime, not behind `if TYPE_CHECKING`: FastAPI resolves a
#: handler's annotations when it wires the graph, and a name that exists only for
#: the type checker raises `NameError` on the first request.
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from atp_api.deps import get_strategy_repository
from atp_core.domain import StrategyState
from atp_core.strategy import examples as _examples  # noqa: F401 — populates the registry
from atp_core.strategy import registry
from atp_core.strategy.ports import StoredStrategy, StrategyRepository

#: `@register` runs at import time, so a process that has not imported the
#: strategy modules has an empty registry — and would report, with total
#: confidence, that this platform has no strategies. The worker and the backtest
#: script already import `examples` for the same reason; the API had never
#: needed to, because nothing here read the registry until now. Without this
#: line the "never run" list below is every class, and the "available" list is
#: none of them.
router = APIRouter(prefix="/strategies", tags=["strategies"])


class StrategyCreate(BaseModel):
    name: str
    description: str = ""
    kind: str  # "coded" (registered class) | "ruleset" (declarative)
    class_name: str | None = None
    params: dict[str, Any] = {}
    ruleset: dict[str, Any] | None = None  # validated against RuleSet
    universe: list[str]
    timeframe: str = "1d"
    risk_config: dict[str, Any] = {}


class StrategyOut(StrategyCreate):
    id: str
    state: str
    created_at: datetime
    updated_at: datetime


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
    #: True when a worker has registered a row for this name. False means the
    #: class exists and nothing has ever loaded it.
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
    worker, and with `WORKER_STRATEGY` empty by default that is the ordinary
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


@router.post("", response_model=StrategyOut, status_code=201)
async def create_strategy(payload: StrategyCreate) -> StrategyOut:
    """Validate before storing.

    A `ruleset` is parsed through `RuleSet` here so a malformed rule fails at
    creation with a clear message, not at 09:31 next Tuesday inside the worker.
    """
    raise NotImplementedError


@router.get("/available")
async def list_available_strategy_classes() -> list[dict[str, Any]]:
    """Registered strategy classes with their params JSON Schema.

    Still a stub, and now for a reason rather than by omission: the list above
    already carries this, marked with whether each class has ever run. A second
    endpoint serving the same registry would be a second thing to keep in step
    with it, and nothing calls this one. It is the frontend's configuration-form
    source, and there is no form — that lands with `POST /strategies`.
    """
    raise NotImplementedError


@router.get("/{strategy_id}", response_model=StrategyOut)
async def get_strategy(strategy_id: str) -> StrategyOut:
    """One strategy.

    Still a stub: nothing consumes it. The screen reads every strategy in one
    request — this table has one row per strategy that has ever booted — so a
    per-id endpoint would be built, tested and documented with no caller.
    """
    raise NotImplementedError


@router.patch("/{strategy_id}", response_model=StrategyOut)
async def update_strategy(strategy_id: str, payload: dict[str, Any]) -> StrategyOut:
    """Editing a strategy that is live requires pausing it first — swapping
    parameters underneath open positions leaves them orphaned from the logic
    that opened them."""
    raise NotImplementedError


@router.post("/{strategy_id}/promote")
async def promote_strategy(strategy_id: str, target_state: str, confirmed_by: str) -> StrategyOut:
    """Advance a stage.

    Promotion to `live` additionally requires: a completed backtest on record,
    a minimum paper-trading period, `ATP_ALLOW_LIVE_TRADING=true`, and an audit
    entry naming a human. See docs/SAFETY.md.

    Still a stub, and now for one missing precondition rather than two.
    "A completed backtest on record" became checkable when `backtest_runs` got a
    reader (ADR 0016). The audit trail's lifecycle verbs are still unwired (ADR
    0010), so the entry naming a human cannot be written — and an endpoint that
    promoted a strategy to live while silently skipping the record of who did it
    would be this ratchet with its pawl removed.
    """
    raise NotImplementedError


@router.post("/{strategy_id}/pause")
async def pause_strategy(strategy_id: str, close_positions: bool = False) -> StrategyOut:
    """Stop generating signals. Existing positions keep their stops unless
    `close_positions` — pausing must not silently strip protection."""
    raise NotImplementedError
