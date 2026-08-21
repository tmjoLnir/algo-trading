import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import RejectionsPanel from './RejectionsPanel'
import type { RejectionView, RejectionsResponse } from '@/api/types'

/**
 * The refused-decisions panel, from the side a person sees.
 *
 * **An empty table is the dangerous state here, not the reassuring one.** This
 * screen is read by somebody asking why a strategy is doing nothing, and "no
 * refusal is recorded" is one keystroke away from "nothing is being refused" —
 * which is a different and possibly false statement, because a stop exit the
 * risk chain denied is written to the worker's log and stored nowhere. Most of
 * what follows pins that the panel says so, including when it has nothing else
 * to say.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const BLIND_SPOTS = [
  'a stop exit refused by the risk chain is written to the log and nowhere else',
  '`no_action` outcomes are excluded on purpose',
]

function rejection(overrides: Partial<RejectionView> = {}): RejectionView {
  return {
    signal_id: 'sig-1',
    at: '2026-08-20T14:30:00Z',
    strategy_id: 'sma_crossover',
    symbol: 'SPY',
    action: 'enter_long',
    rule: 'max_position_size',
    reason: 'SPY would be 12% of equity',
    indicators: { sma_fast: '401.25' },
    ...overrides,
  }
}

function response(overrides: Partial<RejectionsResponse> = {}): RejectionsResponse {
  return {
    rejections: [rejection()],
    by_rule: { max_position_size: 1 },
    blind_spots: BLIND_SPOTS,
    ...overrides,
  }
}

function stub(route: { code: number; body: unknown }) {
  const fetchMock = vi.fn(
    async (_input: RequestInfo | URL, _init?: RequestInit) =>
      ({
        ok: route.code < 400,
        status: route.code,
        statusText: 'stub',
        json: async () => route.body,
      }) as Response,
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <RejectionsPanel />
    </QueryClientProvider>,
  )
}

describe('with refusals', () => {
  it('names the rule, the symbol and the reason', async () => {
    stub({ code: 200, body: response() })
    renderPanel()

    expect(await screen.findByText('SPY would be 12% of equity')).toBeTruthy()
    expect(screen.getAllByText('max_position_size').length).toBeGreaterThan(0)
    expect(screen.getByText('SPY')).toBeTruthy()
  })

  it('shows the indicators the strategy was looking at', async () => {
    // What makes a refusal diagnosable months later, when the bar series has
    // been restated and the indicator cannot be recomputed to what it was.
    stub({ code: 200, body: response() })
    renderPanel()

    expect(await screen.findByText(/sma_fast=/)).toBeTruthy()
    expect(screen.getByText('401.25')).toBeTruthy()
  })

  it('says the per-rule counts cover the rows shown, not all history', async () => {
    stub({ code: 200, body: response() })
    renderPanel()

    expect(await screen.findByText(/of the 1 shown/)).toBeTruthy()
  })
})

describe('with nothing refused', () => {
  const EMPTY = response({ rejections: [], by_rule: {} })

  it('does not say that nothing is being refused', async () => {
    // The distinction the whole panel turns on. "No refusal is recorded" is a
    // statement about this table; "nothing is being refused" is a statement
    // about the platform, and the second does not follow from the first.
    stub({ code: 200, body: EMPTY })
    renderPanel()

    const empty = await screen.findByText(/No refusal is recorded/)
    expect(empty.textContent).toMatch(/of the kind this table holds/)
  })

  it('still renders the blind spots', async () => {
    stub({ code: 200, body: EMPTY })
    renderPanel()

    expect(await screen.findByText(/stop exit refused/)).toBeTruthy()
  })
})

describe('the blind spots', () => {
  it('are shown beside the refusals as well as instead of them', async () => {
    stub({ code: 200, body: response() })
    renderPanel()

    expect(await screen.findByText(/What this table cannot show/)).toBeTruthy()
    expect(screen.getByText(/stop exit refused/)).toBeTruthy()
  })
})

describe('when the endpoint fails', () => {
  it('says it cannot tell, rather than showing an empty table', async () => {
    // An error rendered as "no refusals" would be the panel asserting the one
    // thing it has just failed to determine.
    stub({ code: 503, body: { detail: 'the database is unreachable' } })
    renderPanel()

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/not the same as nothing being blocked/)
    expect(alert.textContent).toMatch(/database is unreachable/)
    expect(screen.queryByText(/No refusal is recorded/)).toBeNull()
  })
})
