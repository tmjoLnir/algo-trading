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
import { wsUrl } from '@/api/origin'
import { SESSION_KEY } from '@/api/session'
import type { EquityCurveView, LiveDashboard } from '@/api/types'

/**
 * Close code 1008, "policy violation" — what `ws.py` refuses an unauthenticated
 * handshake with. Named because a bare 1008 in a condition reads as a magic
 * number, and this one carries a decision.
 */
const WS_POLICY_VIOLATION = 1008

const DEFAULT_REFRESH_MS = 5 * 60 * 1000

/** Where the socket writes ticks. Its own key: the poll owns the book. */
export const LIVE_QUOTES_KEY = ['quotes', 'live'] as const

/** One tick, as the socket delivers it. Prices stay strings (rule §1.1). */
export interface LiveQuote {
  symbol: string
  bid: string
  ask: string
  ts: string
}

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
 * The equity curve behind the headline chart.
 *
 * A separate query from the dashboard's, on purpose. It is a *history*, not
 * part of the current picture, so it does not need to share the aggregate's
 * instant — and putting it in the aggregate would make every 5-minute poll drag
 * a month of points behind it. It refreshes on the same cadence because the
 * newest point is the one the reader is looking at.
 */
export function useEquityCurve(days = 30, resolution?: string) {
  const suffix = resolution ? `&resolution=${resolution}` : ''
  return useQuery<EquityCurveView>({
    queryKey: ['dashboard', 'equity-curve', days, resolution ?? 'auto'],
    queryFn: () => apiGet<EquityCurveView>(`/api/v1/dashboard/equity-curve?days=${days}${suffix}`),
    refetchInterval: DEFAULT_REFRESH_MS,
    refetchIntervalInBackground: false,
    staleTime: DEFAULT_REFRESH_MS - 30_000,
    retry: 2,
  })
}

/** The latest tick per symbol, as delivered by the socket since the last poll. */
export function useLiveQuotes(): Record<string, LiveQuote> {
  const { data } = useQuery<Record<string, LiveQuote>>({
    queryKey: LIVE_QUOTES_KEY,
    // Never fetched — the socket is the only writer. `initialData` gives the
    // cache an entry to be written into before the first message arrives.
    queryFn: () => Promise.resolve({}),
    initialData: {},
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  })
  return data
}

/**
 * Live push between polls.
 *
 * **Ticks do not patch the book.** A quote goes into its own cache entry and is
 * rendered as its own, clearly-labelled live price; the position's mark, its
 * P&L and every percentage beside it stay exactly as the poll delivered them,
 * with the book's age shown. The alternative — writing a fresh price into the
 * position and leaving the P&L computed from the old one — produces precisely
 * the screen assembled from two instants that the single aggregate endpoint
 * exists to prevent, and recomputing the P&L here would mean doing arithmetic
 * on money in IEEE 754 (rule §1.1).
 *
 * A fill or a halt refetches instead: both change more of the picture than one
 * message carries, and the poll is the authoritative path.
 *
 * The socket opens even when there is nothing to subscribe to. Halts are
 * delivered to every client regardless of subscription, and a dashboard holding
 * no positions is exactly the one that most needs to be told trading has
 * stopped.
 */
export function useDashboardStream(symbols: string[]) {
  const queryClient = useQueryClient()

  // Depend on a stable primitive, not the array. A new array literal is never
  // referentially equal to the last one, so depending on `symbols` directly
  // would tear down and reopen the socket on every single render.
  const symbolKey = symbols.join(',')

  useEffect(() => {
    // Rebuild the list from the key rather than closing over `symbols`, so the
    // effect's dependencies are genuinely exhaustive and cannot go stale.
    const subscribed = symbolKey ? symbolKey.split(',') : []

    const url = wsUrl()
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
        let msg: { type?: string; symbol?: string; bid?: string; ask?: string; ts?: string }
        try {
          msg = JSON.parse(event.data)
        } catch {
          // A frame we cannot read is a server bug, not a reason to tear the
          // socket down and reconnect into the same bug.
          return
        }
        switch (msg.type) {
          case 'quote':
            if (msg.symbol && msg.bid && msg.ask && msg.ts) {
              const quote: LiveQuote = {
                symbol: msg.symbol,
                bid: msg.bid,
                ask: msg.ask,
                ts: msg.ts,
              }
              queryClient.setQueryData<Record<string, LiveQuote>>(LIVE_QUOTES_KEY, (current) => ({
                ...(current ?? {}),
                [quote.symbol]: quote,
              }))
            }
            break
          case 'fill':
          case 'halt':
            // Refetch rather than patch: a fill or a halt changes more of the
            // picture than one message carries.
            queryClient.invalidateQueries({ queryKey: ['dashboard', 'live'] })
            break
        }
      }

      ws.onclose = (event) => {
        if (closed) return

        // 1008 is the server refusing this socket on policy — here, no valid
        // session (ADR 0008). Retrying cannot fix it: every reconnect sends the
        // same cookie and is refused the same way, forever, at whatever backoff.
        // Dropping the session instead sends the app to the login screen, which
        // is the only thing that *can* fix it.
        if (event.code === WS_POLICY_VIOLATION) {
          closed = true
          queryClient.setQueryData(SESSION_KEY, null)
          return
        }

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
