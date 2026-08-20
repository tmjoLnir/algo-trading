"""`POST /api/v1/risk/halt` — the dashboard's emergency stop, over ASGI.

A unit test rather than an integration one: the kill switch is behind a port, so
the whole handler runs against a fake with no Redis (CLAUDE.md §1.7).

The theme is that **a halt button must never overstate what it achieved.** The
button is the one acting control on the dashboard, and the two ways it can
mislead are opposite and both bad: reporting success when nothing was written,
and reporting failure in a way that reads as "trading continues" when the switch
has already failed closed. Most of what follows pins the wording and the status
code of that distinction, because those *are* the safety property here — the
happy path is one Redis write.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import SecretStr

from atp_api.auth import Scope, Session, hash_password
from atp_api.deps import get_audit_sink, get_clock, get_current_session, get_kill_switch
from atp_api.main import create_app
from atp_core.audit.ports import Action, AuditEntry
from atp_core.clock import SimulatedClock
from atp_core.config import Settings, get_settings
from atp_core.risk.killswitch import HaltReason, HaltScope
from tests.fakes import FakeKillSwitch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

HALT = "/api/v1/risk/halt"
RESUME = "/api/v1/risk/resume"

#: The step-up password these cases present. `/resume` re-checks it against the
#: settings, so unlike `/halt` these need settings with a real hash in them.
PASSWORD = "a-perfectly-ordinary-password"

NOW = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)


def pinned_settings() -> Settings:
    """Settings that do not depend on the shell the suite is run from.

    `_env_file=None` because a developer's own `.env` must never reach a test.
    """
    return Settings(ATP_RUN_MODE="backtest", _env_file=None)


class RecordingAuditSink:
    """An `AuditSink` that keeps what it was given."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)

    async def recent(
        self,
        limit: int = 100,
        before_id: int | None = None,
        action: str | None = None,
    ) -> list[tuple[int, AuditEntry]]:
        return []


def resume_settings(password: str = PASSWORD) -> Settings:
    """`pinned_settings` plus a credential the step-up can actually check.

    `api_user` matches the session the `app` fixture installs: `authenticate`
    compares the *session's* user against the configured one, so a mismatch here
    would make every resume a 403 for the wrong reason and quietly turn the
    happy-path cases into duplicates of the refusal ones.
    """
    return Settings(
        ATP_RUN_MODE="backtest",
        api_user="test-operator",
        api_secret_key=SecretStr("k" * 64),
        api_password_hash=SecretStr(hash_password(password)),
        _env_file=None,
    )


class UnclearableKillSwitch(FakeKillSwitch):
    """A kill switch whose Redis is gone, on the way *out* of a halt.

    Separate from `UnreachableKillSwitch` because the two failures mean opposite
    things and the handlers must say so: a failed engage leaves trading about to
    resume on its own, a failed clear leaves it stopped.
    """

    def clear(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")


class UnreachableKillSwitch(FakeKillSwitch):
    """A kill switch whose Redis is gone.

    `RedisKillSwitch.engage` deliberately does not swallow its exceptions — a
    halt that failed quietly is the worst outcome this system has — so this
    stands in for that, and the handler has to decide what an operator is told.
    """

    def engage(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")


@pytest.fixture
def kill_switch() -> FakeKillSwitch:
    return FakeKillSwitch()


@pytest.fixture
def audit() -> RecordingAuditSink:
    return RecordingAuditSink()


@pytest.fixture
def app(kill_switch: FakeKillSwitch, audit: RecordingAuditSink) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_settings] = pinned_settings
    application.dependency_overrides[get_clock] = lambda: SimulatedClock(NOW)
    application.dependency_overrides[get_kill_switch] = lambda: kill_switch
    application.dependency_overrides[get_audit_sink] = lambda: audit
    # A FULL session by default. Scope is not what these are about — that is
    # `tests/unit/test_api_contract.py`, from the outside, against every route
    # at once — except for the one case below that is specifically about it.
    application.dependency_overrides[get_current_session] = lambda: Session(
        "test-operator", Scope.FULL
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # No lifespan: `ASGITransport` does not run one, which is what keeps this a
    # unit test with no Redis pool behind it.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


class TestHalting:
    async def test_an_empty_body_stops_everything(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        """The whole point of the endpoint: a client that knows only "stop".

        The dashboard sends a scope and a reason, but the defaults are what make
        this callable by `curl` at three in the morning by someone who does not
        remember the schema.
        """
        response = await client.post(HALT, json={})

        assert response.status_code == 200
        assert kill_switch.is_engaged() is True
        body = response.json()
        assert body["scope"] == HaltScope.GLOBAL.value
        assert body["reason"] == HaltReason.MANUAL.value

    async def test_the_dashboards_own_payload_works(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        """Exactly what `KillSwitchButton.tsx` posts.

        Pinned as its own case because this is the payload that actually reaches
        the endpoint in production, and it went four phases without a test while
        the handler raised `NotImplementedError` underneath it.
        """
        response = await client.post(HALT, json={"scope": "global", "reason": "manual"})

        assert response.status_code == 200
        assert kill_switch.is_engaged() is True

    async def test_the_halt_is_attributed_to_the_session_not_the_payload(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        """An actor a request can name is not an audit trail.

        The body has no `engaged_by` field at all, and this is what keeps it
        that way: a caller who invents one is ignored, and the name on the
        record is the session's.
        """
        response = await client.post(HALT, json={"engaged_by": "somebody-else"})

        assert response.json()["engaged_by"] == "test-operator"
        assert kill_switch.engagements[0][2] == "test-operator"

    async def test_a_narrowed_halt_carries_its_target(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        response = await client.post(HALT, json={"scope": "symbol", "target": "SPY"})

        assert response.status_code == 200
        assert response.json()["target"] == "SPY"

    async def test_the_reason_reaches_the_record(self, client: httpx.AsyncClient) -> None:
        """The automated reasons are usable from the API too.

        They belong to the subsystems that detect them, but nothing about the
        endpoint should refuse an operator who has diagnosed the outage faster
        than the detector did.
        """
        response = await client.post(HALT, json={"reason": "data_feed_lost"})

        assert response.json()["reason"] == HaltReason.DATA_FEED_LOST.value


class TestAlreadyHalted:
    """Engaging twice. The record must keep the *first* stop, not the latest."""

    async def test_a_second_halt_returns_the_original_record(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        """Who stopped trading, and when, is the question asked afterwards.

        `engage` is idempotent and returns the original record rather than
        overwriting it. The endpoint must pass that through unchanged: an
        operator who halts something already halted has to be able to see that
        somebody else got there first, and when.
        """
        first = await client.post(HALT, json={"detail": "feed looks wrong"})

        app.dependency_overrides[get_current_session] = lambda: Session("someone-else", Scope.FULL)
        second = await client.post(HALT, json={"detail": "wait, is this halted?"})

        assert second.status_code == 200
        assert second.json()["engaged_by"] == "test-operator", (
            "the second caller must be shown who actually stopped trading"
        )
        assert second.json()["detail"] == "feed looks wrong"
        assert second.json()["engaged_at"] == first.json()["engaged_at"]

    async def test_the_audit_row_says_it_was_already_halted(
        self, app: FastAPI, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        await client.post(HALT, json={})

        app.dependency_overrides[get_current_session] = lambda: Session("someone-else", Scope.FULL)
        await client.post(HALT, json={})

        assert audit.entries[0].detail["already_halted_by_another"] is False
        assert audit.entries[1].detail["already_halted_by_another"] is True

    async def test_the_same_operator_halting_twice_claims_nothing(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """The flag says what it can stand behind and no more.

        Derived from the identity on the returned record, not from a
        read-then-write that would race. That makes it one-directional: a
        different name is proof somebody else halted first, while the same name
        is not proof they did not — the same operator halting twice is
        indistinguishable from halting once.
        """
        await client.post(HALT, json={})
        await client.post(HALT, json={})

        assert [e.detail["already_halted_by_another"] for e in audit.entries] == [False, False]


class TestTheAuditRow:
    async def test_halting_is_recorded(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        await client.post(HALT, json={"scope": "strategy", "target": "sma_crossover"})

        assert len(audit.entries) == 1
        entry = audit.entries[0]
        assert entry.action == Action.HALT_ENGAGED
        assert entry.actor == "test-operator"
        assert entry.target == "sma_crossover"
        assert entry.detail["scope"] == HaltScope.STRATEGY.value

    async def test_the_row_records_what_was_asked_not_the_halt_already_in_force(
        self, app: FastAPI, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """An audit row is an account of what a person did.

        When a halt is already active, `engage` returns that record untouched —
        so a row built from it would stamp the *first* halt's reason onto the
        second person. An operator pressing the button during an automated
        `data_feed_lost` halt acted for their own reasons, and attributing a
        machine's diagnosis to a human is exactly the kind of thing an
        append-only record must not do.
        """
        await client.post(HALT, json={"reason": "data_feed_lost", "detail": "no ticks"})

        app.dependency_overrides[get_current_session] = lambda: Session("someone-else", Scope.FULL)
        await client.post(HALT, json={"reason": "manual", "detail": "stopping to look"})

        assert audit.entries[1].detail["reason"] == HaltReason.MANUAL.value
        assert audit.entries[1].detail["detail"] == "stopping to look"
        assert audit.entries[1].detail["already_halted_by_another"] is True

    async def test_a_failed_halt_writes_no_row(
        self, app: FastAPI, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """An entry for a halt that did not take is worse than no entry.

        Whoever reviews the incident reads the row as "we stopped at 14:30".
        The row is written after the engage returns, so a failure leaves the
        record silent rather than wrong.
        """
        app.dependency_overrides[get_kill_switch] = UnreachableKillSwitch

        await client.post(HALT, json={})

        assert audit.entries == []

    async def test_an_unwritable_audit_log_does_not_block_the_halt(
        self, app: FastAPI, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        """The failure modes must not be inverted.

        A missing row is a gap in the record; a refused halt is a position
        nobody can close. `get_audit_sink` falls back to a sink that logs and
        drops when there is no database, and the halt has to go through it.
        """
        del app.dependency_overrides[get_audit_sink]  # no session_factory on app.state

        response = await client.post(HALT, json={})

        assert response.status_code == 200
        assert kill_switch.is_engaged() is True


class TestWhenTheStoreIsGone:
    """The failure path, which is the one that can mislead an operator."""

    @pytest.fixture
    def app(self, app: FastAPI) -> FastAPI:
        app.dependency_overrides[get_kill_switch] = UnreachableKillSwitch
        return app

    async def test_it_is_a_503(self, client: httpx.AsyncClient) -> None:
        response = await client.post(HALT, json={})

        assert response.status_code == 503

    async def test_it_says_the_halt_was_not_recorded(self, client: httpx.AsyncClient) -> None:
        """Not "halted", and not a bare "failed" either.

        Both halves have to be in the message, because the operator is in an
        unusual state that neither word describes: the switch fails closed, so
        nothing is trading *right now*; but nothing was written, so trading
        resumes on its own the moment the store recovers. A reader told only
        "could not halt" will stop the worker unnecessarily; a reader told only
        "halted" will walk away.
        """
        detail = (await client.post(HALT, json={})).json()["detail"]

        assert "NOT recorded" in detail
        assert "fails closed" in detail
        assert "resumes" in detail

    async def test_it_names_the_underlying_error(self, client: httpx.AsyncClient) -> None:
        """So the person reading it can tell a dead Redis from a wrong password."""
        detail = (await client.post(HALT, json={})).json()["detail"]

        assert "Connection refused" in detail


class TestRefusals:
    """Bad requests, refused rather than interpreted."""

    async def test_a_narrowed_scope_needs_a_target(self, client: httpx.AsyncClient) -> None:
        """Otherwise the halt is keyed on nothing and stops nothing.

        The dangerous part is not the failure, it is that the halt banner would
        render as though something had been stopped.
        """
        response = await client.post(HALT, json={"scope": "symbol"})

        assert response.status_code == 422

    async def test_a_global_halt_refuses_a_target(self, client: httpx.AsyncClient) -> None:
        """`{"scope": "global", "target": "SPY"}` is someone expecting one symbol
        to stop. It would stop everything, so it is refused rather than obeyed."""
        response = await client.post(HALT, json={"scope": "global", "target": "SPY"})

        assert response.status_code == 422

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"scope": "everything"}, id="unknown-scope"),
            pytest.param({"reason": "because"}, id="unknown-reason"),
        ],
    )
    async def test_unknown_enum_values_are_refused(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch, payload: dict[str, str]
    ) -> None:
        """A halt recorded under a reason nothing queries for is a halt nobody
        finds afterwards. 422 names the values that exist."""
        response = await client.post(HALT, json=payload)

        assert response.status_code == 422
        assert kill_switch.is_engaged() is False

    async def test_a_refused_request_does_not_halt(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch, audit: RecordingAuditSink
    ) -> None:
        """Validation runs before the switch is touched. Stated as its own case
        because a 422 that had already halted trading would be the worst kind of
        surprise — the error says nothing happened."""
        await client.post(HALT, json={"scope": "strategy"})

        assert kill_switch.is_engaged() is False
        assert kill_switch.engagements == []
        assert audit.entries == []


class TestReadOnlySessions:
    async def test_a_read_only_session_may_still_stop_trading(
        self, app: FastAPI, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        """The one place the write-scope rule bends, and it bends on purpose.

        `deps.READ_ONLY_MAY_CALL` names this route; docs/RISK.md is the reason —
        the person watching the book from a phone is exactly who most needs to
        stop it and least needs to place an order. `test_api_contract.py` pins
        that the route is *not refused*; this pins that it actually works, which
        is a different claim and was false until the handler existed.
        """
        app.dependency_overrides[get_current_session] = lambda: Session("reader", Scope.READ)

        response = await client.post(HALT, json={})

        assert response.status_code == 200
        assert kill_switch.is_engaged() is True
        assert response.json()["engaged_by"] == "reader"


class TestResuming:
    """`POST /api/v1/risk/resume` — the deliberate half of the pair.

    The theme is the mirror of the halting one above: **a resume must never
    overstate what it achieved either**, and the ways it can mislead are the
    reverse. Reporting "resumed" when the delete never landed leaves an operator
    walking away from a stopped platform; reporting failure when it did land
    leaves them re-clearing something already clear. Between those sits the case
    with no counterpart on the way in — a clear that found nothing engaged,
    which is a success and reads exactly like the first one unless the body
    says otherwise.
    """

    @pytest.fixture
    def app(self, app: FastAPI) -> FastAPI:
        app.dependency_overrides[get_settings] = resume_settings
        return app

    @staticmethod
    def halted(kill_switch: FakeKillSwitch) -> None:
        """Put a halt in force the way the risk layer would have."""
        kill_switch.engage(HaltScope.GLOBAL, HaltReason.DAILY_LOSS_LIMIT, "risk", "-3.2%")

    async def test_it_resumes_trading(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        self.halted(kill_switch)

        response = await client.post(RESUME, json={"password": PASSWORD})

        assert response.status_code == 200
        assert kill_switch.is_engaged() is False
        assert response.json()["was_halted"] is True

    async def test_the_body_describes_the_halt_that_was_removed(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        """Not an acknowledgement — the record that was overridden.

        The reason is the field worth returning: an operator who cleared what
        they thought was their own manual halt, and is told they cleared
        `daily_loss_limit`, has just learned the risk layer stopped trading for
        a reason of its own and that they have overruled it.
        """
        self.halted(kill_switch)

        body = (await client.post(RESUME, json={"password": PASSWORD})).json()

        assert body["reason"] == HaltReason.DAILY_LOSS_LIMIT.value
        assert body["engaged_by"] == "risk"
        assert body["detail"] == "-3.2%"
        assert body["cleared_by"] == "test-operator"

    async def test_clearing_nothing_is_a_success_that_says_so(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        """The case with no counterpart on the halt side.

        `clear` is deliberately not an error when nothing was engaged, so this
        cannot be a 404 — but a bare 200 would read as "resumed" to a caller
        who believed the platform was stopped, and the useful answer is that
        whatever they were reacting to is not a halt.
        """
        response = await client.post(RESUME, json={"password": PASSWORD})

        assert response.status_code == 200
        body = response.json()
        assert body["was_halted"] is False
        assert body["reason"] is None
        assert body["engaged_by"] is None

    async def test_the_resume_is_attributed_to_the_session_not_the_payload(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        """The same rule the halt row follows, and it matters more here.

        "Who decided it was safe to trade again" is the question the whole
        asymmetry exists to answer (ADR 0008). A `cleared_by` the request could
        set would let the answer be anything the caller liked.
        """
        self.halted(kill_switch)

        body = (
            await client.post(RESUME, json={"password": PASSWORD, "cleared_by": "somebody-else"})
        ).json()

        assert body["cleared_by"] == "test-operator"
        assert kill_switch.clears == [(HaltScope.GLOBAL.value, "test-operator", None)]

    async def test_a_narrowed_resume_clears_only_its_target(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        kill_switch.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        kill_switch.engage(HaltScope.SYMBOL, HaltReason.DATA_FEED_LOST, "worker", target="SPY")

        response = await client.post(
            RESUME, json={"password": PASSWORD, "scope": "symbol", "target": "SPY"}
        )

        assert response.status_code == 200
        assert response.json()["was_halted"] is True
        assert kill_switch.clears == [(HaltScope.SYMBOL.value, "test-operator", "SPY")]


class TestTheResumeAuditRow:
    @pytest.fixture
    def app(self, app: FastAPI) -> FastAPI:
        app.dependency_overrides[get_settings] = resume_settings
        return app

    async def test_resuming_is_recorded(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch, audit: RecordingAuditSink
    ) -> None:
        """The counterpart to `halt_engaged`, and the reason the pair is needed:
        a halt with no clear beside it is still in force."""
        kill_switch.engage(HaltScope.GLOBAL, HaltReason.DAILY_LOSS_LIMIT, "risk", "-3.2%")

        await client.post(RESUME, json={"password": PASSWORD})

        (entry,) = audit.entries
        assert entry.action == Action.HALT_CLEARED
        assert entry.actor == "test-operator"
        assert entry.at == NOW
        assert entry.detail["was_halted"] is True
        assert entry.detail["original_reason"] == HaltReason.DAILY_LOSS_LIMIT.value
        assert entry.detail["originally_engaged_by"] == "risk"

    async def test_a_clear_that_found_nothing_is_still_recorded(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """Somebody with the password decided the platform should be trading.

        That decision belongs in the record whether or not it turned out to be
        necessary — and if nothing was halted, whatever they were reacting to
        was somewhere else, which is worth being able to find afterwards.
        """
        await client.post(RESUME, json={"password": PASSWORD})

        (entry,) = audit.entries
        assert entry.action == Action.HALT_CLEARED
        assert entry.detail["was_halted"] is False
        assert entry.detail["original_reason"] is None

    async def test_a_failed_resume_writes_no_row(
        self, app: FastAPI, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """The mirror of `test_a_failed_halt_writes_no_row`, and worse if broken.

        A row claiming trading resumed when the delete never landed has whoever
        reads it stop looking for the thing still stopping the platform.
        """
        app.dependency_overrides[get_kill_switch] = UnclearableKillSwitch

        await client.post(RESUME, json={"password": PASSWORD})

        assert audit.entries == []

    async def test_a_wrong_password_is_recorded(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """`Action.FORBIDDEN` has always claimed to cover a failed step-up.

        It did not until now — `require_write_scope` wrote its half from the
        start and this end wrote nothing. The gap is the one that matters: a
        wrong password here is either a typo or somebody working through guesses
        with a stolen cookie, `rate_limited` only ever counted attempts at the
        login form, and without this row the second case leaves no trace.
        """
        response = await client.post(RESUME, json={"password": "wrong"})

        assert response.status_code == 403
        (entry,) = audit.entries
        assert entry.action == Action.FORBIDDEN
        assert entry.actor == "test-operator"
        assert entry.target == RESUME
        assert entry.detail["reason"] == "step_up_failed"

    async def test_a_wrong_password_does_not_resume(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        kill_switch.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        await client.post(RESUME, json={"password": "wrong"})

        assert kill_switch.is_engaged() is True
        assert kill_switch.clears == []


class TestWhenTheStoreIsGoneOnTheWayOut:
    """The failure path, whose message is the inverse of the halting one."""

    @pytest.fixture
    def app(self, app: FastAPI) -> FastAPI:
        app.dependency_overrides[get_settings] = resume_settings
        app.dependency_overrides[get_kill_switch] = UnclearableKillSwitch
        return app

    async def test_it_is_a_503(self, client: httpx.AsyncClient) -> None:
        response = await client.post(RESUME, json={"password": PASSWORD})

        assert response.status_code == 503

    async def test_it_says_trading_is_still_stopped(self, client: httpx.AsyncClient) -> None:
        """The opposite reassurance to the one `/halt` gives on failure.

        A failed halt leaves trading about to resume by itself and the message
        has to raise the alarm. A failed resume leaves the halt standing, which
        is the safe direction — and saying so plainly is what stops an operator
        guessing they are now half resumed and re-clearing into a state nobody
        understands.
        """
        detail = (await client.post(RESUME, json={"password": PASSWORD})).json()["detail"]

        assert "NOT resumed" in detail
        assert "still in force" in detail
        assert "failed safe" in detail

    async def test_it_names_the_underlying_error(self, client: httpx.AsyncClient) -> None:
        detail = (await client.post(RESUME, json={"password": PASSWORD})).json()["detail"]

        assert "Connection refused" in detail


class TestResumeRefusals:
    """Bad requests, refused rather than interpreted — as on the way in."""

    @pytest.fixture
    def app(self, app: FastAPI) -> FastAPI:
        app.dependency_overrides[get_settings] = resume_settings
        return app

    async def test_an_unknown_scope_is_a_422_not_a_500(self, client: httpx.AsyncClient) -> None:
        """`scope` is the enum for this reason.

        As a bare string it reached `HaltScope(...)` inside the handler, and a
        typo became an unhandled `ValueError` — a 500 with nothing in the body.
        An operator clearing a halt cannot be asked to guess whether that meant
        "you misspelled it" or "the platform is broken".
        """
        response = await client.post(RESUME, json={"password": PASSWORD, "scope": "globl"})

        assert response.status_code == 422

    async def test_a_narrowed_scope_needs_a_target(self, client: httpx.AsyncClient) -> None:
        response = await client.post(RESUME, json={"password": PASSWORD, "scope": "symbol"})

        assert response.status_code == 422

    async def test_a_global_resume_refuses_a_target(self, client: httpx.AsyncClient) -> None:
        """The same pairing `/halt` enforces, and it has to be the same.

        A halt is keyed on (scope, target) and cleared by that pair. A resume
        that accepted a combination the halt refuses could only ever be clearing
        a halt that cannot exist — and would answer "nothing was halted", which
        reads exactly like "trading is fine".
        """
        response = await client.post(
            RESUME, json={"password": PASSWORD, "scope": "global", "target": "SPY"}
        )

        assert response.status_code == 422

    async def test_a_refused_request_does_not_resume(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        kill_switch.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")

        await client.post(RESUME, json={"password": PASSWORD, "scope": "globl"})

        assert kill_switch.is_engaged() is True
        assert kill_switch.clears == []

    async def test_a_read_only_session_may_not_resume(
        self, app: FastAPI, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        """Where the bend in the write-scope rule stops.

        `deps.READ_ONLY_MAY_CALL` names `/halt` and nothing else, on purpose:
        the person watching from a phone is who most needs to stop trading and
        is the last who should be able to start it again. Nothing in the handler
        enforces this — `require_write_scope` refuses the route by default — so
        this pins the default rather than a line of code, which is exactly the
        thing a later edit could remove without noticing.
        """
        kill_switch.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "ops")
        app.dependency_overrides[get_current_session] = lambda: Session("reader", Scope.READ)

        response = await client.post(RESUME, json={"password": PASSWORD})

        assert response.status_code == 403
        assert kill_switch.is_engaged() is True
