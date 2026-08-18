/**
 * The live dashboard — requirement #7.
 *
 * Auto-refreshes every 5 minutes via `useLiveDashboard`, with WebSocket ticks
 * in between. Layout priority, top to bottom, is what a person needs in the
 * order they need it: am I in danger → what do I hold → why → what's pending.
 *
 * The run-mode and halt banners are not here: they live above the nav in
 * `App`, because whether this is real money and whether trading is stopped are
 * the two facts a user must never have to scroll or click to discover.
 */

import { useDashboardStream, useLiveDashboard, useLiveQuotes } from '@/hooks/useLiveDashboard'
import AccountSummary from '@/components/AccountSummary'
import PositionsTable from '@/components/PositionsTable'
import SignalFeed from '@/components/SignalFeed'
import OrdersTable from '@/components/OrdersTable'
import EquityChart from '@/components/EquityChart'
import FeedStatus from '@/components/FeedStatus'
import RefreshIndicator from '@/components/RefreshIndicator'
import KillSwitchButton from '@/components/KillSwitchButton'

export default function Dashboard() {
  const { data, isLoading, error, ageSeconds, refetch, isFetching } = useLiveDashboard()
  const quotes = useLiveQuotes()
  // Subscribed to what the book holds, but the socket opens regardless — halts
  // reach every client whether it asked for anything or not, and a dashboard
  // holding nothing is the one that most needs to hear about one.
  useDashboardStream(data?.positions?.map((p) => p.symbol) ?? [])

  if (isLoading) return <div className="p-8 text-slate-400">Loading…</div>

  // Show the error AND keep the last good data on screen. A blank dashboard
  // during a transient API blip is worse than stale data clearly labelled as
  // stale — the user still needs to see what they hold.
  if (error && !data) {
    return <div className="p-8 text-rose-400">Failed to load dashboard: {String(error)}</div>
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <RefreshIndicator
          ageSeconds={ageSeconds}
          isFetching={isFetching}
          onRefresh={() => refetch()}
          intervalSeconds={data?.refresh_seconds ?? 300}
          stale={Boolean(error)}
          bookAgeSeconds={data?.book_age_seconds ?? null}
        />
        <div className="flex items-center gap-3">
          <FeedStatus
            healthy={data?.data_feed_healthy ?? null}
            lastDataAt={data?.last_data_at ?? null}
            marketOpen={data?.market_open ?? false}
          />
          <KillSwitchButton halted={(data?.active_halts?.length ?? 0) > 0} />
        </div>
      </div>

      {error ? (
        // Kept alongside the data rather than instead of it.
        <p className="rounded border border-amber-700/60 bg-amber-950/30 px-3 py-2 text-sm text-amber-200">
          ⚠ The last refresh failed — everything below is as of{' '}
          {ageSeconds === null ? 'an unknown time' : `${ageSeconds}s ago`} and may be out of date.
        </p>
      ) : null}

      {data && (
        <>
          <AccountSummary
            account={data.account}
            marketOpen={data.market_open}
            bookAgeSeconds={data.book_age_seconds}
          />
          <EquityChart />
          {/* Positions before signals: what you are exposed to matters more
              than what the system is thinking about. */}
          <PositionsTable positions={data.positions ?? []} quotes={quotes} />
          <div className="grid gap-4 lg:grid-cols-2">
            <SignalFeed signals={data.recent_signals ?? []} />
            <OrdersTable orders={data.working_orders ?? []} />
          </div>
        </>
      )}
    </div>
  )
}
