"""`GET` and `PUT /api/v1/worker/config` over ASGI.

A unit test: both sources are behind ports, so the whole route runs against
fakes with no database and no Redis (CLAUDE.md §1.7).

Three things are worth holding here, and they are the three reasons this
endpoint is not just a form:

1. **Saved and running are different facts.** A worker reads its configuration
   once, at start. The response carries both and the difference between them,
   because a screen that showed only the saved row would report settings no
   process is using — and the operator would not restart, because nothing told
   them to.
2. **Arming the live lock costs a password.** `allow_live_orders` is the third
   of the three live-money locks and it now lives somewhere a session cookie can
   reach. ADR 0009's answer is that the proof travels with the act. Turning it
   *off* asks for nothing, and a test asserts that too — a lock that made
   stopping harder would be worse than no lock.
3. **A refusal is the value object's sentence, not a generic 422.** The same
   rules refuse the same edit at the worker's next boot, so the message an
   operator reads here has to be the one that would have stopped the process.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import SecretStr

from atp_api.auth import Scope, Session, hash_password
from atp_api.deps import (
    get_audit_sink,
    get_current_session,
    get_worker_config_repository,
    get_worker_status_store,
)
from atp_api.main import create_app
from atp_core.audit.ports import Action
from atp_core.config import Settings, get_settings
from atp_core.domain import RunMode
from atp_core.risk.limits import RiskLimits
from atp_core.worker import RunningWorkerConfig, StoredWorkerConfig, WorkerConfig
from tests.fakes import (
    FakeWorkerConfigRepository,
    FakeWorkerStatusStore,
    RecordingAuditSink,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

CONFIG = "/api/v1/worker/config"
PASSWORD = "a-perfectly-ordinary-password"
NOW = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)


def live_settings() -> Settings:
    """Live mode with a checkable credential.

    `api_user` matches the session the `app` fixture installs: `authenticate`
    compares the session's user against the configured one, so a mismatch would
    make every step-up a 403 for the wrong reason and quietly turn the
    happy-path cases into duplicates of the refusal ones.
    """
    return Settings(
        ATP_RUN_MODE="live",
        ATP_ALLOW_LIVE_TRADING=True,
        alpaca_api_key=SecretStr("k"),
        alpaca_api_secret=SecretStr("s"),
        api_user="test-operator",
        api_secret_key=SecretStr("k" * 64),
        api_password_hash=SecretStr(hash_password(PASSWORD)),
        _env_file=None,
    )


#: The eight ceilings as `.env` used to ship them, which is what
#: `DEFAULT_RISK_LIMITS` holds — so a payload carrying these saves a
#: configuration indistinguishable from the one this platform had before they
#: were editable, and the assertions here stay about what the endpoint does.
DEFAULT_RISK: dict[str, Any] = {
    "max_position_pct": "0.10",
    "max_gross_exposure_pct": "1.00",
    "max_daily_loss_pct": "0.03",
    "max_orders_per_minute": 30,
    "max_open_positions": 20,
    "max_quote_age_seconds": 30,
    "default_stop_loss_pct": "0.02",
    "default_take_profit_pct": "0.06",
}


def a_payload(**overrides: Any) -> dict[str, Any]:
    """A complete, valid body. Every field, always — the endpoint takes no
    partial update, because a stop multiplier silently retaining an old value
    because a browser did not consider its input dirty is the surprise this row
    must not have."""
    body: dict[str, Any] = {
        "symbols": ["SPY", "QQQ"],
        "max_silence_seconds": 60,
        "strategy": "sma_crossover",
        "strategy_params": {},
        "timeframe": "1m",
        "sizing_method": "risk_pct",
        "sizing_value": "0.01",
        "stop_type": "atr",
        "stop_multiplier": "2",
        "stop_period": 14,
        "allow_live_orders": False,
        # The ceilings ride along in the same body, and every field of them is
        # required for the same reason the rest are: a partial risk section
        # would let a browser's idea of "dirty" decide which limit stayed put.
        "risk": DEFAULT_RISK,
    }
    body.update(overrides)
    return body


def a_stored(**overrides: Any) -> StoredWorkerConfig:
    config = WorkerConfig(symbols=("SPY",), strategy="sma_crossover", **overrides)
    return StoredWorkerConfig(config=config, revision=7, updated_at=NOW, updated_by="somebody")


@pytest.fixture
def repo() -> FakeWorkerConfigRepository:
    return FakeWorkerConfigRepository()


@pytest.fixture
def status() -> FakeWorkerStatusStore:
    return FakeWorkerStatusStore()


@pytest.fixture
def audit() -> RecordingAuditSink:
    return RecordingAuditSink()


@pytest.fixture
def app(
    repo: FakeWorkerConfigRepository,
    status: FakeWorkerStatusStore,
    audit: RecordingAuditSink,
) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_settings] = live_settings
    application.dependency_overrides[get_worker_config_repository] = lambda: repo
    application.dependency_overrides[get_worker_status_store] = lambda: status
    application.dependency_overrides[get_audit_sink] = lambda: audit
    application.dependency_overrides[get_current_session] = lambda: Session(
        "test-operator", Scope.FULL
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestReadingIt:
    async def test_nothing_saved_reports_the_defaults_as_never_saved(
        self, client: httpx.AsyncClient
    ) -> None:
        """Revision 0, not 1. "Nothing has been saved" and "somebody saved
        something that happens to equal the defaults" are different facts, and
        only the second one is evidence a person looked."""
        body = (await client.get(CONFIG)).json()

        assert body["saved"]["revision"] == 0
        assert body["saved"]["updated_by"] is None
        assert body["saved"]["config"]["strategy"] == ""
        assert body["saved"]["config"]["symbols"] == []

    async def test_no_worker_reporting_is_not_a_pending_restart(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        """There is nothing to be pending against. A screen saying "restart
        required" with no process to restart sends a reader after the wrong
        thing."""
        repo.stored = a_stored()
        body = (await client.get(CONFIG)).json()

        assert body["running"] is None
        assert body["pending_restart"] is False

    async def test_a_worker_on_an_older_revision_is_a_pending_restart(
        self,
        client: httpx.AsyncClient,
        repo: FakeWorkerConfigRepository,
        status: FakeWorkerStatusStore,
    ) -> None:
        repo.stored = a_stored()
        await status.put(
            RunMode.LIVE,
            RunningWorkerConfig(
                config=WorkerConfig(),
                revision=6,
                started_at=NOW,
                trading=False,
                reason="no strategy is configured",
            ),
        )
        body = (await client.get(CONFIG)).json()

        assert body["pending_restart"] is True
        assert body["running"]["revision"] == 6
        # The worker's own sentence, not one the API re-derived.
        assert body["running"]["reason"] == "no strategy is configured"

    async def test_a_worker_on_the_saved_revision_is_in_force(
        self,
        client: httpx.AsyncClient,
        repo: FakeWorkerConfigRepository,
        status: FakeWorkerStatusStore,
    ) -> None:
        repo.stored = a_stored()
        await status.put(
            RunMode.LIVE,
            RunningWorkerConfig(
                config=repo.stored.config,
                revision=7,
                started_at=NOW,
                trading=True,
                reason="trading sma_crossover",
            ),
        )
        assert (await client.get(CONFIG)).json()["pending_restart"] is False

    async def test_the_dropdowns_come_with_the_screen(self, client: httpx.AsyncClient) -> None:
        """One request, not four. Assembled from separate ones they could
        disagree about which strategies exist."""
        options = (await client.get(CONFIG)).json()["options"]

        assert "sma_crossover" in {s["value"] for s in options["strategies"]}
        assert "risk_pct" in {s["value"] for s in options["sizing_methods"]}
        assert "atr" in {s["value"] for s in options["stop_types"]}
        # The form relabels its multiplier input from this rather than owning a
        # copy of the list.
        assert options["multiplier_stops"] == ["atr", "chandelier"]

    async def test_decimals_are_strings(self, client: httpx.AsyncClient) -> None:
        """Both scale money — one sizes every order, the other places the stop —
        and neither may pass through a JSON float (rule §1.1)."""
        config = (await client.get(CONFIG)).json()["saved"]["config"]

        assert config["sizing_value"] == "0.01"
        assert config["stop_multiplier"] == "2"


class TestSavingIt:
    async def test_a_save_stores_and_returns_the_new_screen(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        response = await client.put(CONFIG, json=a_payload())

        assert response.status_code == 200
        assert response.json()["saved"]["revision"] == 1
        assert repo.saves[-1].symbols == ("SPY", "QQQ")

    async def test_symbols_are_normalised_on_the_way_in(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        """A text box hands over what was typed. The ingestor rejects anything
        but an uppercase ticker."""
        await client.put(CONFIG, json=a_payload(symbols=[" spy ", "qqq", "spy"]))

        assert repo.saves[-1].symbols == ("SPY", "QQQ")

    async def test_the_saver_is_recorded(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        await client.put(CONFIG, json=a_payload())

        assert repo.stored is not None
        assert repo.stored.updated_by == "test-operator"

    async def test_what_changed_is_in_the_audit_row(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """ "The worker configuration was saved" answers nothing a post-mortem
        asks. "risk_pct went from 0.01 to 0.02, saved by josh" answers most of
        it."""
        await client.put(CONFIG, json=a_payload(sizing_value="0.02"))

        entry = audit.entries[-1]
        assert entry.action == Action.WORKER_CONFIG_UPDATED
        assert entry.actor == "test-operator"
        assert entry.detail["changes"]["sizing_value"] == {"from": "0.01", "to": "0.02"}
        assert entry.detail["changes"]["strategy"] == {"from": "", "to": "sma_crossover"}

    async def test_an_unchanged_save_still_moves_the_revision(
        self, client: httpx.AsyncClient
    ) -> None:
        """ "Somebody looked at this and pressed save" is a fact worth keeping,
        and a revision that only moved on a diff would make the restart notice
        depend on what changed rather than on when."""
        await client.put(CONFIG, json=a_payload())
        second = await client.put(CONFIG, json=a_payload())

        assert second.json()["saved"]["revision"] == 2


class TestRefusals:
    async def test_an_unregistered_strategy_names_the_ones_that_exist(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        """Saving a typo would be discovered by a worker failing to boot."""
        response = await client.put(CONFIG, json=a_payload(strategy="nope"))

        assert response.status_code == 400
        assert "sma_crossover" in response.json()["detail"]
        assert repo.saves == []

    async def test_a_fractional_stop_of_two_is_refused_in_the_value_objects_words(
        self, client: httpx.AsyncClient
    ) -> None:
        """The same sentence the worker would refuse to boot with — which is the
        point of the rules living in one place."""
        response = await client.put(
            CONFIG, json=a_payload(stop_type="fixed_pct", stop_multiplier="2")
        )

        assert response.status_code == 400
        assert "200" in response.json()["detail"]

    async def test_risk_above_the_backstop_is_refused(self, client: httpx.AsyncClient) -> None:
        response = await client.put(CONFIG, json=a_payload(sizing_value="0.5"))

        assert response.status_code == 400
        assert "10%" in response.json()["detail"]

    async def test_an_empty_strategy_is_allowed_and_means_no_orders(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        """Not a refusal. Choosing to trade nothing is a configuration, and it
        is the default one."""
        response = await client.put(CONFIG, json=a_payload(strategy=""))

        assert response.status_code == 200
        assert repo.saves[-1].trades is False


class TestTheThirdLock:
    async def test_arming_it_without_a_password_is_refused(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        """A session cookie proves somebody signed in this morning, not that
        anybody is at the keyboard now (ADR 0009)."""
        response = await client.put(CONFIG, json=a_payload(allow_live_orders=True))

        assert response.status_code == 403
        assert repo.saves == []

    async def test_a_wrong_password_is_recorded_as_a_failed_step_up(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """A typo and somebody working through guesses with a stolen cookie look
        identical at the moment of refusal. Only the record tells them apart."""
        await client.put(CONFIG, json=a_payload(allow_live_orders=True, password="wrong"))

        entry = audit.entries[-1]
        assert entry.action == Action.FORBIDDEN
        assert entry.detail["reason"] == "step_up_failed"

    async def test_the_right_password_arms_it(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        """A lock that refused everything would pass the two tests above and be
        found only by an operator who could not turn the platform on."""
        response = await client.put(
            CONFIG, json=a_payload(allow_live_orders=True, password=PASSWORD)
        )

        assert response.status_code == 200
        assert repo.saves[-1].allow_live_orders is True

    async def test_turning_it_off_asks_for_nothing(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        """The same asymmetry `/halt` and `/resume` have: stopping must never be
        the harder direction."""
        repo.stored = a_stored(allow_live_orders=True)

        response = await client.put(CONFIG, json=a_payload(allow_live_orders=False))

        assert response.status_code == 200
        assert repo.saves[-1].allow_live_orders is False

    async def test_editing_something_else_while_it_is_already_armed_asks_for_nothing(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        """The password is for *arming*, not for holding. Asking on every save
        of an already-live configuration would train an operator to type it,
        which is how a step-up stops meaning anything."""
        repo.stored = a_stored(allow_live_orders=True)

        response = await client.put(
            CONFIG, json=a_payload(allow_live_orders=True, sizing_value="0.02")
        )

        assert response.status_code == 200
        assert repo.saves[-1].sizing_value == Decimal("0.02")

    async def test_a_read_only_session_cannot_save_at_all(
        self, app: FastAPI, repo: FakeWorkerConfigRepository
    ) -> None:
        """Refused before the handler runs, by the scope rule that covers every
        mutating request rather than by anything written here."""
        app.dependency_overrides[get_current_session] = lambda: Session("test-operator", Scope.READ)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.put(CONFIG, json=a_payload())

        assert response.status_code == 403
        assert repo.saves == []


class TestTheRiskCeilings:
    """The eight ceilings, on the same screen and in the same save.

    Their being here at all is the change: they were `RISK_*` environment
    variables, unreachable from a browser and unattributable to anybody. What
    these cases hold is that moving them did not cost the two things `.env`
    never had — a refusal that names the field, and a record of who moved which
    number and from what — and did not buy something it should not have, namely
    a second password prompt in front of *tightening* a limit.
    """

    async def test_they_are_served_with_the_saved_configuration(
        self, client: httpx.AsyncClient
    ) -> None:
        body = (await client.get(CONFIG)).json()

        assert body["saved"]["config"]["risk"]["max_position_pct"] == "0.10"
        assert body["saved"]["config"]["risk"]["max_open_positions"] == 20

    async def test_the_fractions_are_strings(self, client: httpx.AsyncClient) -> None:
        """Each is multiplied by equity to produce the number an order is
        refused against, so none of them passes through a JSON float."""
        risk = (await client.get(CONFIG)).json()["saved"]["config"]["risk"]

        for name in ("max_position_pct", "max_gross_exposure_pct", "max_daily_loss_pct"):
            assert isinstance(risk[name], str), name

    async def test_the_form_catalogue_is_served_with_them(self, client: httpx.AsyncClient) -> None:
        """The dashboard renders its boxes from this rather than from a list of
        its own, so the prose beside a number that stops real money cannot drift
        from the argument for it."""
        fields = (await client.get(CONFIG)).json()["options"]["risk_fields"]

        assert {f["name"] for f in fields} == {
            "max_position_pct",
            "max_gross_exposure_pct",
            "max_daily_loss_pct",
            "max_orders_per_minute",
            "max_open_positions",
            "max_quote_age_seconds",
            "default_stop_loss_pct",
            "default_take_profit_pct",
        }
        assert all(f["help"] for f in fields)

    async def test_a_save_stores_them(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        tighter = {**DEFAULT_RISK, "max_position_pct": "0.04", "max_open_positions": 5}

        response = await client.put(CONFIG, json=a_payload(risk=tighter))

        assert response.status_code == 200
        assert repo.saves[-1].risk.max_position_pct == Decimal("0.04")
        assert repo.saves[-1].risk.max_open_positions == 5

    async def test_a_fraction_arrives_as_a_decimal_not_a_float(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        """The value stored is the value typed. `Decimal(0.07)` is
        0.070000000000000006...; a ceiling is not allowed to become that."""
        await client.put(
            CONFIG, json=a_payload(risk={**DEFAULT_RISK, "max_daily_loss_pct": "0.07"})
        )

        assert repo.saves[-1].risk.max_daily_loss_pct == Decimal("0.07")

    async def test_a_bad_ceiling_is_refused_in_the_value_objects_words(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        """400 with the sentence `RiskLimits` wrote — the same one the worker
        would print refusing to start on the row, because it is the same rule."""
        response = await client.put(
            CONFIG, json=a_payload(risk={**DEFAULT_RISK, "max_open_positions": 0})
        )

        assert response.status_code == 400
        assert "max_open_positions" in response.json()["detail"]
        assert repo.saves == []

    async def test_a_position_limit_above_the_gross_limit_is_refused(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        response = await client.put(
            CONFIG,
            json=a_payload(
                risk={**DEFAULT_RISK, "max_position_pct": "0.9", "max_gross_exposure_pct": "0.5"}
            ),
        )

        assert response.status_code == 400
        assert repo.saves == []

    async def test_a_missing_risk_section_is_refused(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        """No partial update, for the reason the rest of the body has none: a
        ceiling silently retaining its old value because a browser did not think
        the box was dirty is the surprise this row must not have."""
        body = a_payload()
        del body["risk"]

        response = await client.put(CONFIG, json=body)

        assert response.status_code == 422
        assert repo.saves == []

    async def test_what_changed_names_the_ceiling_and_both_numbers(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """The whole reason these left `.env`. "The configuration was saved"
        answers nothing a post-mortem asks; "max_position_pct went from 0.10 to
        0.25, saved by josh at 14:32" answers most of it."""
        await client.put(CONFIG, json=a_payload(risk={**DEFAULT_RISK, "max_position_pct": "0.25"}))

        changes = audit.entries[-1].detail["changes"]
        assert changes["risk.max_position_pct"] == {"from": "0.10", "to": "0.25"}

    async def test_an_unchanged_ceiling_is_not_in_the_diff(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """Eight rows saying "unchanged" is how the one that moved gets missed."""
        await client.put(CONFIG, json=a_payload(risk={**DEFAULT_RISK, "max_position_pct": "0.25"}))

        changes = audit.entries[-1].detail["changes"]
        assert [k for k in changes if k.startswith("risk.")] == ["risk.max_position_pct"]

    async def test_tightening_one_asks_for_no_password(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository
    ) -> None:
        """The asymmetry that keeps the step-up meaningful. `allow_live_orders`
        grants a capability; these bound orders that are already permitted, and
        making a limit *harder* to tighten is the wrong direction — the same
        reason `/halt` asks for nothing."""
        response = await client.put(
            CONFIG, json=a_payload(risk={**DEFAULT_RISK, "max_position_pct": "0.02"})
        )

        assert response.status_code == 200
        assert repo.saves[-1].risk.max_position_pct == Decimal("0.02")

    async def test_loosening_one_asks_for_no_password_either_but_is_recorded(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """Stated rather than assumed, because it is the direction that can lose
        money. The answer is not a prompt — it is that the audit row carries
        both numbers and the operator's name, which is what makes a loosening
        answerable afterwards."""
        response = await client.put(
            CONFIG, json=a_payload(risk={**DEFAULT_RISK, "max_position_pct": "0.95"})
        )

        assert response.status_code == 200
        entry = audit.entries[-1]
        assert entry.actor == "test-operator"
        assert entry.detail["changes"]["risk.max_position_pct"]["to"] == "0.95"

    async def test_the_running_worker_reports_its_own_ceilings(
        self, client: httpx.AsyncClient, repo: FakeWorkerConfigRepository, status: Any
    ) -> None:
        """The gap this screen exists to show, applied to the ceilings. A worker
        enforces what it booted with; a save since then is not in force for it
        until it restarts, and the screen must be able to show both numbers."""
        booted = WorkerConfig(
            symbols=("SPY",),
            strategy="sma_crossover",
            risk=RiskLimits(max_position_pct=Decimal("0.02")),
        )
        # LIVE because that is the run mode these settings pin: the status
        # store is keyed by it, and a report filed under another mode is a
        # report this screen correctly cannot see.
        await status.put(
            RunMode.LIVE,
            RunningWorkerConfig(
                config=booted, revision=6, started_at=NOW, trading=True, reason="trading"
            ),
        )
        repo.stored = a_stored(risk=RiskLimits(max_position_pct=Decimal("0.5")))

        body = (await client.get(CONFIG)).json()

        assert body["running"]["config"]["risk"]["max_position_pct"] == "0.02"
        assert body["saved"]["config"]["risk"]["max_position_pct"] == "0.5"
        assert body["pending_restart"] is True
