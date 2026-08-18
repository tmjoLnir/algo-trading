# 9. Authorisation is about the act, not about who is asking

**Status:** Accepted · 2026-08-18

## Context

ADR 0008 built authentication and deliberately stopped there. The roadmap item
it half-satisfied said "authentication **and** authorisation", and the reason
given for splitting it was honest and still holds: this platform has one
operator, so there is no second principal to distinguish, and a `role` column
with one value in it would imply a permission model that does not exist.

That argument rules out RBAC. It does not rule out authorisation, because the
domain already asks for some — in prose, unenforced. `docs/RISK.md`:

> Halts everything. Engaging needs no confirmation — hesitation is the expensive
> part. **Clearing requires a named human** and is audit-logged.

That is an authorisation rule, and it is about *two acts* rather than two kinds
of person. The same shape appears throughout: `/halt` takes no confirmation
while `flatten-all` demands a literal phrase; live trading needs two independent
flags and an unattended worker needs a third. Authority here has always been
modelled as what may be done, under what proof — never as who someone is.

There is also a real situation, not a hypothetical org chart, that a single
operator meets: looking at the book from a phone on the LAN — the case
`ATP_WEB_BIND_ADDR` exists for. You want to see what you hold. You do not want
that device, or a cookie copied off it, to be able to liquidate the account.

## Decision

Two mechanisms, both about acts.

**Session scopes.** A session is `full` or `read`, chosen at sign-in and carried
in the *signed* token. A read-only session may call every safe method and is
refused every mutating one with **403**, with a single exception below. The
scope is requested at login and cannot be changed afterwards: a session that can
promote itself is not read-only, it is a full session with a preference.

**`/risk/halt` is that exception, and it is a rule rather than a concession.**
docs/DASHBOARD.md keeps the kill switch always visible and never behind a menu;
docs/RISK.md says engaging it needs no confirmation because hesitation is the
expensive part; `ws.py` broadcasts halts to clients that subscribed to nothing,
because a trading halt is not something to opt into. Someone watching the book
from a phone is precisely who most needs to be able to stop it and least needs
to place an order. Clearing a halt is *not* on the list — that is the asymmetry
docs/RISK.md asks for, finally enforced rather than described.

**Step-up on the two irreversible acts.** `/risk/resume` and
`/risk/flatten-all` require the account password in the request body, checked
against the same bcrypt hash. A cookie proves someone signed in within the last
twelve hours; it does not prove anyone is at the keyboard now.

Enforcement lives in one dependency that decides from the request's method and
path, not route by route — a rule applied per handler is a rule someone adds a
handler without. A new mutating route is refused to read-only sessions by
default, and admitting one means adding it to a named list with a reason.

## Consequences

**403 and not 401, and the distinction is load-bearing.** A refused write is not
a credential problem: the session is valid and re-presenting it changes nothing.
Answering 401 would send the dashboard to a login screen that cannot help. The
front end drops the session on 401 only, and a browser test drives four
consecutive 403s and asserts the operator is still signed in afterwards.

**No elevation window.** Step-up could have minted a "recently authenticated"
period, which is the conventional UX. It would also be a stretch of minutes
during which a walked-away laptop can flatten the book — the exact situation
this exists to prevent. The proof travels with the act instead.

**The password moves to the request body.** It must never be a query parameter:
nginx writes query strings to its access log verbatim. A contract test asserts
neither endpoint takes it in the URL.

**An unknown or absent scope resolves to `read`.** A token minted before scopes
existed, or carrying a value this version does not know, is downgraded rather
than trusted. Guessing wrong this way costs a disabled button; guessing wrong
the other way costs an irreversible action by a session never granted it.

**Read-only barely changes the dashboard today**, and that is worth stating
plainly rather than overselling. The only acting control currently on the screen
is the kill switch, which read-only sessions may deliberately still use. What a
read-only session changes is what the *API* permits — so a stolen cookie cannot
trade, whatever the page it came from would have offered. The badge in the nav
exists because without it the difference would be invisible until something was
refused.

**Still no authorisation *between people*.** There is one account. If a second
principal ever appears, scopes are the wrong tool for telling them apart and a
users table (ADR 0008's rejected alternative) becomes the right one. Scopes
would compose with it rather than block it: `authenticate()` would return a
different subject, and the scope machinery would not change.

## Alternatives

**A `role` column with one value.** The conventional reading of "authorisation",
and the thing ADR 0008 argued against. It would have described a permission
model rather than enforced one.

**Refusing writes to read-only sessions with 401.** Simpler on the client, since
one handler covers everything. Wrong: it conflates "you are not signed in" with
"you are, and may not do that", and it makes the fix for the second one a login
that cannot fix it.

**Scope as a toggle inside the app rather than a choice at sign-in.** Friendlier,
and it defeats the mechanism entirely — the holder of a compromised session
could turn the restriction off.

**Step-up for every write, not just the irreversible two.** Safer in the
abstract, and it would put a password prompt in front of routine order entry
until people stopped reading it. The two acts gated here are the ones that
cannot be undone.
