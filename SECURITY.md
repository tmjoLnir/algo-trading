# Security

## Reporting

Do not open a public issue for a vulnerability. Email the maintainer directly.

Treat anything that could move money or expose a credential as security-
sensitive: an authentication bypass, an order path that skips risk checks, a
credential in a log, or a rule-set input that reaches an interpreter it should
not.

## If a broker key is exposed

In this order:

1. **Revoke it at the broker.** Immediately. A revoked key in a git history is
   harmless; a live key removed from git is not.
2. Generate a new pair, update `.env` / your secrets manager.
3. Check the account for unauthorised activity.
4. Only then clean up the repository history.

Rotation order matters. Cleaning git first leaves a live key exposed for as long
as the cleanup takes.

## Known gaps in the skeleton

- **The rate limiter trusts `X-Forwarded-For`.** It has to: behind nginx every
  request otherwise shares one bucket. Only safe because this stack always sits
  behind its own proxy — exposed directly, an attacker could rotate the header
  to sidestep the limit (ADR 0010).
- **Sessions cannot be revoked before they expire.** Stateless tokens with no
  denylist: a stolen cookie is good until `API_SESSION_HOURS` elapses.
- **No authorisation between people.** There is one account. Authorisation
  exists, but it is about the act — read-only sessions, and a password re-check
  on the two irreversible endpoints (ADR 0009). A second principal would need a
  users table, which scopes compose with rather than replace.
- **Still not ready for a public address.** No TLS termination of our own, no
  secrets manager, no chosen deployment target. Keep the bind addresses private
  — `make check-bindings` refuses a wildcard or a public one.
- No encryption at rest for credentials beyond the host's own.

## Practices

- Secrets in `.env` (gitignored) or a secrets manager. Never in code.
- Paper and live use separate key pairs.
- `structlog` redacts known credential keys, but do not rely on it — never pass
  a secret to a log call.
- Rule sets arrive over HTTP and are untrusted: interpreted over a validated
  tree, never `eval()`d.
- Containers run as a non-root user.
- Gitleaks runs in CI on every push.
