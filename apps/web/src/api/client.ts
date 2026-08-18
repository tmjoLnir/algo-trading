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
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    throw new ApiError(res.status, body.detail ?? res.statusText)
  }
  return res.status === 204 ? (undefined as T) : res.json()
}

export const apiGet = <T>(path: string) => request<T>(path)
export const apiPost = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })
export const apiPatch = <T>(path: string, body: unknown) =>
  request<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
export const apiDelete = <T>(path: string) => request<T>(path, { method: 'DELETE' })
