"""Authentication: the failure paths, mostly.

The happy path is one line and is covered incidentally by every other API test.
What matters here is everything that must *not* work, because an authentication
bug does not announce itself — it looks exactly like a system that is working
until someone who should not be inside is.

Design and rationale: ADR 0008.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from atp_api.auth import (
    MAX_PASSWORD_BYTES,
    PasswordTooLongError,
    authenticate,
    create_session_token,
    hash_password,
    read_session_token,
    verify_password,
)
from atp_core.config import Settings

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
        assert read_session_token(token, settings, NOW) == "operator"

    def test_a_token_is_valid_up_to_its_expiry_and_not_after(self) -> None:
        settings = settings_for(api_session_hours=12)
        token = create_session_token("operator", settings, NOW)

        just_inside = NOW + timedelta(hours=12) - timedelta(seconds=1)
        just_outside = NOW + timedelta(hours=12) + timedelta(seconds=1)

        assert read_session_token(token, settings, just_inside) == "operator"
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
