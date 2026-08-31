"""What lands in the audit trail, and what deliberately does not.

The adapter's storage behaviour is `tests/integration/test_audit_log.py`. This
is about the events themselves: which actions are recorded, who they are
attributed to, and — the part easiest to get subtly wrong — the difference
between an identity the server verified and a username the caller typed.

Design: ADR 0010.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr

from atp_api.auth import Scope, Session, hash_password
from atp_api.deps import _DroppedAuditLog, get_audit_sink, get_current_session, get_rate_limiter
from atp_api.main import create_app
from atp_api.ratelimit import AlwaysAllows
from atp_core.audit import Action, AuditEntry
from atp_core.config import Settings, get_settings
from atp_core.persistence.audit import PostgresAuditLog
from atp_core.persistence.db import create_engine, create_session_factory

PASSWORD = "a-perfectly-ordinary-password"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class RecordingSink:
    """Captures entries instead of storing them."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)

    async def recent(
        self, limit: int = 100, before_id: int | None = None, action: str | None = None
    ) -> list[tuple[int, AuditEntry]]:
        return list(enumerate(self.entries))

    def actions(self) -> list[str]:
        return [entry.action for entry in self.entries]

    def only(self) -> AuditEntry:
        assert len(self.entries) == 1, f"expected exactly one entry, got {self.actions()}"
        return self.entries[0]


class RefusingSink:
    """A badly-behaved sink, for pinning that the port's contract is a contract."""

    async def record(self, entry: AuditEntry) -> None:
        raise RuntimeError("this sink is broken")

    async def recent(
        self, limit: int = 100, before_id: int | None = None, action: str | None = None
    ) -> list[tuple[int, AuditEntry]]:
        return []


def settings_for(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "api_user": "operator",
        "api_secret_key": SecretStr("k" * 64),
        "api_password_hash": SecretStr(hash_password(PASSWORD)),
        "_env_file": None,
    }
    return Settings(**(base | overrides))


def client_for(sink: Any, settings: Settings, session: Session | None = None) -> httpx.AsyncClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_audit_sink] = lambda: sink
    app.dependency_overrides[get_rate_limiter] = lambda: AlwaysAllows()
    if session is not None:
        app.dependency_overrides[get_current_session] = lambda: session
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


class TestSigningIn:
    async def test_a_successful_login_is_recorded_against_the_verified_user(self) -> None:
        sink = RecordingSink()
        async with client_for(sink, settings_for()) as client:
            response = await client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": PASSWORD},
                headers={"X-Forwarded-For": "203.0.113.7"},
            )

        assert response.status_code == 200
        entry = sink.only()
        assert entry.action == Action.LOGIN
        assert entry.actor == "operator"
        assert entry.target == "203.0.113.7"
        assert entry.detail["scope"] == "full"

    async def test_the_requested_scope_is_recorded(self) -> None:
        """Which kind of session was handed out is part of what happened."""
        sink = RecordingSink()
        async with client_for(sink, settings_for()) as client:
            await client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": PASSWORD, "read_only": True},
            )

        assert sink.only().detail["scope"] == "read"

    async def test_a_failed_login_is_not_attributed_to_the_username_that_was_typed(self) -> None:
        """The distinction the whole record depends on.

        `actor` means "who we know this was". Before a login succeeds nobody is
        known, so writing the typed username there would let anyone put any name
        in the audit trail by failing to log in as them — which is the opposite
        of what the column is for. The claim goes in `detail`, where it reads as
        a claim.
        """
        sink = RecordingSink()
        async with client_for(sink, settings_for()) as client:
            response = await client.post(
                "/api/v1/auth/login",
                json={"username": "someone-else", "password": "wrong"},
                headers={"X-Forwarded-For": "203.0.113.7"},
            )

        assert response.status_code == 401
        entry = sink.only()
        assert entry.action == Action.LOGIN_FAILED
        assert entry.actor == "anonymous"
        assert entry.actor != "someone-else"
        assert entry.detail["username"] == "someone-else"
        assert entry.target == "203.0.113.7"

    async def test_the_password_never_reaches_the_record(self) -> None:
        """Not in `detail`, not in `target`, not anywhere.

        An audit trail that stored attempted passwords would be a list of
        near-misses for the real one, kept in the database, readable from a
        screen (CLAUDE.md §1.6).
        """
        sink = RecordingSink()
        async with client_for(sink, settings_for()) as client:
            await client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": "hunter2-is-my-password"},
            )

        assert "hunter2-is-my-password" not in str(sink.only())


class TestSigningOut:
    async def test_a_real_session_signing_out_is_recorded(self) -> None:
        sink = RecordingSink()
        settings = settings_for()
        async with client_for(sink, settings) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": PASSWORD},
            )
            assert login.status_code == 200
            sink.entries.clear()
            await client.post("/api/v1/auth/logout")

        entry = sink.only()
        assert entry.action == Action.LOGOUT
        assert entry.actor == "operator"

    async def test_signing_out_without_a_session_records_nothing(self) -> None:
        """Clearing a cookie that was already invalid is not an event.

        A row saying "anonymous logged out" would be noise in a record whose
        whole value is that everything in it means something — and this endpoint
        is unauthenticated, so anyone can call it as often as they like.
        """
        sink = RecordingSink()
        async with client_for(sink, settings_for()) as client:
            response = await client.post("/api/v1/auth/logout")

        assert response.status_code == 204
        assert sink.entries == []


class TestRefusals:
    async def test_a_read_only_session_refused_a_write_is_recorded(self) -> None:
        """Either the operator forgot which session they were in, or a cookie is
        somewhere it should not be. Both look identical at the moment of refusal.
        """
        sink = RecordingSink()
        session = Session("operator", Scope.READ)
        async with client_for(sink, settings_for(), session) as client:
            response = await client.post("/api/v1/orders", json={})

        assert response.status_code == 403
        entry = sink.only()
        assert entry.action == Action.FORBIDDEN
        assert entry.actor == "operator"
        assert entry.target == "/api/v1/orders"
        assert entry.detail == {"method": "POST", "scope": "read"}

    async def test_a_permitted_read_records_nothing(self) -> None:
        """The record is of consequential actions, not of traffic.

        A row per GET would bury the events worth reading under the dashboard's
        own reads.
        """
        sink = RecordingSink()
        session = Session("operator", Scope.READ)
        async with client_for(sink, settings_for(), session) as client:
            await client.get("/api/v1/positions")

        assert sink.entries == []

    async def test_a_full_session_writing_records_nothing_here(self) -> None:
        """Only the *refusal* is this dependency's business.

        Recording the action itself belongs with the handler that performs it —
        and every one of those is still a stub (ADR 0010).
        """
        sink = RecordingSink()
        session = Session("operator", Scope.FULL)
        async with client_for(sink, settings_for(), session) as client:
            await client.post("/api/v1/orders", json={})

        assert sink.entries == []


class TestTheSinkContract:
    """`AuditSink.record` never raises — the shipped implementations honour it."""

    async def test_the_dropped_sink_does_not_raise(self) -> None:
        await _DroppedAuditLog().record(AuditEntry(at=NOW, actor="operator", action=Action.LOGIN))

    async def test_the_postgres_sink_does_not_raise_when_it_cannot_connect(self) -> None:
        engine = create_engine("postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nope")
        try:
            await PostgresAuditLog(create_session_factory(engine)).record(
                AuditEntry(at=NOW, actor="operator", action=Action.LOGIN)
            )
        finally:
            await engine.dispose()

    async def test_a_sink_that_breaks_the_contract_breaks_the_request(self) -> None:
        """Documented rather than defended against, deliberately.

        The guarantee lives in the port and is honoured by both implementations
        above. Wrapping every call site in a try/except would spread the
        responsibility across the codebase to defend against a sink nobody has
        written — and would hide the bug in the one that eventually does.
        """
        async with client_for(RefusingSink(), settings_for()) as client:
            response = await client.post(
                "/api/v1/auth/login",
                json={"username": "operator", "password": PASSWORD},
            )

        assert response.status_code == 500
