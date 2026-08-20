"""Authentication: the failure paths, mostly.

The happy path is one line and is covered incidentally by every other API test.
What matters here is everything that must *not* work, because an authentication
bug does not announce itself — it looks exactly like a system that is working
until someone who should not be inside is.

Design and rationale: ADR 0008.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from jose import jwt
from pydantic import SecretStr

from atp_api.auth import (
    ALGORITHM,
    MAX_PASSWORD_BYTES,
    PasswordTooLongError,
    Scope,
    Session,
    authenticate,
    create_session_token,
    hash_password,
    looks_like_bcrypt_hash,
    read_session_token,
    signing_key,
    verify_password,
)
from atp_api.deps import get_current_session
from atp_api.main import create_app
from atp_core.config import Settings, get_settings

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
PASSWORD = "a-perfectly-ordinary-password"


def settings_for(password: str | None = PASSWORD, **overrides: object) -> Settings:
    """Settings with a real hash in them.

    `_env_file=None` for the reason `test_dashboard_api.pinned_settings` gives:
    a developer's own `.env` must never reach a test.
    """
    base: dict[str, object] = {
        "api_user": "operator",
        "api_secret_key": SecretStr("k" * 64),
        "api_password_hash": SecretStr(hash_password(password) if password else ""),
        "_env_file": None,
    }
    return Settings(**(base | overrides))  # type: ignore[arg-type]


class TestPasswordHashing:
    def test_a_password_verifies_against_its_own_hash(self) -> None:
        assert verify_password(PASSWORD, hash_password(PASSWORD))

    def test_a_different_password_does_not(self) -> None:
        assert not verify_password("something else", hash_password(PASSWORD))

    def test_the_hash_is_salted(self) -> None:
        """Two hashes of the same password differ, and both verify.

        An unsalted scheme lets one precomputed table cover every deployment
        that chose the same password.
        """
        first, second = hash_password(PASSWORD), hash_password(PASSWORD)
        assert first != second
        assert verify_password(PASSWORD, first)
        assert verify_password(PASSWORD, second)

    def test_an_empty_stored_hash_refuses_everything(self) -> None:
        """The unconfigured deployment must have no valid password at all.

        The dangerous reading of "no password set" is "any password works". This
        pins the other one.
        """
        assert not verify_password(PASSWORD, "")
        assert not verify_password("", "")

    def test_a_malformed_stored_hash_is_a_refusal_not_a_crash(self) -> None:
        """A mangled `.env` value is a failed login, not a 500.

        A 500 on a malformed hash tells whoever is probing that they have found
        a path the ordinary one does not take.
        """
        assert not verify_password(PASSWORD, "clearly-not-a-bcrypt-hash")

    def test_a_password_longer_than_bcrypt_accepts_is_refused_not_truncated(self) -> None:
        """bcrypt stops at 72 bytes; truncating would be the dangerous fix.

        Silently trimming makes every password sharing a 72-byte prefix the same
        password. Refusing is the honest behaviour, at both ends.
        """
        too_long = "x" * (MAX_PASSWORD_BYTES + 1)
        with pytest.raises(PasswordTooLongError):
            hash_password(too_long)
        assert not verify_password(too_long, hash_password("x" * MAX_PASSWORD_BYTES))


class TestSessionTokens:
    def test_a_token_round_trips_to_its_subject(self) -> None:
        settings = settings_for()
        token = create_session_token("operator", settings, NOW)
        assert read_session_token(token, settings, NOW) == Session("operator", Scope.FULL)

    def test_a_token_is_valid_up_to_its_expiry_and_not_after(self) -> None:
        settings = settings_for(api_session_hours=12)
        token = create_session_token("operator", settings, NOW)

        just_inside = NOW + timedelta(hours=12) - timedelta(seconds=1)
        just_outside = NOW + timedelta(hours=12) + timedelta(seconds=1)

        assert read_session_token(token, settings, just_inside) == Session("operator", Scope.FULL)
        assert read_session_token(token, settings, just_outside) is None

    def test_expiry_is_judged_against_the_passed_clock_not_the_wall_clock(self) -> None:
        """The reason expiry is checked here rather than inside `jose`.

        A library reading the system clock cannot be pinned, and CLAUDE.md §1.2
        exists because that class of bug is the hardest here to notice.
        """
        settings = settings_for()
        token = create_session_token("operator", settings, NOW)
        long_after = NOW + timedelta(days=3650)
        assert read_session_token(token, settings, long_after) is None

    def test_a_token_signed_with_another_key_is_refused(self) -> None:
        """The whole point of signing. A forged cookie must not authenticate."""
        theirs = settings_for(api_secret_key=SecretStr("k" * 64))
        ours = settings_for(api_secret_key=SecretStr("j" * 64))
        token = create_session_token("operator", theirs, NOW)
        assert read_session_token(token, ours, NOW) is None

    @pytest.mark.parametrize(
        "bad",
        ["", "not-a-token", "a.b.c", "eyJhbGciOiJub25lIn0..", "   "],
        ids=["empty", "garbage", "three-empty-segments", "alg-none", "whitespace"],
    )
    def test_a_malformed_token_is_refused(self, bad: str) -> None:
        assert read_session_token(bad, settings_for(), NOW) is None

    def test_a_tampered_payload_is_refused(self) -> None:
        """Flipping a character in the payload breaks the signature."""
        settings = settings_for()
        token = create_session_token("operator", settings, NOW)
        head, payload, signature = token.split(".")
        tampered = f"{head}.{payload[:-2]}{'AA' if payload[-2:] != 'AA' else 'BB'}.{signature}"
        assert read_session_token(tampered, settings, NOW) is None


class TestAuthenticate:
    def test_the_configured_operator_with_the_right_password(self) -> None:
        assert authenticate("operator", PASSWORD, settings_for()) == "operator"

    def test_the_wrong_password(self) -> None:
        assert authenticate("operator", "wrong", settings_for()) is None

    def test_the_wrong_username(self) -> None:
        assert authenticate("someone-else", PASSWORD, settings_for()) is None

    def test_no_configured_hash_refuses_every_login(self) -> None:
        """Fail closed. An unconfigured deployment has no way in, not a free one."""
        unconfigured = settings_for(password=None)
        assert authenticate("operator", PASSWORD, unconfigured) is None
        assert authenticate("operator", "", unconfigured) is None
        assert authenticate("", "", unconfigured) is None

    def test_a_renamed_operator_is_honoured(self) -> None:
        settings = settings_for(api_user="joshua")
        assert authenticate("joshua", PASSWORD, settings) == "joshua"
        assert authenticate("operator", PASSWORD, settings) is None


class TestScopes:
    """What a session may do, which is not the same question as who holds it."""

    def test_a_session_is_full_unless_asked_otherwise(self) -> None:
        settings = settings_for()
        token = create_session_token("operator", settings, NOW)
        session = read_session_token(token, settings, NOW)
        assert session is not None
        assert session.scope is Scope.FULL
        assert session.may_act

    def test_a_read_only_session_round_trips_as_one(self) -> None:
        settings = settings_for()
        token = create_session_token("operator", settings, NOW, Scope.READ)
        session = read_session_token(token, settings, NOW)
        assert session is not None
        assert session.scope is Scope.READ
        assert not session.may_act

    def test_the_scope_is_signed_and_cannot_be_edited_by_its_holder(self) -> None:
        """The property the whole design rests on.

        A read-only session whose holder can promote it is a full session with a
        preference. Re-signing the payload with a different scope requires the
        key, and forging it without one must not validate.
        """
        settings = settings_for()
        read_token = create_session_token("operator", settings, NOW, Scope.READ)

        claims = jwt.decode(
            read_token,
            signing_key(settings),
            algorithms=[ALGORITHM],
            options={"verify_exp": False},
        )
        assert claims["scp"] == "read"

        forged = jwt.encode({**claims, "scp": "full"}, "a-key-we-do-not-have", algorithm=ALGORITHM)
        assert read_session_token(forged, settings, NOW) is None

        # And re-signed with the real key it *would* be full — which is the
        # point: only something holding the key can say so.
        legitimate = jwt.encode(
            {**claims, "scp": "full"}, signing_key(settings), algorithm=ALGORITHM
        )
        session = read_session_token(legitimate, settings, NOW)
        assert session is not None and session.scope is Scope.FULL

    @pytest.mark.parametrize("claim", [None, "", "admin", "FULL", "superuser", 7])
    def test_an_absent_or_unknown_scope_falls_back_to_read(self, claim: object) -> None:
        """Fail closed.

        A token minted before scopes existed, or carrying a value this version
        does not know, is downgraded rather than trusted. The cost of guessing
        wrong this way is a disabled button; the cost the other way is an
        irreversible action taken by a session that was never granted it.
        """
        settings = settings_for()
        payload: dict[str, object] = {
            "sub": "operator",
            "iat": int(NOW.timestamp()),
            "exp": int((NOW + timedelta(hours=1)).timestamp()),
        }
        if claim is not None:
            payload["scp"] = claim
        token = jwt.encode(payload, signing_key(settings), algorithm=ALGORITHM)

        session = read_session_token(token, settings, NOW)
        assert session is not None
        assert session.scope is Scope.READ


class TestStepUp:
    """Re-presenting the password for the two acts that cannot be undone."""

    @staticmethod
    def client(settings: Settings) -> httpx.AsyncClient:
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_current_session] = lambda: Session("operator", Scope.FULL)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        )

    async def test_resume_without_the_password_is_rejected_by_the_schema(self) -> None:
        async with self.client(settings_for()) as client:
            response = await client.post("/api/v1/risk/resume", json={"scope": "global"})
        assert response.status_code == 422

    async def test_resume_with_the_wrong_password_is_forbidden(self) -> None:
        async with self.client(settings_for()) as client:
            response = await client.post(
                "/api/v1/risk/resume", json={"scope": "global", "password": "wrong"}
            )
        assert response.status_code == 403

    async def test_resume_with_the_right_password_passes_the_gate(self) -> None:
        """A valid session and the password get through to the handler.

        The handler is still a stub, so "through" is a 500 rather than a 200 —
        which is the honest assertion to make here. A test expecting 200 would
        be testing an implementation that does not exist yet.
        """
        async with self.client(settings_for()) as client:
            response = await client.post(
                "/api/v1/risk/resume", json={"scope": "global", "password": PASSWORD}
            )
        assert response.status_code != 403

    async def test_flatten_all_needs_the_password_as_well_as_the_phrase(self) -> None:
        """Both proofs, not either.

        The phrase shows the caller knows what the button does. The password
        shows they are entitled to press it. A copied cookie plus a phrase read
        off the documentation is not enough.
        """
        async with self.client(settings_for()) as client:
            wrong = await client.post(
                "/api/v1/risk/flatten-all",
                json={"confirm": "FLATTEN ALL POSITIONS", "password": "wrong"},
            )
            right = await client.post(
                "/api/v1/risk/flatten-all",
                json={"confirm": "FLATTEN ALL POSITIONS", "password": PASSWORD},
            )
        assert wrong.status_code == 403
        assert right.status_code != 403

    async def test_the_password_is_never_a_query_parameter(self) -> None:
        """It must travel in the body. nginx logs query strings verbatim."""
        spec = create_app().openapi()
        for path in ("/api/v1/risk/resume", "/api/v1/risk/flatten-all"):
            for operation in spec["paths"][path].values():
                names = {p["name"] for p in operation.get("parameters", [])}
                assert "password" not in names, f"{path} takes the password in the URL"


class TestLooksLikeBcryptHash:
    """The check that makes one silent misconfiguration audible.

    `.env` is read by Docker Compose, which interpolates `$NAME`. A bcrypt hash
    is `$2b$12$<salt><digest>`, so a hash pasted without single quotes has its
    `$` and the salt's leading letters substituted away with a blank string.
    What arrives is non-empty — so the startup check for an *unset* hash says
    nothing — and is not a hash, so every login is refused. The operator sees a
    correct password rejected and nothing anywhere saying why.

    `verify_password` already refuses these correctly; this is about saying so
    at startup instead of at the login screen.
    """

    #: Exactly what compose leaves behind, captured from `docker compose config`
    #: against a `.env` holding an unquoted hash whose salt began `hnn.`. The
    #: `$hnn` is gone; `$2b` and `$12` survive because compose does not read a
    #: `$` followed by a digit as a variable.
    COMPOSE_MANGLED = "$2b$12.KpQ8vZ3rT1uW9xYzAeL5mNbCdEfGhIjKlMnOpQrStUvWxYz12"

    def test_a_real_hash_is_accepted(self) -> None:
        assert looks_like_bcrypt_hash(hash_password(PASSWORD))

    def test_the_compose_mangled_hash_is_caught(self) -> None:
        """The whole point. This is a real value from a real `.env`."""
        assert not looks_like_bcrypt_hash(self.COMPOSE_MANGLED)
        # And it is genuinely unusable, which is why it has to be caught.
        assert not verify_password(PASSWORD, self.COMPOSE_MANGLED)

    def test_a_dollar_escaped_hash_is_caught(self) -> None:
        """`$$`-escaping is the usual advice for compose and is wrong here.

        pydantic-settings does not interpolate, so it hands the doubled `$`s
        straight through and the hash never verifies. An operator who "fixed"
        the compose warning that way must be told, not left with the same
        silent lockout by a different route.
        """
        doubled = hash_password(PASSWORD).replace("$", "$$")
        assert not looks_like_bcrypt_hash(doubled)
        assert not verify_password(PASSWORD, doubled)

    def test_an_empty_hash_is_not_structurally_valid(self) -> None:
        """Not the caller's concern — `main` checks empty first and says
        something different about it — but the predicate must not call the
        unconfigured state a hash."""
        assert not looks_like_bcrypt_hash("")

    @pytest.mark.parametrize("variant", ["2a", "2b", "2x", "2y"])
    def test_every_bcrypt_variant_prefix_is_accepted(self, variant: str) -> None:
        """A hash made by another tool, or an older one of ours, is still a
        hash. Rejecting it would invent an outage this check exists to prevent.
        """
        real = hash_password(PASSWORD)
        assert looks_like_bcrypt_hash(f"${variant}${real.split('$', 2)[2]}")
