/**
 * Thin fetch wrapper. All server state goes through React Query — never call
 * these from a useEffect (CLAUDE.md §4).
 *
 * The host is not this module's business: `apiBase()` owns it, and defaults to
 * the page's own origin so the built bundle carries no hostname.
 */

import { apiBase } from './origin'

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(`${status}: ${detail}`)
  }
}

/**
 * Statuses that something *in front of* the API produces on its own when it
 * cannot reach it: 502, 503 and 504 from nginx in production, 500 from the Vite
 * dev server's proxy. Listed so the message can say what actually happened —
 * `502: Bad Gateway` on a dashboard is a sentence about a machine the reader
 * does not know they have.
 *
 * 503 is on the list and is also a status the API itself returns, which is not
 * a conflict: whether the body parsed decides which of the two this was, and
 * this set is consulted only after it did not.
 */
const PROXY_COULD_NOT_REACH_THE_API: ReadonlySet<number> = new Set([500, 502, 503, 504])

/**
 * What to say when the error body did not parse as JSON at all.
 *
 * Whether the body parsed is the discriminator, rather than the status code,
 * and the difference is load-bearing in both directions. The API's own 503 —
 * Redis unreadable, so the halt state is unknown — arrives as JSON and explains
 * itself in `detail`; replacing that with a guess about containers would be
 * strictly worse. And a 500 the API really did answer is still JSON, so it must
 * not be described as unreachable either. Only a body that failed to parse
 * means the API was never reached, because nginx and the Vite proxy both reply
 * in HTML or plain text.
 */
function unreachableDetail(res: Response): string {
  if (!PROXY_COULD_NOT_REACH_THE_API.has(res.status)) return res.statusText
  return (
    `${res.statusText || 'no response'} — the API could not be reached by the server ` +
    `that served this page. Check that it is running (\`docker compose ps\`), then ` +
    `open /readyz, which reports the API and each dependency separately.`
  )
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, {
    ...init,
    // The session is an HttpOnly cookie, so this is how it travels — there is
    // no token for the page to attach, deliberately (ADR 0008). `include`
    // rather than the `same-origin` default so a build pointed at an API on
    // another origin still authenticates; the API sets explicit CORS origins
    // and allow-credentials, which the spec requires for this to be honoured.
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!res.ok) {
    // `null` means the body did not parse as JSON, which is a different fact
    // from "parsed, but carried no detail" — see `unreachableDetail`.
    const body = await res.json().catch(() => null)
    const detail =
      typeof body?.detail === 'string'
        ? body.detail
        : body !== null
          ? res.statusText
          : unreachableDetail(res)
    throw new ApiError(res.status, detail)
  }
  return res.status === 204 ? (undefined as T) : res.json()
}

export const apiGet = <T>(path: string) => request<T>(path)
export const apiPost = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })
export const apiPatch = <T>(path: string, body: unknown) =>
  request<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
export const apiDelete = <T>(path: string) => request<T>(path, { method: 'DELETE' })
