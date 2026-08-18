# 8. One operator, a bcrypt hash, and a session cookie

**Status:** Accepted · 2026-08-18

## Context

`get_current_user()` had been a `NotImplementedError` stub since the skeleton.
Every endpoint under `/risk`, `/orders` and `/positions` was reachable by anyone
who could open a socket to the port, and `docs/SAFETY.md` said so plainly:
*bind to localhost only, never expose port 8000 publicly*. It is item one of
Phase 6 and the roadmap has called it blocking for any deployment throughout.

The immediate exposure was worse than "reads the book", which is how the
dashboard's own documentation had it. `POST /api/v1/risk/halt` took `actor` as a
**query parameter the caller filled in themselves**, as did `/risk/resume`,
`/risk/flatten-all`, `/orders`, `/positions/{symbol}/close` and
`/positions/{symbol}/stop`. Those handlers are stubs today, so nothing moved;
but the audit trail they were designed around was a form with a name box on it,
and that was going to be true on the day they stopped being stubs.

The skeleton had already made two choices worth honouring. `apps/api` declared
`python-jose[cryptography]` and `passlib[bcrypt]`, and `Settings` carried an
`api_secret_key` that `redacted()` already scrubbed — a test even names it
`"jwt-signing-key"`. The shape intended was a signed token and a hashed
password. What it had not decided was where users come from, or how the token
reaches the server.

## Decision

**One operator, from configuration.** `API_USER` and `API_PASSWORD_HASH` in
`.env`; no `users` table, no migration, no user CRUD. Nothing in the
requirements asks for a second person, and CLAUDE.md §7 says to prefer the
stub's shape over inventing a module.

This is **authentication**, and the phase item says "authentication and
authorisation". With one account there is nothing for authorisation to decide,
so none is built — rather than a role column with one value in it, which would
imply a permission model that does not exist. The item stays open until there is
a second principal to distinguish.

**A session cookie, not a bearer header — and the WebSocket decides it.** The
dashboard holds a socket, and a browser cannot set `Authorization` on a
WebSocket handshake. Bearer would therefore have meant the token in a query
string, which nginx writes to its access log in plain text on every reconnect,
or an abuse of `Sec-WebSocket-Protocol`. A cookie is sent on the handshake by
itself. `HttpOnly` also puts it beyond reach of any script on the page, which
`localStorage` cannot do, and the same-origin serving arrangement already
removed the cross-site awkwardness that normally argues against cookies. The
cookie still carries a JWT signed with `api_secret_key`, so `python-jose` is
still the right dependency.

**`actor` comes from the token.** Every handler that records who did something
takes `CurrentUser` instead of a parameter. This is most of the value of the
work: an audit trail the caller fills in is not one.

**`bcrypt` directly, and `passlib` dropped.** passlib 1.7.4 — released in 2020
and still the current version — detects its backend by reading
`bcrypt.__about__.__version__`, which bcrypt removed. On bcrypt 5 the backend
fails to load and every `hash()` call raises. The declared dependency did not
run. Pinning bcrypt below 4.1 to keep a 2020 wrapper alive is the worse trade on
a password hash, so the wrapper goes.

**Refusing is quiet; failing is loud.** Every rejected token is the same 401
with the same body, whether it was absent, expired, forged or malformed — the
client's response is identical, and telling them apart reports which half of the
problem an attacker has solved. Startup is the opposite: an unset
`API_PASSWORD_HASH` logs `CRITICAL` and no login can succeed.

## Consequences

**An unconfigured deployment has no way in, rather than a free one.** With no
hash configured every login is refused. That is the safe half of the failure and
it is announced at startup, not discovered at the login screen.

**An unset `API_SECRET_KEY` mints an ephemeral one and warns**, instead of
refusing to boot. Refusing would break `make up` on a clean checkout, which is a
roadmap deliverable CI runs on every push. Sessions then do not survive a
restart. This is not a way to run without authentication — the key is still
secret and still required — but it is a way to run without *durable* sessions,
and it says so in the log.

**Sessions cannot be revoked before they expire.** Nothing keeps a denylist, so
a token copied off the machine stays valid until `API_SESSION_HOURS` elapses.
That is the honest cost of stateless sessions, and the reason the default is 12
hours rather than weeks. A denylist wants somewhere to live, and the only
somewhere is the Redis whose unavailability is already a 503 on the dashboard
path — buying revocation with a new reason for the API to be down.

**There is still no rate limit on `/auth/login`.** It is its own Phase 6 item.
What stands in for one meanwhile is bcrypt: a cost-12 verification takes roughly
a quarter-second, which is a poor rate to guess at, and is why the work factor
is not tuned down for latency. It is a brake, not a lock.

**`/docs` stays open.** It discloses the shape of the API and nothing from the
book, CI's `stack` job gates on it to prove every router mounted, and closing it
would buy obscurity at the price of a check that has caught real breakage.

**Two existing test files needed the dependency overridden.** 53 tests in
`test_dashboard_api.py` and `test_marketdata_api.py` drive protected routes and
are about the book and the calendar, not about who is asking. They override
`get_current_user`; `tests/unit/test_api_contract.py` holds the enforcement
itself, from outside, against every route in the generated schema at once — so
neither the allow-list in `create_app` nor the test can drift alone.

## Alternatives

**A `users` table with migrations and CRUD.** Rejected for now, not forever. It
is the right answer the moment two people need distinguishing, and the token
layer is written so it can back `authenticate()` later without any router or any
part of the dashboard changing. Building it today would be machinery standing in
for a requirement nobody has.

**A bearer token in `localStorage`.** Rejected on the WebSocket argument above,
and on XSS: any script that runs on the page can read `localStorage` and cannot
read an `HttpOnly` cookie.

**HTTP Basic auth.** Rejected because the browser owns the credential dialog and
the session, which makes signing out unreliable and a run-mode banner on the
login screen impossible — and knowing whether a system is live *before* you sign
in is exactly what docs/DASHBOARD.md's loudest rule is about.

**A shared secret in a header, with no user at all.** Simpler, and it would have
left `actor` meaningless. The audit trail is the point.
