import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiGet, apiPost } from './client'

/**
 * The API client is thin, but it is the single choke point every monetary
 * figure on the dashboard passes through. Three behaviours matter enough to
 * pin: errors must surface the server's `detail` (a risk rejection explains
 * itself there), money must arrive as strings so it never touches a JS float,
 * and an error that did *not* come from the API must say so rather than
 * repeating a proxy's wording at someone who is trying to fix their stack.
 */

function mockFetch(status: number, body: unknown, ok = status < 400) {
  const fn = vi.fn().mockResolvedValue({
    ok,
    status,
    statusText: 'mocked',
    json: async () => body,
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

/**
 * A response from something that is not the API — nginx's 502 page, or the Vite
 * proxy's plain-text 500. Distinguished by the body failing to parse as JSON,
 * which is exactly how the client tells the two apart.
 */
function mockProxyFailure(status: number, statusText: string) {
  const fn = vi.fn().mockResolvedValue({
    ok: false,
    status,
    statusText,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON at position 0')
    },
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('apiGet', () => {
  it('returns the parsed body on success', async () => {
    mockFetch(200, { equity: '100000.00' })
    await expect(apiGet<{ equity: string }>('/api/v1/x')).resolves.toEqual({
      equity: '100000.00',
    })
  })

  it('throws ApiError carrying the server detail', async () => {
    // A risk-rejected order explains itself in `detail`; swallowing it would
    // leave a blocked strategy looking identical to one with no signals.
    mockFetch(422, { detail: "risk rule 'max_position_size' blocked the order" })
    await expect(apiGet('/api/v1/orders')).rejects.toThrowError(ApiError)
    await expect(apiGet('/api/v1/orders')).rejects.toThrow(/max_position_size/)
  })

  it('falls back to statusText when the error body has no detail', async () => {
    mockFetch(500, {})
    await expect(apiGet('/api/v1/x')).rejects.toThrow(/mocked/)
  })

  it('explains a 502 instead of repeating "Bad Gateway" at the user', async () => {
    // What a dashboard showed for real: `Failed to load dashboard: Error: 502:
    // Bad Gateway`. That is nginx saying it could not reach the API, phrased as
    // a fact about a machine the reader does not know they have. The status is
    // still carried — it is what a bug report needs — but the text has to name
    // the actual condition and where to look next.
    mockProxyFailure(502, 'Bad Gateway')
    await expect(apiGet('/api/v1/dashboard/live')).rejects.toThrow(/could not be reached/)
    await expect(apiGet('/api/v1/dashboard/live')).rejects.toThrow(/readyz/)
  })

  it('explains the Vite dev proxy 500 the same way', async () => {
    // The dev server answers 500 where nginx answers 502 for the same fault —
    // an unreachable upstream — so keying the message off the status alone
    // would leave `make up` with the unhelpful version.
    mockProxyFailure(500, 'Internal Server Error')
    await expect(apiGet('/api/v1/dashboard/live')).rejects.toThrow(/could not be reached/)
  })

  it("keeps the API's own 503 detail rather than blaming the containers", async () => {
    // The regression guard for the message above. A 503 from the API itself —
    // Redis unreadable, so whether trading is halted is unknown — arrives as
    // JSON and already says the most important thing on the screen. Overwriting
    // it with advice about `docker compose ps` would replace a fact with a
    // guess, and this is the one error where that matters most.
    const detail = 'cannot read the halt state — refusing to report trading'
    mockFetch(503, { detail })

    const error = await apiGet('/api/v1/dashboard/live').catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(503)
    expect((error as ApiError).detail).toBe(detail)
    expect((error as ApiError).message).not.toMatch(/could not be reached/)
  })

  it('returns undefined for 204 rather than parsing an empty body', async () => {
    mockFetch(204, undefined)
    await expect(apiGet('/api/v1/x')).resolves.toBeUndefined()
  })

  it('preserves monetary values as strings, never numbers', async () => {
    // 0.1 + 0.2 !== 0.3 in binary floating point. If the client ever coerced
    // these to numbers, every P&L figure downstream would be subtly wrong.
    mockFetch(200, { unrealized_pnl: '0.30', qty: '0.1' })
    const body = await apiGet<Record<string, unknown>>('/api/v1/positions/AAPL')
    expect(typeof body.unrealized_pnl).toBe('string')
    expect(typeof body.qty).toBe('string')
  })
})

describe('apiPost', () => {
  it('sends a JSON body and the correct method', async () => {
    const fetchMock = mockFetch(200, { ok: true })
    await apiPost('/api/v1/risk/halt', { scope: 'global' })

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ scope: 'global' }))
  })

  it('defaults to an empty object body when none is given', async () => {
    const fetchMock = mockFetch(200, {})
    await apiPost('/api/v1/risk/halt')

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(init.body).toBe('{}')
  })
})
