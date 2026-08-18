"""Authentication: one operator, a bcrypt hash, and a signed session cookie.

The whole of it. There is no users table, deliberately — see ADR 0008. This
platform is run by one person, and a `users` table with CRUD behind it would be
machinery standing in for a requirement nobody has.

**Why a cookie and not a bearer header.** The dashboard holds a WebSocket, and a
browser cannot set `Authorization` on a WebSocket handshake. Bearer therefore
forces the token into a query string — where nginx writes it to the access log,
in plain text, for every reconnect — or into an abuse of `Sec-WebSocket-Protocol`.
A cookie is sent on the handshake automatically. `HttpOnly` also puts it out of
reach of any script on the page, which `localStorage` cannot do, and the
same-origin work (docs/DASHBOARD.md) already removed the cross-site awkwardness
that usually argues against cookies.

**Why `bcrypt` directly and not `passlib[bcrypt]`, which this package declared.**
passlib 1.7.4 is from 2020 and reads `bcrypt.__about__.__version__` to detect its
backend; bcrypt removed that attribute, so on bcrypt 5 passlib fails its own
backend load and then raises on every `hash()` call. The declared dependency does
not run. bcrypt's own API is four lines of the same thing, so this uses it and
`passlib` is dropped rather than pinned to a 2020 release for its own sake.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import bcrypt
from jose import JWTError, jwt

from atp_core.config import Settings
from atp_core.errors import ATPError
from atp_core.logging import get_logger

log = get_logger(__name__)

#: Name of the session cookie. Prefixed because a bare `session` is the first
#: thing a drive-by script guesses, and it costs nothing to be specific.
COOKIE_NAME = "atp_session"

ALGORITHM = "HS256"

#: bcrypt hashes at most 72 bytes and, since 4.1, *raises* rather than silently
#: truncating. Truncation was the more dangerous behaviour — two distinct long
#: passwords sharing a prefix hash identically — so this refuses at the same
#: boundary rather than trimming on the caller's behalf.
MAX_PASSWORD_BYTES = 72


class PasswordTooLongError(ATPError):
    """A password bcrypt cannot hash without silently losing part of it.

    Derives from `ATPError` like everything raised on purpose here (CLAUDE.md
    §4), so a caller can tell a deliberate refusal from a genuine bug.
    """


def hash_password(password: str) -> str:
    """Hash a password for `API_PASSWORD_HASH`. Used by scripts/hash_password.py."""
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(
            f"bcrypt hashes at most {MAX_PASSWORD_BYTES} bytes and this is {len(raw)}. "
            "Use a shorter one rather than a truncated one."
        )
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time check of a password against a stored hash.

    Every failure path returns False rather than raising. A malformed hash in
    config, or a password longer than bcrypt accepts, is a failed login — not a
    500 that tells an attacker they found an edge the normal path does not have.
    """
    if not hashed:
        return False
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


#: Process-wide fallback signing key, minted once if none is configured. Module
#: state rather than a settings field because it must not appear in a settings
#: dump, and because regenerating it per call would invalidate every session on
#: every request.
_ephemeral_key: str | None = None


def signing_key(settings: Settings) -> str:
    """The key sessions are signed with.

    An unset `API_SECRET_KEY` mints a random one for the life of the process
    rather than refusing to start. Refusing would break `make up` on a clean
    checkout — which is a roadmap deliverable CI runs on every push — and the
    failure mode of an ephemeral key is mild and self-announcing: sessions do
    not survive a restart, so you log in again. It is *not* a way to run without
    authentication; the key is still secret and still required.
    """
    configured = settings.api_secret_key.get_secret_value()
    if configured:
        return configured

    global _ephemeral_key
    if _ephemeral_key is None:
        _ephemeral_key = secrets.token_urlsafe(48)
        log.warning(
            "auth.ephemeral_signing_key",
            msg="API_SECRET_KEY is unset — sessions will not survive a restart",
            fix="generate one with: openssl rand -hex 32",
        )
    return _ephemeral_key


def create_session_token(subject: str, settings: Settings, now: datetime) -> str:
    """Mint a session token for `subject`, expiring `api_session_hours` from now.

    `now` is passed in rather than read here, so this never touches the wall
    clock (CLAUDE.md §1.2) and a test can pin expiry without sleeping.
    """
    expires = now + timedelta(hours=settings.api_session_hours)
    claims = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(claims, signing_key(settings), algorithm=ALGORITHM)


def read_session_token(token: str, settings: Settings, now: datetime) -> str | None:
    """The subject of a valid, unexpired token, or None.

    Expiry is checked here against the injected `now` rather than left to
    `jose`, which would read the system clock inside the library — the one thing
    docs/BACKTESTING.md's "the clock" note says is hardest to notice going
    wrong. Everything else about the signature is still jose's job.

    None rather than an exception for every rejection, because the caller's
    response is identical whichever way a token is bad, and distinguishing them
    to the client is how an attacker learns which half they got right.
    """
    if not token:
        return None
    try:
        claims = jwt.decode(
            token,
            signing_key(settings),
            algorithms=[ALGORITHM],
            options={"verify_exp": False},
        )
    except JWTError:
        return None

    expires_at = claims.get("exp")
    if not isinstance(expires_at, int) or now.timestamp() >= expires_at:
        return None

    subject = claims.get("sub")
    return subject if isinstance(subject, str) and subject else None


def authenticate(username: str, password: str, settings: Settings) -> str | None:
    """Check a username and password, returning the subject to issue a token for.

    Fails closed when no hash is configured: a deployment that never set
    `API_PASSWORD_HASH` has no valid login at all, rather than an empty password
    that works. `main.lifespan` says so loudly at startup so this is discovered
    then rather than at the login screen.

    Both halves are compared even when the username is already wrong, so the
    response takes the same time either way and does not report which field was
    the mistake.
    """
    expected_hash = settings.api_password_hash.get_secret_value()
    user_ok = secrets.compare_digest(username, settings.api_user)
    password_ok = verify_password(password, expected_hash)
    return settings.api_user if (user_ok and password_ok) else None
