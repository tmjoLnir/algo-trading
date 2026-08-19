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
- **Still not ready for a public address.** No TLS termination of our own. The
  deployment *shape* is chosen (ADR 0011) and secrets are encrypted at rest
  (below), but **no host has been selected and nothing is provisioned**. The
  arrangement puts the platform on a private network rather than a public
  address — which is what the two gaps below assume. Keep the bind addresses private — `make check-bindings` refuses a
  wildcard or a public one, in both the development and the deployed
  configuration.
- **Behind `tailscale serve`, the session cookie is not marked `Secure`.** TLS
  terminates at Tailscale, which forwards plain HTTP to nginx; nginx sets
  `X-Forwarded-Proto` from its own `$scheme`, so `_is_https()` sees `http` and
  leaves the flag off even though the browser's connection is encrypted. The
  traffic is still encrypted over the tailnet; what is missing is the flag that
  stops a browser sending the cookie over plain HTTP. Fixing it means choosing
  which proxy's headers to trust (docs/DEPLOYMENT.md, "Known limitations").
- No encryption at rest for credentials beyond the host's own.

## Practices

- Secrets are encrypted at rest with **SOPS + age** and live in the repository
  as ciphertext (`infra/env/*.sops.env`, `scripts/manage_secrets.py`, ADR 0011). The
  private key never enters the repository. On a host they are decrypted to a
  `0600` `.env`; `.env` itself stays gitignored and is never the source of
  truth.
- **The run-mode locks are not secrets and may not be in a bundle** —
  `ATP_RUN_MODE`, `ATP_ALLOW_LIVE_TRADING`, `WORKER_ALLOW_LIVE_ORDERS`. A bundle
  is copied between hosts and restored from backups; none of that may switch on
  live trading. `scripts/manage_secrets.py` refuses them on import and again on
  install, so this is enforced rather than remembered.
- Losing the age private key makes every bundle encrypted to it unreadable.
  Back it up offline. It is the one item here with no recovery path.
- **Both alert transports are addressed by a credential.**
  `ALERT_NTFY_TOPIC` on a public ntfy server is the only thing in front of your
  halt notifications, in both directions: anyone holding it reads when trading
  stopped and can forge a message saying it resumed. `ALERT_TELEGRAM_TOKEN` is
  worse — it *is* the bot, it travels in the URL path, and whoever has it can
  read the chat and post as you. Both live in the bundle, never in a commit,
  and nothing logs either, including on the failure paths. Alerts carry no
  balances or positions for the same reason (ADR 0012).
- Paper and live use separate key pairs.
- `structlog` redacts known credential keys, but do not rely on it — never pass
  a secret to a log call.
- Rule sets arrive over HTTP and are untrusted: interpreted over a validated
  tree, never `eval()`d.
- Containers run as a non-root user.
- Gitleaks runs in CI on every push.
