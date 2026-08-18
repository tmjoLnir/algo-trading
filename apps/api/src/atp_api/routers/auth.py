"""Login, logout, and "who am I" — the only unauthenticated routes that matter.

Deliberately thin, like every router here: check a password, mint a token, set a
cookie. The decisions behind it are in `atp_api.auth` and ADR 0008.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from atp_api.auth import COOKIE_NAME, Scope, authenticate, create_session_token
from atp_api.deps import CurrentSession, get_clock
from atp_core.clock import Clock
from atp_core.config import Settings, get_settings
from atp_core.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    #: Not `SecretStr`. Pydantic would render it as `**********` in a validation
    #: error, which is right for a *response* model and wrong here — this value
    #: is checked and discarded, and the type would only make the handler
    #: unwrap it.
    password: str = Field(min_length=1, max_length=1024)
    #: Ask for a session that can look but not act (ADR 0009). Requested at
    #: sign-in rather than toggled later, because a session that can promote
    #: itself is not a read-only session — it is a full one with a preference.
    read_only: bool = False


class WhoAmI(BaseModel):
    user: str
    #: "read" or "full". The dashboard reads this to decide what to disable; the
    #: server does not trust that decision, it re-checks on every request.
    scope: str


class PreSessionContext(BaseModel):
    """What the login screen is allowed to know before anyone has signed in."""

    run_mode: str


def _is_https(request: Request) -> bool:
    """Whether the browser's connection is TLS, not this process's.

    nginx terminates TLS and forwards plain HTTP, so `request.url.scheme` is
    `http` on every proxied request however the page was loaded. Marking the
    cookie `Secure` off that would leave it unmarked behind TLS — and marking it
    unconditionally would make the browser drop it on plain-HTTP localhost,
    which is how everyone runs this today. `X-Forwarded-Proto` is what nginx
    sets (infra/docker/web.nginx.conf) and is the only thing that knows.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    return (forwarded.split(",")[0].strip() or request.url.scheme) == "https"


@router.post("/login", response_model=WhoAmI)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> WhoAmI:
    """Exchange a username and password for a session cookie.

    There is no rate limit here yet — that is its own Phase 6 item. What stands
    in for one meanwhile is bcrypt itself: a cost-12 hash takes roughly a
    quarter-second to verify, which is a poor rate for guessing and is why the
    work factor is not tuned down. It is a brake, not a lock, and the item above
    it in the roadmap is the lock.

    The failure is one 401 with one message. "No such user" and "wrong password"
    are the same answer here, because telling them apart confirms which usernames
    exist.
    """
    subject = authenticate(payload.username, payload.password, settings)
    if subject is None:
        log.warning("auth.login_failed", username=payload.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",
        )

    scope = Scope.READ if payload.read_only else Scope.FULL
    token = create_session_token(subject, settings, clock.now(), scope)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.api_session_hours * 3600,
        httponly=True,
        samesite="strict",
        secure=_is_https(request),
        path="/",
    )
    log.info("auth.login", user=subject, scope=scope.value)
    return WhoAmI(user=subject, scope=scope.value)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> None:
    """Clear the session cookie.

    Unauthenticated on purpose: logging out with an already-expired session must
    work, and it is the one action whose whole effect is to remove authority.

    The token itself is not revoked — nothing here keeps a denylist, so a token
    already copied off the machine stays valid until it expires. That is the
    honest cost of stateless sessions and the reason `api_session_hours` is
    hours rather than weeks.
    """
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=_is_https(request),
        path="/",
    )


@router.get("/me", response_model=WhoAmI)
async def me(session: CurrentSession) -> WhoAmI:
    """Who the caller is, or 401.

    The dashboard's first call on load: a 200 renders the app, a 401 renders the
    login screen. That keeps "am I logged in" a question the server answers,
    rather than something the client infers from a cookie it cannot read —
    `HttpOnly` means the page genuinely cannot see it.
    """
    return WhoAmI(user=session.user, scope=session.scope.value)


@router.get("/context", response_model=PreSessionContext)
async def context(settings: Annotated[Settings, Depends(get_settings)]) -> PreSessionContext:
    """The run mode, before there is a session.

    docs/DASHBOARD.md calls the run-mode banner the most important pixel on the
    screen, and the moment before signing in is not an exception — it is when
    the operator is still deciding whether to open a live-money system. So the
    login screen needs this one fact, and needs it unauthenticated.

    It cannot come from the root `/` handler, which returns the same thing: in
    the deployed arrangement nginx serves the dashboard at `/`, so a request
    there gets `index.html` and never reaches the API at all. That is not a
    proxy misconfiguration — `/` is where the SPA belongs — it just means
    anything the browser needs must live under `/api`. This was found by
    signing in through the real proxy, having passed a unit test whose stub
    matched the path too loosely.

    Deliberately nothing else. Run mode discloses whether real money is at
    stake, which is a warning rather than a secret; the book, the account and
    the halts all stay behind the session.
    """
    return PreSessionContext(run_mode=settings.run_mode.value)
