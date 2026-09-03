"""Is this worker ready to spend a week trading paper?

docs/FIRST_PAPER_RUN.md states the preconditions in prose and lists, at the end,
"the things most likely to break first". Every one of those was discoverable in
advance and nothing checked any of them, which matters more here than it would
anywhere else in this codebase: **the input this demonstration needs and cannot
re-run is calendar time.** A backtest that starts wrong is re-run in four
minutes. A paper week that starts with thirty bars behind a strategy needing two
hundred produces silence for five days, and silence is exactly what a correct
run of `sma_crossover` also produces — so the failure is not merely expensive,
it is indistinguishable from success at the moment you most want to tell them
apart.

Every function here is **pure**: it takes facts somebody else gathered and
returns a verdict. That is `trading.decide`'s shape and for its reason — the
answer to "would this configuration trade, and would it get anywhere" belongs
somewhere readable rather than inferred from a wiring block — and it is what
lets the interesting cases be tested without a venue, a database or a week.

`scripts/preflight.py` is the caller that does the I/O.

**A WARN is not a soft FAIL.** FAIL means this configuration cannot produce the
demonstration and running it wastes the week. WARN means it can, and something
about it will make the result harder to read or narrower than you think. The two
are separated because a preflight that fails on everything doubtful trains an
operator to override it, which is how a check stops working (SAFETY.md's
reasoning about the third lock, applied to this).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from atp_core.domain import RunMode
from atp_core.domain.enums import StopType
from atp_core.errors import ATPError
from atp_core.risk.rules import position_size
from atp_core.strategy import registry
from atp_worker.trading import resolve_stop_config

if TYPE_CHECKING:
    from datetime import datetime

    from atp_core.config import Settings
    from atp_core.risk.killswitch import HaltRecord
    from atp_core.risk.limits import RiskLimits
    from atp_core.worker.config import WorkerConfig
    from atp_worker.trading import TradingDecision

#: The clause of docs/SAFETY.md's go-live checklist a failing check maps to,
#: where one exists. Carried on the check rather than printed beside it so a
#: caller rendering these can point a reader at the list that is authoritative.
SAFETY_CHECKLIST = "docs/SAFETY.md, 'Before you go live'"

#: The US pattern-day-trader threshold. Here rather than in `RiskLimits`
#: because it is the venue's rule and not one this platform enforces —
#: reporting it as though it were ours would suggest it could be tuned.
PDT_EQUITY_FLOOR = Decimal("25000")


class Status(StrEnum):
    PASS = "pass"
    #: Runnable, but the result will be narrower or harder to read than expected.
    WARN = "warn"
    #: This configuration cannot produce the demonstration.
    FAIL = "fail"
    #: Not checked — a source was unreachable or deliberately not consulted.
    #: Distinct from PASS, because "we did not look" and "we looked and it was
    #: fine" are the two things an operator must never confuse at 09:29.
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Check:
    """One question, its answer, and what to do about it.

    `fix` is a command or a setting, not advice. A preflight that says
    "insufficient history" and stops has moved the operator's problem from
    "which of eleven things is wrong" to "what do I type", which is not much of
    a saving.
    """

    name: str
    status: Status
    detail: str
    fix: str = ""
    #: Where the requirement comes from, when it comes from somewhere.
    source: str = ""


@dataclass(frozen=True, slots=True)
class Preflight:
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.WARN]

    @property
    def skipped(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.SKIP]

    @property
    def ready(self) -> bool:
        return not self.failures

    def exit_code(self) -> int:
        """0 ready, 1 not.

        Warnings and skips do not fail the command. A skip is the honest state
        of a check whose source was not reachable — `--no-broker`, a stack that
        is not up yet — and failing on it would make the local-only run useless,
        which is the run an operator does first.
        """
        return 0 if self.ready else 1


# ── the configuration itself ────────────────────────────────────────────────


def check_run_mode(settings: Settings) -> Check:
    """Paper, and nothing else.

    Live is a FAIL rather than a warning, and it is the one place this module
    refuses a configuration that would work. docs/FIRST_PAPER_RUN.md opens by
    saying nothing in it should be run against a live account; a preflight that
    shrugged at `ATP_RUN_MODE=live` would be the tool that made the exception
    routine.
    """
    if settings.run_mode is RunMode.PAPER:
        return Check("run mode", Status.PASS, "ATP_RUN_MODE=paper")
    if settings.run_mode is RunMode.LIVE:
        return Check(
            "run mode",
            Status.FAIL,
            "ATP_RUN_MODE=live — this procedure is for paper and only paper",
            fix="ATP_RUN_MODE=paper",
            source="docs/FIRST_PAPER_RUN.md, 'Preconditions'",
        )
    return Check(
        "run mode",
        Status.FAIL,
        f"ATP_RUN_MODE={settings.run_mode.value} has no venue to trade against",
        fix="ATP_RUN_MODE=paper",
    )


def check_credentials(settings: Settings) -> Check:
    """A key is present, and it is pointed at the paper host.

    **Never reports the key, its length, or a prefix of it** (CLAUDE.md §1.6).
    What it reports is the base URL, which is the thing that actually goes
    wrong: paper and live use separate key pairs (SAFETY.md layer 3), so a live
    key against the paper endpoint fails authentication — a useful accident,
    and one that presents at 09:31 as a stream that never connects rather than
    as anything naming credentials.
    """
    if not settings.broker_configured:
        return Check(
            "credentials",
            Status.FAIL,
            "ALPACA_API_KEY is empty — there is no venue to send an order to",
            fix="set ALPACA_API_KEY and ALPACA_API_SECRET in .env (paper keys, not live)",
            source="docs/FIRST_PAPER_RUN.md, 'Preconditions'",
        )
    url = settings.broker_base_url
    if "paper" not in url:
        return Check(
            "credentials",
            Status.FAIL,
            f"a key is set but the broker base URL is {url}, which is not the paper endpoint",
            fix="ATP_RUN_MODE=paper",
        )
    return Check("credentials", Status.PASS, f"key present, pointed at {url}")


def check_locks(decision: TradingDecision) -> Check:
    """`trading.decide`, rendered.

    Not re-derived here. There is one answer to "does this worker place orders"
    and it lives in that function; a second copy in the preflight could pass
    while the worker declined, which is the shape of every bug this module is
    trying to prevent an operator from spending a week on.
    """
    if decision.enabled:
        return Check("trading locks", Status.PASS, decision.reason)
    return Check(
        "trading locks",
        Status.FAIL,
        decision.reason,
        fix="choose a strategy and a watchlist on the Config tab" if not decision.blocked else "",
    )


def check_strategy(config: WorkerConfig) -> Check:
    """The named strategy exists and accepts the parameters it was given.

    Constructed, not merely looked up. A strategy validates its own parameters
    at construction — `sma_crossover` refuses a fast period above its slow one —
    and a typo that survives to the worker starts a strategy on its defaults
    while the operator believes it is running what they set.
    """
    name = config.strategy
    if not name:
        return Check(
            "strategy",
            Status.FAIL,
            "no strategy is configured",
            fix="choose one on the dashboard's Config tab",
        )
    try:
        strategy_cls = registry.get(name)
    except ATPError as exc:
        known = ", ".join(sorted(registry.all_strategies())) or "none registered"
        return Check("strategy", Status.FAIL, str(exc), fix=f"one of: {known}")

    try:
        strategy = strategy_cls(dict(config.strategy_params) or None)
    except (ATPError, TypeError, ValueError) as exc:
        return Check(
            "strategy",
            Status.FAIL,
            f"{name} rejected its parameters: {exc}",
            fix="fix the strategy parameters on the Config tab",
        )
    return Check("strategy", Status.PASS, f"{name} constructs, warmup_bars={strategy.warmup_bars}")


def check_stop_config(config: WorkerConfig) -> Check:
    """The configured stop is one `StopManager` can actually place.

    A `time` stop is the case worth naming. It is a real stop type and it has no
    price level, so it arms nothing on the position and — the part that bites —
    it cannot make a `risk_pct` entry sizeable, because there is no distance to
    measure risk against. Configured together, those two settings produce a week
    of refusals.
    """
    try:
        stop = resolve_stop_config(config)
    except (ATPError, ValueError) as exc:
        return Check("stop type", Status.FAIL, str(exc), fix="set the stop type to ATR")
    if stop.stop_type is StopType.TIME:
        return Check(
            "stop type",
            Status.WARN,
            "a time stop places no price level — nothing rests at the venue, "
            "so SAFETY.md layer 5 is not exercised by this run",
            source=SAFETY_CHECKLIST,
        )
    # A non-positive multiplier is refused by `WorkerConfig` itself now, so this
    # cannot be reached from a stored row. Kept because a caller can construct
    # the value object in other ways, and a stop at or through the entry price
    # is the one misconfiguration here that loses money rather than time.
    if config.stop_multiplier <= 0:
        return Check(
            "stop type",
            Status.FAIL,
            f"stop multiplier {config.stop_multiplier} is not positive",
            fix="set the stop multiplier to 2",
        )
    return Check(
        "stop type",
        Status.PASS,
        f"{stop.stop_type.value} x {config.stop_multiplier}",
    )


def check_alert_transport(settings: Settings) -> Check:
    """Somewhere for a halt to reach a human.

    A WARN, never a FAIL: alerting is explicitly *not* one of SAFETY.md's
    layers, every layer acts on its own, and a run with no transport configured
    is still a valid run. It is worth a line because the checklist asks for one
    and because the thing an operator discovers on day three is that nothing
    told them the worker halted on day one.
    """
    if settings.alert_ntfy_topic or (
        settings.alert_telegram_token and settings.alert_telegram_chat_id
    ):
        return Check("alerts", Status.PASS, "a transport is configured")
    return Check(
        "alerts",
        Status.WARN,
        "no alert transport — a halt will reach a log file and nothing else",
        fix="set ALERT_NTFY_TOPIC, then: uv run python scripts/check_alerts.py --by you",
        source=SAFETY_CHECKLIST,
    )


# ── what the week will actually run on ──────────────────────────────────────


def check_warmup(symbol: str, *, required: int, stored: int, newest: datetime | None) -> Check:
    """Enough stored history for the strategy to have an opinion.

    Ranked fourth on docs/FIRST_PAPER_RUN.md's own list of what breaks first,
    and the only one on that list that is entirely knowable before the week
    starts. `runner.warmup_short_history` warns per symbol at boot and
    `LiveContext.history` raises for whoever needs the missing bars — both are
    the right behaviours and both happen after the operator has committed the
    week.

    Short history is a FAIL rather than a WARN because of what it produces: not
    an error, but *no signals*, which is also what a correct run of a crossover
    strategy produces most weeks. A result nobody can interpret is worse than a
    refusal.
    """
    if stored == 0:
        return Check(
            f"history {symbol}",
            Status.FAIL,
            f"no stored bars for {symbol}",
            fix=f"uv run python scripts/backfill_bars.py --symbols {symbol} --verify",
        )
    if stored < required:
        return Check(
            f"history {symbol}",
            Status.FAIL,
            f"{stored} stored bars, strategy needs {required} to warm up — "
            f"it would decide on nothing and the week would report silence",
            fix=f"uv run python scripts/backfill_bars.py --symbols {symbol} --verify",
        )
    detail = f"{stored} bars, needs {required}"
    if newest is not None:
        detail = f"{detail}, newest {newest.date().isoformat()}"
    return Check(f"history {symbol}", Status.PASS, detail)


def check_quote_freshness(symbol: str, *, age_seconds: float | None, budget: int) -> Check:
    """The prices orders would be priced against.

    Against the saved `max_quote_age_seconds` — the same budget `StaleDataRule`
    refuses orders on — so this answers the question the rule will ask rather
    than a similar-looking one.

    A missing quote is a WARN and not a FAIL, because the honest reading depends
    on when you run this: pre-market, no quote is correct and expected, and a
    preflight that refused would be unusable at the hour it is most useful. What
    it must not do is stay silent, because a strategy started on a cold cache
    spends its first session being refused by a rule that names data rather than
    the strategy.
    """
    if age_seconds is None:
        return Check(
            f"quotes {symbol}",
            Status.WARN,
            "no quote cached — correct before the open, a dead feed after it",
            fix="check the worker is up and data.stream.started appeared",
        )
    if age_seconds > budget:
        return Check(
            f"quotes {symbol}",
            Status.WARN,
            f"last quote is {age_seconds:.0f}s old against a {budget}s budget — "
            f"StaleDataRule refuses orders on exactly this",
            fix="check the market-data stream before starting the strategy",
        )
    return Check(f"quotes {symbol}", Status.PASS, f"{age_seconds:.0f}s old, budget {budget}s")


def check_not_halted(halts: list[HaltRecord]) -> Check:
    """Nothing is halted — at any scope.

    `active_halts` rather than a global-only read, because a symbol-scoped halt
    left over from a previous incident is the one that would go unnoticed: the
    worker starts, the loop runs, and one name silently never trades. The
    watchlist is usually one symbol during a first paper run, which makes that
    the whole run.

    `active_halts` lets a Redis failure raise rather than answering "nothing" —
    for the reason its docstring gives, and the caller renders that as SKIP.
    A halt found here is a FAIL: the week produces refusals and nothing else.
    """
    if not halts:
        return Check("kill switch", Status.PASS, "clear at every scope")
    rendered = ", ".join(
        f"{h.scope.value}{f' {h.target}' if h.target else ''} by {h.engaged_by}" for h in halts
    )
    return Check(
        "kill switch",
        Status.FAIL,
        f"halted: {rendered}",
        fix='uv run python scripts/halt.py clear --by "<your name>"',
    )


# ── the one that produces a silent week ─────────────────────────────────────


def check_sizing_is_reachable(
    config: WorkerConfig,
    limits: RiskLimits,
    *,
    equity: Decimal,
    price: Decimal,
    stop_price: Decimal | None,
) -> Check:
    """Would the first entry survive its own position cap?

    This is the check that exists because of a real interaction rather than a
    hypothetical one. `risk_pct` sizes by the distance to the stop, so a wide
    stop buys a large position *by construction*: 1% of $100,000 against a 2×ATR
    stop on a ~$97 name asks for 305 shares — 29.5% of the account against a 10%
    the `max_position_pct` ceiling — and `max_position_size` then refuses the entry
    whole rather than trimming it. Both numbers are right. They measure
    different things, and the pair is docs/RISK.md's own recommendation.

    What that produces over a week is a worker that runs perfectly and never
    fills anything, which docs/FIRST_PAPER_RUN.md warns is indistinguishable
    from a strategy that never signalled — "a week of no signals is not a week
    of correct trading". Ninety seconds of arithmetic now decides which of those
    the week is going to be.

    Sized through `position_size`, never re-derived: the number this predicts
    has to be the number the router computes, or the prediction is about a
    different platform (ADR 0006).
    """
    method = config.sizing_method
    value = config.sizing_value
    try:
        qty = position_size(method, equity, price, stop_price=stop_price, risk_pct=value)
    except ValueError as exc:
        return Check(
            "sizing",
            Status.FAIL,
            f"{method} cannot size an entry here: {exc}",
            fix="on the Config tab, set sizing to fixed_qty with a value of 1 for a first run",
            source="docs/FIRST_PAPER_RUN.md, 'Stage 2'",
        )
    if qty <= 0:
        return Check(
            "sizing",
            Status.FAIL,
            f"{method} sizes the first entry at {qty} shares — nothing would be submitted",
            fix="raise the sizing value on the Config tab, or use fixed_qty for a first run",
        )

    notional = qty * price
    cap = limits.max_position_pct * equity
    share = notional / equity if equity else Decimal(0)
    if notional > cap:
        return Check(
            "sizing",
            Status.FAIL,
            f"{method} asks for {qty} shares at {price} = {share:.1%} of equity, and "
            f"the max position ceiling caps a position at {limits.max_position_pct:.0%} — "
            f"max_position_size would refuse every entry, and the week would look silent",
            fix=(
                f"lower the sizing value on the Config tab (about "
                f"{value * limits.max_position_pct * equity / notional:.4f} fits), "
                f"or raise the max position ceiling in the risk limits below it"
            ),
            source="docs/RISK.md, 'Why risk_pct is the default'",
        )
    return Check(
        "sizing",
        Status.PASS,
        f"{method} sizes the first entry at {qty} shares = {share:.1%} of equity, "
        f"under the {limits.max_position_pct:.0%} cap",
    )


def check_account(
    *,
    trading_blocked: bool,
    is_pattern_day_trader: bool,
    equity: Decimal,
    buying_power: Decimal,
    is_paper_host: bool,
) -> Check:
    """The venue's own view of the account.

    `trading_blocked` is the one that matters and the one nothing else in this
    platform reads: a restricted account accepts a submit and refuses it, so a
    week against one produces refusals whose reason lives at Alpaca rather than
    in any log here — which is the hardest kind of silence to attribute.

    The pattern-day-trader flag under $25,000 is a WARN and belongs on this list
    because docs/SAFETY.md's account checklist asks for it to be *understood*
    rather than avoided. Paper accounts enforce it, so a strategy that
    round-trips daily can exhaust its day trades mid-week and spend the rest of
    the run being refused for a reason that has nothing to do with the strategy.
    """
    if not is_paper_host:
        return Check(
            "account",
            Status.FAIL,
            "this account was read from a host that is not the paper endpoint",
            fix="ATP_RUN_MODE=paper",
        )
    if trading_blocked:
        return Check(
            "account",
            Status.FAIL,
            "the venue reports trading_blocked — a submit would be accepted and refused there",
            fix="resolve it in Alpaca's own UI; nothing here can",
        )
    detail = f"equity {equity}, buying power {buying_power}"
    if is_pattern_day_trader and equity < PDT_EQUITY_FLOOR:
        return Check(
            "account",
            Status.WARN,
            f"{detail} — flagged PDT under ${PDT_EQUITY_FLOOR:,}, so day trades are "
            f"rationed and a mid-week refusal may be the rule rather than the strategy",
            source=SAFETY_CHECKLIST,
        )
    return Check("account", Status.PASS, detail)
