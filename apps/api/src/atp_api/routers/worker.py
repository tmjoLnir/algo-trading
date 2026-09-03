"""The worker's trading configuration and the account-wide risk ceilings —
read them, and change them.

These eighteen values used to be environment variables, which had three
consequences worth naming because this endpoint exists to undo all three:

- **Changing one needed shell access to the host.** Widening a stop or adding a
  symbol meant SSH, an editor, and a restart. The dashboard could show a book it
  had no way to influence.
- **Nothing recorded who changed what.** `.env` carries no author and no
  timestamp, so after a bad week "who moved risk-per-trade to 2%, and when" was
  answerable only by asking people.
- **The API could not see them at all.** Every screen that wanted to explain why
  nothing was trading had to say "check `WORKER_STRATEGY`" and hope.

**Two configurations, and the screen must show both.** A worker reads this row
once, at start, and cannot see a later edit — so `saved` and `running` can
differ for as long as nobody restarts the process. A settings page that showed
only what was saved would report a stop multiplier no running process is using,
which is the same class of lie as a dashboard rendering an empty book because
Redis blinked. `running` comes from what the worker itself published
(`WorkerStatusStore`), `pending_restart` is the comparison, and both are on the
screen.

**`allow_live_orders` is the one field that asks for a password.** It is the
third of the three live-money locks (CLAUDE.md §1.8): `ATP_RUN_MODE=live` and
`ATP_ALLOW_LIVE_TRADING=true` say this process may trade real money, and this
says this unattended loop may place the orders. Moving it out of `.env` moved it
within reach of anything holding a session cookie, so it arrives here with ADR
0009's answer attached — the password travels with the act, and a walked-away
laptop cannot arm real trading with one click. Turning it **off** asks for
nothing, for the same reason `/halt` does not: stopping must never be the
harder direction.

The other two locks stay in `Settings` and stay out of this endpoint. A run mode
editable from a browser would be the whole ratchet behind one form.

**The eight risk ceilings arrive as one nested object and save in the same
act.** They are limits rather than intent — the platform refuses an order that
crosses one, where the fields above describe what it tries to do — so they are
nested rather than flattened into the payload, and the form renders them under
their own heading. What they are *not* is a second save: an operator who lifts a
position limit while widening a stop made one decision, and one revision, one
audit entry and one restart notice is how this endpoint records it.

They do **not** ask for a password, and the asymmetry with `allow_live_orders` is
deliberate. That field authorises an unattended loop to place real orders, which
is a new capability. These bound orders that are already authorised, and the
direction that matters is that *tightening* one must never be harder than
loosening it — the same reason `/halt` asks for nothing. Every change to them is
audited with both numbers, which is what makes a loosening answerable after the
fact.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel, Field

from atp_api.deps import (
    CurrentUser,
    get_audit_sink,
    get_clock,
    get_worker_config_repository,
    get_worker_status_store,
)
from atp_api.stepup import require_step_up
from atp_core.audit.ports import Action, AuditEntry, AuditSink
from atp_core.clock import Clock
from atp_core.config import Settings, get_settings
from atp_core.errors import ConfigError
from atp_core.logging import get_logger
from atp_core.risk.limits import RISK_LIMIT_FIELDS, RiskLimits
from atp_core.strategy import examples as _examples  # noqa: F401 — populates the registry
from atp_core.strategy import registry
from atp_core.worker import (
    SIZING_METHODS,
    STOP_TYPES,
    RunningWorkerConfig,
    StoredWorkerConfig,
    WorkerConfig,
    WorkerConfigRepository,
    WorkerStatusStore,
    strategy_options,
)
from atp_core.worker.config import (
    DEFAULT_WORKER_CONFIG,
    MULTIPLIER_STOPS,
    PERIOD_STOPS,
    normalise_symbols,
)

log = get_logger(__name__)

router = APIRouter(prefix="/worker", tags=["worker"])

#: The revision a worker reports when it booted before anything was ever saved.
#: Distinguishable from revision 1 — a saved config that happens to equal the
#: defaults — which is why "nothing is stored" is not rendered as "revision 1".
UNSAVED = 0


class RiskLimitsPayload(BaseModel):
    """The eight account-wide ceilings, as the wire carries them.

    The five fractions are strings for the reason every decimal on this API is
    (docs/DASHBOARD.md): each is multiplied by equity to produce the number an
    order is measured against, and a `0.1` that had been through a JSON float
    would move that ceiling.

    One model for both directions — read and write — unlike the pair above,
    because unlike the worker configuration there is nothing here the server
    knows and the client does not. Two identical models would be two places to
    forget a field.
    """

    max_position_pct: Decimal
    max_gross_exposure_pct: Decimal
    max_daily_loss_pct: Decimal
    max_orders_per_minute: int
    max_open_positions: int
    max_quote_age_seconds: int
    default_stop_loss_pct: Decimal
    default_take_profit_pct: Decimal


class WorkerConfigView(BaseModel):
    """The ten parameters, as the wire carries them.

    The two decimals are strings for the reason every decimal on this API is
    (docs/DASHBOARD.md): `sizing_value` scales every order and `stop_multiplier`
    decides where the protective stop sits, and neither should pass through a
    JSON float on the way to a screen.
    """

    symbols: list[str]
    max_silence_seconds: int
    #: Empty means this worker places no orders. Not null: the field always has
    #: a value, and "" is that value.
    strategy: str
    strategy_params: dict[str, Any]
    sizing_method: str
    sizing_value: str
    stop_type: str
    stop_multiplier: str
    stop_period: int
    allow_live_orders: bool
    #: The ceilings this configuration carries. Present on the running view too,
    #: which is the whole reason the worker publishes them: a ceiling edited
    #: since a worker booted is not the ceiling refusing that worker's orders.
    risk: RiskLimitsPayload


class SavedConfigView(BaseModel):
    """What is stored, and who stored it."""

    config: WorkerConfigView
    #: `0` when nothing has ever been saved and `config` is the defaults.
    revision: int
    updated_at: datetime | None
    updated_by: str | None


class RunningConfigView(BaseModel):
    """What a worker last reported booting with.

    `started_at` is the freshness signal, exactly as `book_as_of` is on the
    dashboard: this is a fact about a process that may since have died, and a
    screen that presented it as current would explain a stale configuration as a
    live one.
    """

    config: WorkerConfigView
    revision: int
    started_at: datetime
    trading: bool
    reason: str


class OptionView(BaseModel):
    """One entry in a dropdown, with the prose that says when to pick it."""

    value: str
    label: str
    help: str


class StrategyOptionView(OptionView):
    """A registered strategy class, with what it accepts.

    `params_schema` and `default_params` are here so the form can show what a
    strategy takes without a second request, and so the operator writing JSON
    into the parameters box has the field names in front of them rather than in
    the source.
    """

    params_schema: dict[str, Any]
    default_params: dict[str, Any]


class LimitFieldView(BaseModel):
    """One risk entry box, with the sentence that says what the number means.

    Sent rather than hard-coded in the browser for the same reason the stop
    dropdown's prose is: docs/RISK.md's argument for a number belongs on the
    screen where it is typed, and a copy of it in TypeScript is a copy that goes
    stale the first time the argument changes.

    `maximum` is the server's own ceiling, sent so the form can refuse before
    the round trip. It is a convenience and never the authority — `RiskLimits`
    is, and it re-checks everything.
    """

    name: str
    label: str
    unit: str
    help: str
    maximum: Decimal | None


class WorkerOptionsView(BaseModel):
    """Everything the form needs to render its selects."""

    strategies: list[StrategyOptionView]
    sizing_methods: list[OptionView]
    stop_types: list[OptionView]
    #: The risk section's boxes, in the order the risk chain checks them —
    #: which is the order the risk panel already lists them in, so an operator
    #: who moves between the two screens reads the same sequence twice.
    risk_fields: list[LimitFieldView]
    #: Stop families whose number is a multiple rather than a distance, and
    #: those that read the period. The form relabels its inputs from these
    #: rather than hard-coding a list the platform would then own twice.
    multiplier_stops: list[str]
    period_stops: list[str]


class WorkerConfigScreen(BaseModel):
    """Everything the settings screen renders, in one request.

    One aggregate rather than four, for the reason the dashboard's is one: the
    saved row, the running report and the option catalogue assembled from three
    instants could disagree about which strategies exist, and the reader could
    not tell which to trust.
    """

    saved: SavedConfigView
    #: Null when no worker has ever published — a platform that has never run
    #: one, or a Redis that was flushed. Not an empty running config: "nothing
    #: has reported" and "a worker is running nothing" are different sentences.
    running: RunningConfigView | None
    #: True when a worker is running and its revision is not the saved one.
    #: False when no worker has reported — there is nothing to be pending
    #: against, and a screen that said "restart required" with no process to
    #: restart would send a reader looking for the wrong thing.
    pending_restart: bool
    options: WorkerOptionsView
    #: The process-level facts this screen cannot change, shown because they
    #: decide whether the settings below can do anything. A paper worker ignores
    #: `allow_live_orders` entirely; a backtest worker places no orders at all.
    run_mode: str
    allow_live_trading: bool
    #: Whether arming `allow_live_orders` will demand the password. Always true
    #: today; sent rather than assumed so the form asks when the server asks.
    live_orders_require_password: bool


class WorkerConfigUpdate(BaseModel):
    """A configuration as the form sends it.

    Every field is required. A partial update would make the form's "save"
    depend on which inputs the browser considered dirty, and a stop multiplier
    silently retaining an old value because a field was not touched is the kind
    of surprise this row must not have.
    """

    symbols: list[str] = Field(max_length=200)
    max_silence_seconds: int
    strategy: str = Field(max_length=64)
    strategy_params: dict[str, Any]
    sizing_method: str = Field(max_length=32)
    sizing_value: Decimal
    stop_type: str = Field(max_length=32)
    stop_multiplier: Decimal
    stop_period: int
    allow_live_orders: bool
    risk: RiskLimitsPayload
    #: Required only when this request arms `allow_live_orders`. Never logged,
    #: never stored, and in the body rather than a query string because nginx
    #: writes query strings to its access log verbatim.
    password: str = Field(default="", max_length=1024)


def _risk_payload(limits: RiskLimits) -> RiskLimitsPayload:
    return RiskLimitsPayload(
        max_position_pct=limits.max_position_pct,
        max_gross_exposure_pct=limits.max_gross_exposure_pct,
        max_daily_loss_pct=limits.max_daily_loss_pct,
        max_orders_per_minute=limits.max_orders_per_minute,
        max_open_positions=limits.max_open_positions,
        max_quote_age_seconds=limits.max_quote_age_seconds,
        default_stop_loss_pct=limits.default_stop_loss_pct,
        default_take_profit_pct=limits.default_take_profit_pct,
    )


def _config_view(config: WorkerConfig) -> WorkerConfigView:
    return WorkerConfigView(
        symbols=list(config.symbols),
        max_silence_seconds=config.max_silence_seconds,
        strategy=config.strategy,
        strategy_params=dict(config.strategy_params),
        sizing_method=config.sizing_method,
        sizing_value=str(config.sizing_value),
        stop_type=config.stop_type,
        stop_multiplier=str(config.stop_multiplier),
        stop_period=config.stop_period,
        allow_live_orders=config.allow_live_orders,
        risk=_risk_payload(config.risk),
    )


def _saved_view(stored: StoredWorkerConfig | None) -> SavedConfigView:
    """The stored row, or the defaults labelled as never having been saved."""
    if stored is None:
        return SavedConfigView(
            config=_config_view(DEFAULT_WORKER_CONFIG),
            revision=UNSAVED,
            updated_at=None,
            updated_by=None,
        )
    return SavedConfigView(
        config=_config_view(stored.config),
        revision=stored.revision,
        updated_at=stored.updated_at,
        updated_by=stored.updated_by,
    )


def _running_view(running: RunningWorkerConfig | None) -> RunningConfigView | None:
    if running is None:
        return None
    return RunningConfigView(
        config=_config_view(running.config),
        revision=running.revision,
        started_at=running.started_at,
        trading=running.trading,
        reason=running.reason,
    )


def _options() -> WorkerOptionsView:
    return WorkerOptionsView(
        strategies=[StrategyOptionView(**o) for o in strategy_options(registry.all_strategies())],
        sizing_methods=[OptionView(**asdict(o)) for o in SIZING_METHODS],
        stop_types=[OptionView(**asdict(o)) for o in STOP_TYPES],
        risk_fields=[LimitFieldView(**asdict(f)) for f in RISK_LIMIT_FIELDS],
        multiplier_stops=sorted(MULTIPLIER_STOPS),
        period_stops=sorted(PERIOD_STOPS),
    )


def _screen(
    stored: StoredWorkerConfig | None,
    running: RunningWorkerConfig | None,
    settings: Settings,
) -> WorkerConfigScreen:
    saved = _saved_view(stored)
    return WorkerConfigScreen(
        saved=saved,
        running=_running_view(running),
        pending_restart=running is not None and running.revision != saved.revision,
        options=_options(),
        run_mode=settings.run_mode.value,
        allow_live_trading=settings.allow_live_trading,
        live_orders_require_password=True,
    )


def _to_config(payload: WorkerConfigUpdate) -> WorkerConfig:
    """The value object, with every refusal carrying the sentence that explains it.

    `WorkerConfig.__post_init__` owns the rules so that the worker and this
    endpoint cannot disagree — a value the API accepts and the worker refuses
    would save cleanly and then kill the process at its next start.

    The registry check is the one rule that is *not* in the value object, and
    deliberately: a stored row must stay loadable in a process that has not
    imported the strategy modules, so `WorkerConfig` cannot depend on the
    registry being populated. It belongs here, where the alternative is saving a
    typo that a worker discovers by failing to boot.
    """
    try:
        config = WorkerConfig(
            symbols=normalise_symbols(payload.symbols),
            max_silence_seconds=payload.max_silence_seconds,
            strategy=payload.strategy.strip(),
            strategy_params=payload.strategy_params,
            sizing_method=payload.sizing_method,  # type: ignore[arg-type]
            sizing_value=payload.sizing_value,
            stop_type=payload.stop_type,  # type: ignore[arg-type]
            stop_multiplier=payload.stop_multiplier,
            stop_period=payload.stop_period,
            allow_live_orders=payload.allow_live_orders,
            # Constructed here rather than trusted, so `RiskLimits.__post_init__`
            # is what refuses a ceiling — the same rules the worker applies to
            # the row it reads at start, so the two cannot disagree about what
            # is storable.
            risk=RiskLimits(
                max_position_pct=payload.risk.max_position_pct,
                max_gross_exposure_pct=payload.risk.max_gross_exposure_pct,
                max_daily_loss_pct=payload.risk.max_daily_loss_pct,
                max_orders_per_minute=payload.risk.max_orders_per_minute,
                max_open_positions=payload.risk.max_open_positions,
                max_quote_age_seconds=payload.risk.max_quote_age_seconds,
                default_stop_loss_pct=payload.risk.default_stop_loss_pct,
                default_take_profit_pct=payload.risk.default_take_profit_pct,
            ),
        )
    except ConfigError as exc:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if config.strategy and config.strategy not in registry.all_strategies():
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=(
                f"no strategy named {config.strategy!r} is registered; "
                f"one of {sorted(registry.all_strategies())}, or empty to place no orders"
            ),
        )
    return config


#: The ceiling names, taken from the form catalogue rather than restated, so a
#: limit added to `RiskLimits` cannot be saved without appearing in the audit
#: diff — the failure mode being a widened ceiling that no post-mortem can find.
RISK_FIELDS: tuple[str, ...] = tuple(f.name for f in RISK_LIMIT_FIELDS)


def _changes(before: WorkerConfig, after: WorkerConfig) -> dict[str, Any]:
    """Which fields moved, and what they moved between.

    The audit row's whole value. "The worker configuration was saved" answers
    nothing a post-mortem asks; "risk_pct went from 0.01 to 0.02 at 14:32, saved
    by josh" answers most of it. No field here is a secret — the password is not
    part of the configuration and never reaches this function.
    """
    fields = (
        "symbols",
        "max_silence_seconds",
        "strategy",
        "strategy_params",
        "sizing_method",
        "sizing_value",
        "stop_type",
        "stop_multiplier",
        "stop_period",
        "allow_live_orders",
    )
    out: dict[str, Any] = {}
    for name in fields:
        old, new = getattr(before, name), getattr(after, name)
        if old != new:
            out[name] = {"from": _jsonable(old), "to": _jsonable(new)}
    # The ceilings, flattened as `risk.max_position_pct` rather than nested one
    # level deeper. The audit detail is read as a list of what moved, and a
    # nested object would make the one entry that matters most in a post-mortem
    # — "the position limit was lifted, by whom, when" — the only one a reader
    # has to unwrap.
    for field_name in RISK_FIELDS:
        old, new = getattr(before.risk, field_name), getattr(after.risk, field_name)
        if old != new:
            out[f"risk.{field_name}"] = {"from": _jsonable(old), "to": _jsonable(new)}
    return out


def _jsonable(value: Any) -> Any:
    """Audit detail is a JSON column; Decimals and tuples are not."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    return value


@router.get("/config", response_model=WorkerConfigScreen)
async def get_worker_config(
    repo: Annotated[WorkerConfigRepository, Depends(get_worker_config_repository)],
    status_store: Annotated[WorkerStatusStore, Depends(get_worker_status_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WorkerConfigScreen:
    """The saved configuration, the running one, and the options for both."""
    stored = await repo.load()
    running = await status_store.get(settings.run_mode)
    return _screen(stored, running, settings)


@router.put("/config", response_model=WorkerConfigScreen)
async def update_worker_config(
    payload: WorkerConfigUpdate,
    actor: CurrentUser,
    repo: Annotated[WorkerConfigRepository, Depends(get_worker_config_repository)],
    status_store: Annotated[WorkerStatusStore, Depends(get_worker_status_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditSink, Depends(get_audit_sink)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> WorkerConfigScreen:
    """Replace the configuration. Validated, audited, and password-gated on one field.

    The order is deliberate. Validate first, so a bad request never reaches the
    password prompt and an operator is not asked to authenticate a save that was
    going to be refused anyway. Step up second, so the credential is checked
    against the act it authorises. Write third. Audit last, after the row
    exists, so an entry never claims a change that did not take — the same rule
    the halt verbs follow.
    """
    config = _to_config(payload)
    current = await repo.load()
    before = current.config if current is not None else DEFAULT_WORKER_CONFIG

    # Arming, not merely holding. A save that leaves the lock where it already
    # was asks for nothing, and neither does turning it off: stopping is never
    # the harder direction (ADR 0009). Asked regardless of run mode — the value
    # persists, and a paper platform that is later switched to live would
    # otherwise arrive there already armed by a save nobody proved.
    arming = config.allow_live_orders and not before.allow_live_orders
    if arming:
        await require_step_up(
            payload.password, actor, settings, audit, clock, "/api/v1/worker/config"
        )

    changes = _changes(before, config)
    # One reading, used for both. Two calls would stamp the row and the entry
    # that describes it at different instants, and a reader correlating them
    # would have to know that was meaningless.
    at = clock.now()
    stored = await repo.save(config, actor=actor, at=at)

    await audit.record(
        AuditEntry(
            at=at,
            actor=actor,
            action=Action.WORKER_CONFIG_UPDATED,
            target="worker",
            detail={"revision": stored.revision, "changes": changes},
        )
    )
    if arming:
        # The loudest line this endpoint can write. An unattended loop has just
        # been authorised to place real orders, and the log is where that has to
        # be findable without the audit screen.
        log.critical(
            "worker_config.live_orders_armed",
            actor=actor,
            revision=stored.revision,
            msg="live order placement is now permitted for the worker — takes effect on restart",
        )

    running = await status_store.get(settings.run_mode)
    return _screen(stored, running, settings)
