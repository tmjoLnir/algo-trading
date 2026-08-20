import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import AccountSummary from './AccountSummary'
import OrdersTable from './OrdersTable'
import PositionsTable from './PositionsTable'
import SignalFeed from './SignalFeed'
import FeedStatus from './FeedStatus'
import type { AccountView, OrderView, PositionView, SignalView } from '@/api/types'

/**
 * What the dashboard states, and what it refuses to.
 *
 * These are rendered rather than unit-tested through props, because every rule
 * under test is a property of what a person sees. The three that matter:
 *
 * 1. **A figure we do not know renders as `—`, never as `0`.** The API sends
 *    null on purpose for an unmarked position or an account with no session
 *    anchor; turning it into a zero on the way to the screen would undo the
 *    entire reason it is nullable.
 * 2. **Colour is not the only signal.** Every gain or loss carries a sign or an
 *    arrow, because a red-green screen is unreadable to a good fraction of
 *    people (docs/DASHBOARD.md).
 * 3. **Every signal shows its reason, refused ones included.** A strategy
 *    blocked by a risk rule on every bar looks, from anywhere else in the
 *    system, exactly like a strategy that had no ideas.
 */

afterEach(cleanup)

const ACCOUNT: AccountView = {
  equity: '101234.5678',
  cash: '90000.00',
  gross_exposure: '11234.56',
  net_exposure: '11234.56',
  leverage: '0.1110',
  realized_pnl: '250.00',
  unrealized_pnl: '134.56',
  day_pnl: '-450.25',
  day_pnl_pct: '-0.0044',
  open_position_count: 2,
  unmarked_symbols: [],
}

const POSITION: PositionView = {
  symbol: 'AAPL',
  qty: '10',
  avg_entry_price: '100.00',
  last_price: '110.00',
  market_value: '1100.00',
  unrealized_pnl: '100.00',
  unrealized_pnl_pct: '0.1000',
  realized_pnl: '0',
  fees_paid: '0',
  stop_loss_price: '90.00',
  take_profit_price: '130.00',
  distance_to_stop_pct: '2.0000',
  opened_at: '2024-06-02T14:30:00Z',
}

const SIGNAL: SignalView = {
  id: 'sig-1',
  ts: '2024-06-03T14:30:00Z',
  strategy_id: 'sma_crossover',
  symbol: 'AAPL',
  action: 'enter_long',
  reason: 'SMA(20) crossed above SMA(50)',
  indicators: { sma_fast: '100.5', sma_slow: '99.1' },
  acted_on: true,
  rejected_by: null,
  rejection_reason: null,
}

const ORDER: OrderView = {
  id: 'ord-1',
  client_order_id: 'atp-1',
  ts: '2024-06-03T14:30:00Z',
  symbol: 'MSFT',
  side: 'buy',
  order_type: 'limit',
  qty: '10',
  filled_qty: '4',
  limit_price: '400.25',
  stop_price: null,
  avg_fill_price: '400.10',
  status: 'partially_filled',
  strategy_id: 'sma_crossover',
}

describe('AccountSummary', () => {
  it('renders the headline figures', () => {
    render(<AccountSummary account={ACCOUNT} marketOpen bookAgeSeconds={12} />)

    expect(screen.getByText('101,234.56')).toBeDefined()
    expect(screen.getByText(/0.11×/)).toBeDefined()
  })

  it('shows a loss with a sign and an arrow, not only a colour', () => {
    render(<AccountSummary account={ACCOUNT} marketOpen bookAgeSeconds={0} />)

    expect(screen.getByText(/▼ -450\.25/)).toBeDefined()
  })

  it('says the book is unpublished rather than rendering an empty account', () => {
    // "You hold nothing" and "nobody has said what you hold" are different
    // sentences and only one of them is safe to act on.
    render(<AccountSummary account={null} marketOpen bookAgeSeconds={null} />)

    expect(screen.getByText(/No book published/)).toBeDefined()
    expect(screen.queryByText('0.00')).toBeNull()
  })

  it('shows a dash for a day P&L with no session anchor', () => {
    render(
      <AccountSummary
        account={{ ...ACCOUNT, day_pnl: null, day_pnl_pct: null }}
        marketOpen
        bookAgeSeconds={0}
      />,
    )

    expect(screen.getByText(/no session anchor yet/)).toBeDefined()
  })

  it('warns that the figures understate exposure when a position is unmarked', () => {
    // Non-empty `unmarked_symbols` means equity and exposure are both too low,
    // which is the direction that makes a breached limit look compliant.
    render(
      <AccountSummary
        account={{ ...ACCOUNT, unmarked_symbols: ['TSLA'] }}
        marketOpen
        bookAgeSeconds={0}
      />,
    )

    expect(screen.getByText(/understate exposure/)).toBeDefined()
    expect(screen.getByText(/TSLA/)).toBeDefined()
  })

  it('calls leverage undefined rather than zero at zero equity', () => {
    render(
      <AccountSummary account={{ ...ACCOUNT, leverage: null }} marketOpen bookAgeSeconds={0} />,
    )

    expect(screen.getByText(/undefined at zero equity/)).toBeDefined()
  })
})

describe('PositionsTable', () => {
  it('renders a holding with its distance to stop', () => {
    render(<PositionsTable positions={[POSITION]} />)

    const row = screen.getByRole('row', { name: /AAPL/ })
    expect(within(row).getByText('200%')).toBeDefined()
  })

  it('flags a position price has already passed its stop', () => {
    // A signed fraction, not an absolute one: clamping would render the most
    // alarming row on the screen as an ordinary small number.
    render(<PositionsTable positions={[{ ...POSITION, distance_to_stop_pct: '-0.5000' }]} />)

    expect(screen.getByText('THROUGH STOP')).toBeDefined()
  })

  it('says a position has no stop rather than implying it is safe', () => {
    render(
      <PositionsTable
        positions={[{ ...POSITION, stop_loss_price: null, distance_to_stop_pct: null }]}
      />,
    )

    expect(screen.getByText('no stop')).toBeDefined()
  })

  it('does not call a position with a stop it cannot price "no stop"', () => {
    // `distance_to_stop_pct` is null for two different reasons. Nothing
    // protecting the position is one; a stop that exists beside a position
    // nothing has priced is the other, and it is the more alarming of the two.
    // Rendering the second as the first put the words "no stop" in the same row
    // as a stop price — a contradiction on its face.
    render(
      <PositionsTable
        positions={[
          { ...POSITION, last_price: null, stop_loss_price: '90.00', distance_to_stop_pct: null },
        ]}
      />,
    )

    expect(screen.getByText('stop set, unmarked')).toBeDefined()
    expect(screen.queryByText('no stop')).toBeNull()
  })

  it('renders an unmarked position without inventing a value', () => {
    render(
      <PositionsTable
        positions={[
          {
            ...POSITION,
            last_price: null,
            market_value: null,
            unrealized_pnl: null,
            unrealized_pnl_pct: null,
          },
        ]}
      />,
    )

    const row = screen.getByRole('row', { name: /AAPL/ })
    expect(within(row).queryByText('0.00')).toBeNull()
    expect(within(row).getAllByText('—').length).toBeGreaterThan(0)
  })

  it('shows a live tick beside the mark rather than over it', () => {
    // The P&L in the same row was computed from the mark. Writing a newer price
    // over it would put two instants in one row, which is the disagreement the
    // single aggregate endpoint exists to prevent.
    render(
      <PositionsTable
        positions={[POSITION]}
        quotes={{
          AAPL: { symbol: 'AAPL', bid: '111.00', ask: '111.10', ts: '2024-06-03T14:31:00Z' },
        }}
      />,
    )

    const row = screen.getByRole('row', { name: /AAPL/ })
    expect(within(row).getByText('110.00')).toBeDefined()
    expect(within(row).getByText(/111\.00 \/ 111\.10/)).toBeDefined()
  })

  it('says it is flat rather than showing an empty table', () => {
    render(<PositionsTable positions={[]} />)

    expect(screen.getByText(/Flat — no open positions/)).toBeDefined()
  })

  it('marks a short as one', () => {
    render(<PositionsTable positions={[{ ...POSITION, qty: '-10' }]} />)

    expect(screen.getByText('SHORT')).toBeDefined()
  })
})

describe('SignalFeed', () => {
  it('shows the reason on every signal', () => {
    render(<SignalFeed signals={[SIGNAL]} />)

    expect(screen.getByText('SMA(20) crossed above SMA(50)')).toBeDefined()
  })

  it('keeps a refused signal and names the rule that refused it', () => {
    render(
      <SignalFeed
        signals={[
          {
            ...SIGNAL,
            acted_on: false,
            rejected_by: 'max_gross_exposure',
            rejection_reason: 'would exceed 100% of equity',
          },
        ]}
      />,
    )

    expect(screen.getByText(/blocked · max_gross_exposure/)).toBeDefined()
    expect(screen.getByText('would exceed 100% of equity')).toBeDefined()
  })

  it('separates nothing-to-do from a refusal', () => {
    // `no_action` is the router reporting an exit for a flat position, and it
    // is approved. Styling it as a rejection would inflate the number an
    // operator reads to judge whether their limits are too tight.
    render(
      <SignalFeed
        signals={[
          {
            ...SIGNAL,
            acted_on: false,
            rejected_by: 'no_action',
            rejection_reason: 'position is already flat',
          },
        ]}
      />,
    )

    expect(screen.getByText('no action')).toBeDefined()
    expect(screen.queryByText(/blocked/)).toBeNull()
  })

  it('shows the indicator values behind the decision', () => {
    render(<SignalFeed signals={[SIGNAL]} />)

    expect(screen.getByText('100.5')).toBeDefined()
  })

  it('says why the feed is empty rather than showing nothing', () => {
    render(<SignalFeed signals={[]} />)

    expect(screen.getByText(/a restart empties it/)).toBeDefined()
  })
})

describe('OrdersTable', () => {
  it('shows both halves of a partial fill', () => {
    // An order is not binary (CLAUDE.md §5). A row reporting only `qty` would
    // hide the position that already exists.
    render(<OrdersTable orders={[ORDER]} />)

    expect(screen.getByText('4 / 10')).toBeDefined()
  })

  it('names the price the order is resting at', () => {
    render(<OrdersTable orders={[ORDER]} />)

    expect(screen.getByText('limit 400.25')).toBeDefined()
  })

  it('says nothing is working rather than showing an empty table', () => {
    render(<OrdersTable orders={[]} />)

    expect(screen.getByText(/Nothing working at the venue/)).toBeDefined()
  })
})

describe('FeedStatus', () => {
  it('distinguishes unknown from healthy', () => {
    // A green light with no book behind it would be a guess.
    render(<FeedStatus healthy={null} lastDataAt={null} marketOpen />)

    expect(screen.getByText('feed unknown')).toBeDefined()
  })

  it('reports stale during a session', () => {
    render(<FeedStatus healthy={false} lastDataAt="2024-06-03T14:00:00Z" marketOpen />)

    expect(screen.getByText('feed stale')).toBeDefined()
  })

  it('reports quiet rather than stale out of hours', () => {
    // A light that goes red every evening is a light nobody reads.
    render(<FeedStatus healthy={false} lastDataAt="2024-06-03T14:00:00Z" marketOpen={false} />)

    expect(screen.getByText('feed quiet')).toBeDefined()
  })
})
