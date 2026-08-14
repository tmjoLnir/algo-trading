import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiGet, apiPost } from './client'

/**
 * The API client is thin, but it is the single choke point every monetary
 * figure on the dashboard passes through. Two behaviours matter enough to pin:
 * errors must surface the server's `detail` (a risk rejection explains itself
 * there), and money must arrive as strings so it never touches a JS float.
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
