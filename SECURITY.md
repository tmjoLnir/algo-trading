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

- **No authentication.** `get_current_user()` is a stub. Every endpoint under
  `/risk`, `/orders` and `/positions` can move money. Bind to localhost only
  until this is implemented — it is a blocking item for any deployment.
- No rate limiting on the API.
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
