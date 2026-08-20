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

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

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

#: The shape of a bcrypt hash: `$2<variant>$<cost>$<22-char salt><31-char digest>`,
#: the last 53 characters drawn from bcrypt's own base64 alphabet.
_BCRYPT_HASH = re.compile(r"^\$2[abxy]\$\d{2}\$[./A-Za-z0-9]{53}$")


def looks_like_bcrypt_hash(hashed: str) -> bool:
    """Whether `hashed` is structurally a bcrypt hash — not whether it verifies.

    Exists for one failure that is otherwise silent. `API_PASSWORD_HASH` reaches
    this process through `.env`, and Docker Compose interpolates `$NAME` in that
    file: a hash pasted unquoted has its `$` and the salt's leading letters
    eaten as an unset variable, arriving here shortened. `verify_password`
    refuses it correctly — every login fails — and the startup check for an
    *unset* hash stays quiet, because a truncated hash is not an empty one.

    Structure only, deliberately. Anything stronger would have to hash to find
    out, and this runs at startup on a value that is allowed to be absent.

    See `scripts/hash_password.py`, which prints the line single-quoted so this
    never fires, and `.env.example` for the operator-facing version.
    """
    return bool(_BCRYPT_HASH.match(hashed))


class Scope(StrEnum):
    """What a session is allowed to do — not who its holder is.

    Authorisation here is about the act, which is the shape docs/RISK.md already
    argues for: "engaging needs no confirmation — hesitation is the expensive
    part. Clearing requires a named human." That is a statement about two
    actions, not about two kinds of person, and this platform has exactly one
    person (ADR 0008). Roles would have been a column with one value in it.

    READ is the interesting one. It exists for a real situation rather than a
    hypothetical org chart: looking at the book from a phone on the LAN, where
    you want to see what you hold without carrying the ability to liquidate it
    if the device is lost or the tab is left open in a café.
    """

    READ = "read"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class Session:
    """A verified session: who, and what they may do."""

    user: str
    scope: Scope

    @property
    def may_act(self) -> bool:
        return self.scope is Scope.FULL


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


def create_session_token(
    subject: str,
    settings: Settings,
    now: datetime,
    scope: Scope = Scope.FULL,
) -> str:
    """Mint a session token for `subject`, expiring `api_session_hours` from now.

    The scope goes in the *signed* payload rather than anywhere the client can
    reach. A scope the browser could edit would be a suggestion, and the point
    of a read-only session is that its holder cannot decide to stop being one.

    `now` is passed in rather than read here, so this never touches the wall
    clock (CLAUDE.md §1.2) and a test can pin expiry without sleeping.
    """
    expires = now + timedelta(hours=settings.api_session_hours)
    claims = {
        "sub": subject,
        "scp": scope.value,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(claims, signing_key(settings), algorithm=ALGORITHM)


def read_session_token(token: str, settings: Settings, now: datetime) -> Session | None:
    """The session a valid, unexpired token represents, or None.

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
    if not isinstance(subject, str) or not subject:
        return None

    # An unrecognised or absent scope resolves to READ, not FULL. A token minted
    # before scopes existed, or one whose claim is a value this version does not
    # know, is downgraded rather than trusted — the failure of a stale session is
    # then "that button is disabled" rather than "it did the irreversible thing".
    try:
        scope = Scope(claims.get("scp", ""))
    except ValueError:
        scope = Scope.READ

    return Session(user=subject, scope=scope)


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
