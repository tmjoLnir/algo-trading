/**
 * The live dashboard — requirement #7.
 *
 * Auto-refreshes every 5 minutes via `useLiveDashboard`, with WebSocket ticks
 * in between. Layout priority, top to bottom, is what a person needs in the
 * order they need it: am I in danger → what do I hold → why → what's pending.
 */

import { useLiveDashboard, useDashboardStream } from '@/hooks/useLiveDashboard'
import AccountSummary from '@/components/AccountSummary'
import PositionsTable from '@/components/PositionsTable'
import SignalFeed from '@/components/SignalFeed'
import OrdersTable from '@/components/OrdersTable'
import EquityChart from '@/components/EquityChart'
import RefreshIndicator from '@/components/RefreshIndicator'
import KillSwitchButton from '@/components/KillSwitchButton'

export default function Dashboard() {
  const { data, isLoading, error, ageSeconds, refetch, isFetching } = useLiveDashboard()
  useDashboardStream(data?.positions.map((p) => p.symbol) ?? [])

  if (isLoading) return <div className="p-8 text-slate-400">Loading…</div>

  // Show the error AND keep the last good data on screen. A blank dashboard
  // during a transient API blip is worse than stale data clearly labelled as
  // stale — the user still needs to see what they hold.
  if (error && !data) {
    return <div className="p-8 text-rose-400">Failed to load dashboard: {String(error)}</div>
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <RefreshIndicator
          ageSeconds={ageSeconds}
          isFetching={isFetching}
          onRefresh={() => refetch()}
          intervalSeconds={data?.refresh_seconds ?? 300}
          stale={Boolean(error)}
        />
        <KillSwitchButton halted={(data?.active_halts.length ?? 0) > 0} />
      </div>

      {data && (
        <>
          <AccountSummary account={data.account} marketOpen={data.market_open} />
          <EquityChart />
          {/* Positions before signals: what you are exposed to matters more
              than what the system is thinking about. */}
          <PositionsTable positions={data.positions} />
          <div className="grid gap-4 lg:grid-cols-2">
            <SignalFeed signals={data.recent_signals} />
            <OrdersTable orders={data.working_orders} />
          </div>
        </>
      )}
    </div>
  )
}
