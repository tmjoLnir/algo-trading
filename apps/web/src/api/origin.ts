/**
 * Where the server is.
 *
 * Both values are deliberately *relative by default*, because Vite inlines
 * `import.meta.env.VITE_*` into the bundle at build time. A bundle built with
 * `VITE_API_BASE_URL=http://localhost:8000` carries that string forever — it
 * cannot be re-pointed by setting an environment variable on the container
 * serving it, which is exactly the shape of mistake that looks fine in `npm
 * run dev` and breaks for every user of a deployed build.
 *
 * Defaulting to the page's own origin removes the hostname from the bundle
 * altogether: the same artefact is correct on localhost, on a LAN address and
 * behind a hostname, provided something in front routes /api and /ws to the
 * API. In this repo that is `infra/docker/web.nginx.conf` in production and
 * the dev-server proxy in `vite.config.ts`, which exist to make the two
 * environments resolve identically rather than merely both work.
 *
 * The overrides remain for the case they are actually for — an API on a
 * genuinely different origin — which also means configuring `API_CORS_ORIGINS`
 * on the API to admit that origin.
 */

/** Minimal shape of `window.location` this module reads — so a test can pass a literal. */
export type OriginLocation = Pick<Location, 'protocol' | 'host'>

/**
 * Prefix for API paths. Empty string means "same origin as this page", which
 * is what makes `fetch('/api/v1/dashboard/live')` resolve against whatever host
 * served the dashboard.
 */
export function apiBase(override = import.meta.env.VITE_API_BASE_URL): string {
  return override ?? ''
}

/**
 * URL for the dashboard WebSocket.
 *
 * The scheme is derived from the page's, not hardcoded: a page served over
 * HTTPS may not open a `ws://` socket — browsers block it as mixed content —
 * so a hardcoded `ws://` is a dashboard that silently loses live updates the
 * day it is put behind TLS, while the 5-minute poll keeps working and hides it.
 */
export function wsUrl(
  loc: OriginLocation = window.location,
  override = import.meta.env.VITE_WS_URL,
): string {
  if (override) return override
  const scheme = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${loc.host}/ws`
}
