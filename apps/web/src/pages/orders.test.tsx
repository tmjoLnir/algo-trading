import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import Orders from './Orders'
import OrderHistoryTable from '@/components/OrderHistoryTable'
import type { OrderHistoryView, OrdersResponse } from '@/api/types'

/**
 * What the order screen states, and what it refuses to.
 *
 * This screen exists for the rows no other read in the platform contains: an
 * order that was refused moved no quantity, so it is in no round trip, in no
 * book, and on no equity curve. The assertions that matter are therefore about
 * refusals and about partial fills — the two places where a status word alone
 * misleads.
 *
 * 1. **A refusal carries its reason**, and a refusal whose reason was never
 *    recorded says so rather than rendering the dash that means "nothing
 *    refused this". Two different facts must not collapse into one glyph.
 * 2. **A partial fill is a proportion.** `cancelled` covers an order that never
 *    traded and one that filled 90% before the cancel landed, and those are
 *    different positions.
 * 3. **The two refusals read differently.** Our risk engine declining to send an
 *    order and the venue declining one we sent call for opposite responses.
 * 4. **A full page says it is full**, because a list that stops at the limit
 *    looks identical to one that ended.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const ORDER: OrderHistoryView = {
  id: 'ord-1',
  client_order_id: 'atp-abc123',
  broker_order_id: 'brk-9',
  symbol: 'AAPL',
  side: 'buy',
  order_type: 'market',
  time_in_force: 'day',
  qty: '100',
  filled_qty: '100',
  limit_price: null,
  stop_price: null,
  avg_fill_price: '218.4200',
  status: 'filled',
  purpose: 'entry',
  reject_reason: null,
  strategy_id: 'sma_cross',
  signal_id: 'sig-1',
  created_at: '2026-08-14T13:35:00Z',
  submitted_at: '2026-08-14T13:35:01Z',
  filled_at: '2026-08-14T13:35:02Z',
}

function response(orders: OrderHistoryView[], limitReached = false): OrdersResponse {
  return { orders, limit_reached: limitReached, run_mode: 'paper' }
}

describe('a refused order', () => {
  it('shows the reason it was refused', () => {
    // The row this screen exists for. A rejection with no reason on screen
    // tells the reader something went wrong and not what.
    render(
      <OrderHistoryTable
        orders={[
          {
            ...ORDER,
            status: 'rejected_risk',
            filled_qty: '0',
            avg_fill_price: null,
            reject_reason: 'MaxPositionSize: 500 shares exceeds the 100 limit',
          },
        ]}
      />,
    )

    expect(screen.getByText('MaxPositionSize: 500 shares exceeds the 100 limit')).toBeTruthy()
  })

  it('says so when no reason was recorded, rather than showing a dash', () => {
    // A dash means "nothing refused this order". Using it for "something
    // refused this and we did not record why" states the opposite.
    render(
      <OrderHistoryTable
        orders={[{ ...ORDER, status: 'rejected', filled_qty: '0', reject_reason: null }]}
      />,
    )

    expect(screen.getByText(/refused, but no reason was recorded/)).toBeTruthy()
  })

  it('distinguishes our own risk engine from the venue', () => {
    // Opposite responses: the first is a limit doing its job, the second is a
    // problem with the account or the instrument.
    render(
      <OrderHistoryTable
        orders={[
          { ...ORDER, id: 'a', status: 'rejected_risk', reject_reason: 'over the limit' },
          { ...ORDER, id: 'b', status: 'rejected', reject_reason: 'insufficient buying power' },
        ]}
      />,
    )

    expect(screen.getByText('refused by risk')).toBeTruthy()
    expect(screen.getByText('refused by venue')).toBeTruthy()
  })

  it('leaves the reason cell a dash when nothing refused the order', () => {
    render(<OrderHistoryTable orders={[ORDER]} />)
    expect(screen.getByText('—')).toBeTruthy()
  })
})

describe('a partial fill', () => {
  it('shows what filled against what was asked for', () => {
    // The status word does not answer this: a cancelled order that filled 90%
    // first is a position, and a cancelled order that filled none is not.
    render(
      <OrderHistoryTable
        orders={[{ ...ORDER, status: 'cancelled', qty: '500', filled_qty: '450' }]}
      />,
    )

    const row = screen.getByText('cancelled').closest('tr')
    expect(row).toBeTruthy()
    expect(within(row as HTMLElement).getByText(/450/)).toBeTruthy()
    expect(within(row as HTMLElement).getByText(/500/)).toBeTruthy()
  })
})

describe('prices', () => {
  it('renders money from the string it was sent, without rounding it', () => {
    // Truncated, not rounded — the display is a view of an exact value held
    // elsewhere (docs/DASHBOARD.md).
    render(<OrderHistoryTable orders={[{ ...ORDER, avg_fill_price: '218.4289' }]} />)
    expect(screen.getByText('218.42')).toBeTruthy()
    expect(screen.queryByText('218.43')).toBeNull()
  })

  it('says a market order traded at market rather than showing a missing price', () => {
    render(<OrderHistoryTable orders={[{ ...ORDER, limit_price: null, stop_price: null }]} />)
    expect(screen.getByText('at market')).toBeTruthy()
  })

  it('shows a limit price when the order named one', () => {
    render(
      <OrderHistoryTable
        orders={[{ ...ORDER, order_type: 'limit', limit_price: '215.00', stop_price: null }]}
      />,
    )
    expect(screen.getByText('215.00')).toBeTruthy()
  })
})

describe('a purpose that was never recorded', () => {
  it('says so rather than guessing a bucket', () => {
    // Labelling a historical exit an "entry" is worse than admitting the record
    // does not say (docs/ANALYTICS.md).
    render(<OrderHistoryTable orders={[{ ...ORDER, purpose: null }]} />)
    expect(screen.getByText(/purpose not recorded/)).toBeTruthy()
  })
})

describe('an empty result', () => {
  it('says nothing was placed, not that nothing filled', () => {
    render(<OrderHistoryTable orders={[]} />)
    expect(screen.getByText(/No orders match these filters/)).toBeTruthy()
    expect(screen.getByText(/refusals included/)).toBeTruthy()
  })
})

function stub(status: number, body: unknown) {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
    return {
      ok: status < 400,
      status,
      statusText: 'stub',
      json: async () => body,
    } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Orders />
    </QueryClientProvider>,
  )
}

describe('the page', () => {
  it('names the run mode the orders belong to', async () => {
    // Paper and live share a table; a screen that did not say which would be
    // unreadable on a machine that has run both.
    stub(200, response([ORDER]))
    renderPage()

    expect(await screen.findByText('AAPL')).toBeTruthy()
    expect(screen.getByText('paper')).toBeTruthy()
  })

  it('says when the page came back full', async () => {
    stub(200, response([ORDER], true))
    renderPage()

    expect(await screen.findByText(/There are older orders than these/)).toBeTruthy()
  })

  it('does not claim there are more when the list ended', async () => {
    stub(200, response([ORDER], false))
    renderPage()

    await screen.findByText('AAPL')
    expect(screen.queryByText(/There are older orders than these/)).toBeNull()
  })

  it('shows what the server said when a status is refused', async () => {
    // The endpoint answers an unknown status with a 422 naming the real ones,
    // so that "you asked for something that does not exist" cannot read as
    // "there are none of those".
    stub(422, { detail: "unknown order status 'nonsense'; known statuses are filled, rejected" })
    renderPage()

    expect(await screen.findByText(/unknown order status 'nonsense'/)).toBeTruthy()
  })

  it('asks for the newest page by default, with no filters applied', async () => {
    const fetchMock = stub(200, response([ORDER]))
    renderPage()
    await screen.findByText('AAPL')

    const url = String(fetchMock.mock.calls[0]?.[0])
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('limit')).toBe('100')
    expect(params.get('status')).toBeNull()
    expect(params.get('symbol')).toBeNull()
  })
})
