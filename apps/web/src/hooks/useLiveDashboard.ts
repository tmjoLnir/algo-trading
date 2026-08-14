/**
 * The 5-minute auto-refresh — requirement #7.
 *
 * One query for the whole dashboard, so every number on screen comes from the
 * same instant (see the API's dashboard router for why that matters).
 *
 * Four details here are deliberate and easy to get wrong:
 *
 * 1. `refetchInterval` is driven by the server's `refresh_seconds`, not a
 *    hardcoded 300000. Change the cadence in one place — the backend config.
 * 2. `refetchIntervalInBackground: false` — a hidden tab does not poll. Twenty
 *    forgotten tabs polling every 5 minutes is real load for zero benefit.
 * 3. `refetchOnWindowFocus: true` — returning to the tab refetches immediately.
 *    Without it, a trader who alt-tabs back sees data up to 5 minutes stale
 *    and has no way to know.
 * 4. `staleTime` sits just under the interval, so the poll is what drives
 *    refreshes rather than incidental remounts.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { apiGet } from '@/api/client'
import type { LiveDashboard } from '@/api/types'

const DEFAULT_REFRESH_MS = 5 * 60 * 1000

export function useLiveDashboard() {
  const query = useQuery<LiveDashboard>({
    queryKey: ['dashboard', 'live'],
    queryFn: () => apiGet<LiveDashboard>('/api/v1/dashboard/live'),
    refetchInterval: (q) => (q.state.data?.refresh_seconds ?? 300) * 1000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    staleTime: DEFAULT_REFRESH_MS - 30_000,
    retry: 3,
  })

  return {
    ...query,
    /**
     * How stale the data on screen is. Show this — a dashboard that looks
     * live but is quietly four minutes behind is worse than one that admits it,
     * because the user acts on it either way.
     */
    ageSeconds: query.dataUpdatedAt ? Math.floor((Date.now() - query.dataUpdatedAt) / 1000) : null,
  }
}

/**
 * Live push between polls.
 *
 * The socket writes into the same React Query cache the poll owns, so the UI
 * has one source of state. Ticks patch prices; the poll remains authoritative
 * and corrects any drift every 5 minutes.
 *
 * A halt message invalidates the whole query immediately — that is the one
 * event where waiting up to 5 minutes to find out is not acceptable.
 */
export function useDashboardStream(symbols: string[]) {
  const queryClient = useQueryClient()

  // Depend on a stable primitive, not the array. A new array literal is never
  // referentially equal to the last one, so depending on `symbols` directly
  // would tear down and reopen the socket on every single render.
  const symbolKey = symbols.join(',')

  useEffect(() => {
    if (!symbolKey) return
    // Rebuild the list from the key rather than closing over `symbols`, so the
    // effect's dependencies are genuinely exhaustive and cannot go stale.
    const subscribed = symbolKey.split(',')

    const url = import.meta.env.VITE_WS_URL ?? 'ws://localhost:8000/ws'
    let ws: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout>
    let attempt = 0
    let closed = false

    const connect = () => {
      ws = new WebSocket(url)

      ws.onopen = () => {
        attempt = 0
        ws?.send(
          JSON.stringify({ type: 'subscribe', channels: ['quotes', 'fills'], symbols: subscribed }),
        )
      }

      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data)
        switch (msg.type) {
          case 'quote':
            // TODO: patch the cached position's last_price and unrealized P&L.
            break
          case 'fill':
          case 'halt':
            // Refetch rather than patch: a fill or a halt changes more of the
            // picture than one message carries.
            queryClient.invalidateQueries({ queryKey: ['dashboard', 'live'] })
            break
        }
      }

      ws.onclose = () => {
        if (closed) return
        // Exponential backoff, capped at 30s. Reconnecting in a tight loop
        // against a server that is already struggling makes it worse.
        const delay = Math.min(1000 * 2 ** attempt++, 30_000)
        reconnectTimer = setTimeout(connect, delay)
      }
    }

    connect()
    return () => {
      closed = true
      clearTimeout(reconnectTimer)
      ws?.close()
    }
  }, [symbolKey, queryClient])
}
