import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import RiskLimitsPanel from './RiskLimitsPanel'
import type { LimitUsageView, RiskStatusView } from '@/api/types'

/**
 * The risk limits panel, from the side a person sees.
 *
 * **The panel has three states and they must not look alike**, which is what
 * almost every case below is about:
 *
 * 1. a book was published, so the readings are real;
 * 2. no book was published, so the ceilings show and every reading is `—`;
 * 3. `/risk/status` failed outright, so the ceilings come from `/risk/limits`
 *    instead and the panel says why the readings are missing.
 *
 * States 2 and 3 are the ones worth testing hard. The server deliberately sends
 * nulls rather than zeroes for an unknown book (ADR 0007), and a screen that
 * rendered a null as an empty bar would put that safety property back exactly
 * where it started — an empty bar and a bar at zero are the same picture, and
 * one of them means "nobody knows what the book holds".
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function usage(overrides: Partial<LimitUsageView> = {}): LimitUsageView {
  return {
    rule: 'max_gross_exposure',
    unit: 'fraction_of_equity',
    ceiling: '1.00',
    current: '0.1089',
    utilisation: '0.1089',
    at_limit: false,
    observable: true,
    note: null,
    ...overrides,
  }
}

function status(overrides: Partial<RiskStatusView> = {}): RiskStatusView {
  return {
    as_of: '2026-08-20T14:30:00Z',
    book_as_of: '2026-08-20T14:30:00Z',
    book_age_seconds: 0,
    book_published: true,
    equity: '10100',
    limits: [usage()],
    unmarked_symbols: [],
    ...overrides,
  }
}

const LIMITS = {
  max_position_pct: '0.10',
  max_gross_exposure_pct: '1.00',
  max_daily_loss_pct: '0.03',
  max_orders_per_minute: 30,
  max_open_positions: 20,
  max_quote_age_seconds: 30,
  default_stop_loss_pct: '0.02',
  default_take_profit_pct: '0.06',
}

/** Route `/risk/status` and `/risk/limits` independently — the fallback needs it. */
function stub(routes: { status?: { code: number; body: unknown }; limits?: unknown }) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/risk/limits')) {
      return {
        ok: true,
        status: 200,
        statusText: 'stub',
        json: async () => routes.limits ?? LIMITS,
      } as Response
    }
    const route = routes.status ?? { code: 200, body: status() }
    return {
      ok: route.code < 400,
      status: route.code,
      statusText: 'stub',
      json: async () => route.body,
    } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <RiskLimitsPanel />
    </QueryClientProvider>,
  )
}

describe('with a published book', () => {
  it('renders the reading against its ceiling', async () => {
    stub({})
    renderPanel()

    expect(await screen.findByText('10.89%')).toBeTruthy()
    expect(screen.getByText('100.00%')).toBeTruthy()
  })

  it('names the rule that would refuse', async () => {
    // A refusal on the Orders tab reads "refused by max_gross_exposure". A
    // reader has to be able to get from that string to the row that should
    // have predicted it.
    stub({})
    renderPanel()

    expect(await screen.findByText('max_gross_exposure')).toBeTruthy()
  })

  it('says "at limit" in words, not only in colour', async () => {
    // docs/DASHBOARD.md makes this a rule: colour alone is not a signal a
    // colour-blind reader can use.
    stub({ status: { code: 200, body: status({ limits: [usage({ at_limit: true })] }) } })
    renderPanel()

    expect(await screen.findByText('at limit')).toBeTruthy()
  })

  it('warns that unmarked positions understate every exposure figure', async () => {
    // The direction that makes a breached limit look compliant.
    stub({ status: { code: 200, body: status({ unmarked_symbols: ['MSFT'] }) } })
    renderPanel()

    expect(await screen.findByText(/understates/)).toBeTruthy()
    expect(screen.getByText(/MSFT/)).toBeTruthy()
  })

  it('shows a signed day P&L, so a good day is not a loss', async () => {
    stub({
      status: {
        code: 200,
        body: status({
          limits: [
            usage({
              rule: 'daily_loss_limit',
              ceiling: '0.03',
              current: '0.0500',
              utilisation: '0.0000',
            }),
          ],
        }),
      },
    })
    renderPanel()

    expect(await screen.findByText('+5.00%')).toBeTruthy()
  })
})

describe('with no published book', () => {
  const NO_BOOK = status({
    book_published: false,
    book_as_of: null,
    book_age_seconds: null,
    equity: null,
    limits: [usage({ current: null, utilisation: null, at_limit: null })],
  })

  it('still shows the ceilings', async () => {
    // Dropping the rows would read as the limits themselves having gone away.
    stub({ status: { code: 200, body: NO_BOOK } })
    renderPanel()

    expect(await screen.findByText('100.00%')).toBeTruthy()
  })

  it('renders no reading rather than zero', async () => {
    stub({ status: { code: 200, body: NO_BOOK } })
    renderPanel()

    expect(await screen.findByText('no reading')).toBeTruthy()
    expect(screen.queryByText('0.00%')).toBeNull()
  })

  it('draws no bar at all, because an empty bar reads as none used', async () => {
    stub({ status: { code: 200, body: NO_BOOK } })
    renderPanel()

    await screen.findByText('no reading')
    expect(screen.queryByRole('presentation')).toBeNull()
  })

  it('says an unpublished book is not a compliant one', async () => {
    stub({ status: { code: 200, body: NO_BOOK } })
    renderPanel()

    const note = await screen.findByText(/No worker has published a book/)
    expect(note.textContent).toMatch(/not/)
  })
})

describe('when the status endpoint fails', () => {
  const FAILED = {
    status: { code: 503, body: { detail: 'cannot read the published book: redis is down' } },
  }

  it('falls back to the ceilings from /risk/limits', async () => {
    // The whole reason `/risk/limits` is its own route: it reads config and
    // touches no store, so it answers during exactly the incident that took
    // `/status` down.
    stub(FAILED)
    renderPanel()

    expect(await screen.findByText('100.00%')).toBeTruthy()
    expect(screen.getByText('Open positions')).toBeTruthy()
  })

  it('says the readings are unknown rather than zero', async () => {
    stub(FAILED)
    renderPanel()

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/not zero/)
    expect(alert.textContent).toMatch(/redis is down/)
  })

  it('does not ask for the ceilings while the primary is answering', async () => {
    // One request in the ordinary case. The fallback is a failure path, not a
    // second poll on every page load.
    const fetchMock = stub({})
    renderPanel()

    await screen.findByText('10.89%')
    await waitFor(() =>
      expect(fetchMock.mock.calls.every((call) => !String(call[0]).includes('/risk/limits'))).toBe(
        true,
      ),
    )
  })
})

describe('a limit that cannot be observed', () => {
  it('is distinguished from one that merely has no reading today', async () => {
    // `rate_limit` is unknown *always* — the rule's window lives in the
    // worker's process and counts refused attempts, which are recorded as
    // signals rather than orders. The other rows are unknown right now.
    stub({
      status: {
        code: 200,
        body: status({
          limits: [
            usage({
              rule: 'rate_limit',
              unit: 'orders_per_minute',
              ceiling: '30',
              current: null,
              utilisation: null,
              at_limit: null,
              observable: false,
              note: 'recorded as signals rather than orders',
            }),
          ],
        }),
      },
    })
    renderPanel()

    expect(await screen.findByText('not observable')).toBeTruthy()
    expect(screen.getByText(/recorded as signals/)).toBeTruthy()
  })
})
