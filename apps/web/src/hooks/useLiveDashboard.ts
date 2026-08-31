/**
 * The live dashboard read — requirement #7.
 *
 * One query for the whole dashboard, so every number on screen comes from the
 * same instant (see the API's dashboard router for why that matters).
 *
 * **There is no longer a refresh cadence.** The reader decides when to read,
 * with the browser's own reload or the button on the screen (ADR 0022). What
 * remains automatic is deliberately only the three things that are not a timer:
 *
 * 1. `refetchOnWindowFocus: true` — returning to the tab re-reads. Kept, and
 *    load-bearing now rather than a convenience: with nothing on a schedule it
 *    is the one thing that refreshes a screen somebody walked back to.
 * 2. `staleTime: 0` — moving between tabs in the app re-reads rather than
 *    replaying a cache. It used to sit just under the poll interval because the
 *    poll was what drove refreshes; with no poll, that number would be the only
 *    thing standing between a navigation and a stale screen.
 * 3. A fill or a halt on the socket still invalidates this query. That is not a
 *    cadence — it is the book changing underneath the reader, which is the one
 *    case where waiting to be asked is wrong.
 *
 * The age of the reading is displayed and advances on its own clock
 * (`useSecondsSince`), because a number nobody refreshes still gets older.
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

/**
 * The book. Named because three separate things now invalidate it — a fill, a
 * halt, and a reconnect — and three copies of a key literal is how one of them
 * quietly stops matching after a rename.
 */
export const LIVE_DASHBOARD_KEY = ['dashboard', 'live'] as const

/** Where the socket writes ticks. Its own key: the aggregate read owns the book. */
export const LIVE_QUOTES_KEY = ['quotes', 'live'] as const

/** First reconnect wait, doubling from there. */
const RECONNECT_BASE_MS = 1_000

/** Ceiling on the wait. Reconnecting in a tight loop against a server that is
 * already struggling makes it worse. */
const RECONNECT_MAX_MS = 30_000

/**
 * How long to wait before attempt `n` (0-based): exponential, capped, jittered.
 *
 * The jitter is the part worth explaining, and it is the same reasoning as
 * `atp_core.ws.backoff_delay` on the server — one ladder's worth of thinking,
 * applied at both ends. Every socket that dropped for the same reason came down
 * at the same instant, so an unjittered ladder has them all knock again at the
 * same instant, and again after that: the API is restarted, every open tab
 * reconnects together, and the moment it is worst able to cope is the moment
 * they all arrive. Uniform in `[delay/2, delay]`, so it only ever waits *less*
 * than the ceiling and a reconnect is never slower for being jittered.
 */
function reconnectDelay(attempt: number): number {
  const capped = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS)
  return capped * (0.5 + Math.random() / 2)
}

/** One tick, as the socket delivers it. Prices stay strings (rule §1.1). */
export interface LiveQuote {
  symbol: string
  bid: string
  ask: string
  ts: string
}

export function useLiveDashboard() {
  const query = useQuery<LiveDashboard>({
    queryKey: LIVE_DASHBOARD_KEY,
    queryFn: () => apiGet<LiveDashboard>('/api/v1/dashboard/live'),
    refetchOnWindowFocus: true,
    staleTime: 0,
    retry: 3,
  })

  return query
}

/**
 * The equity curve behind the headline chart.
 *
 * A separate query from the dashboard's, on purpose. It is a *history*, not
 * part of the current picture, so it does not need to share the aggregate's
 * instant — and putting it in the aggregate would make every read drag a month
 * of points behind it. It re-reads when the dashboard does, because the newest
 * point is the one the reader is looking at.
 */
export function useEquityCurve(days = 30, resolution?: string) {
  const suffix = resolution ? `&resolution=${resolution}` : ''
  return useQuery<EquityCurveView>({
    queryKey: ['dashboard', 'equity-curve', days, resolution ?? 'auto'],
    queryFn: () => apiGet<EquityCurveView>(`/api/v1/dashboard/equity-curve?days=${days}${suffix}`),
    staleTime: 0,
    retry: 2,
  })
}

/** The latest tick per symbol, as delivered by the socket since the last read. */
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
 * Live push between reads.
 *
 * **Ticks do not patch the book.** A quote goes into its own cache entry and is
 * rendered as its own, clearly-labelled live price; the position's mark, its
 * P&L and every percentage beside it stay exactly as the read delivered them,
 * with the book's age shown. The alternative — writing a fresh price into the
 * position and leaving the P&L computed from the old one — produces precisely
 * the screen assembled from two instants that the single aggregate endpoint
 * exists to prevent, and recomputing the P&L here would mean doing arithmetic
 * on money in IEEE 754 (rule §1.1).
 *
 * A fill or a halt refetches instead: both change more of the picture than one
 * message carries, and the aggregate read is the authoritative path.
 *
 * The socket opens even when there is nothing to subscribe to. Halts are
 * delivered to every client regardless of subscription, and a dashboard holding
 * no positions is exactly the one that most needs to be told trading has
 * stopped.
 *
 * **A reconnect re-reads the book, because a reconnect means a gap.** Redis
 * pub/sub has no replay, so everything published while this socket was down
 * reached nobody and is not coming — a fill, and in the worst case a halt. That
 * used to be survivable by accident: the dashboard polled, so the next poll
 * repaired it within five minutes without anybody deciding it should. Nothing
 * polls now (ADR 0022), so the repair has to be deliberate or it does not
 * happen, and the screen carries the pre-outage book for as long as the tab
 * stays open — with the halt banner, which is the one thing on the page whose
 * job is to interrupt somebody, showing the state of trading from before the
 * disconnection.
 *
 * This is the browser's half of the reconnect gap that CLAUDE.md §5 names, and
 * the read is the whole backfill: unlike a market-data feed there is no history
 * to re-request here, because one aggregate read *is* the current state of
 * everything the socket carries. So it costs one Redis GET, which ADR 0022
 * measured at indistinguishable from zero on this host, in exchange for the
 * screen never being quietly wrong after a blip.
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
    // Whether this effect has ever had a live socket. The distinction is the
    // whole of the gap repair below: the first open of a freshly-mounted effect
    // has missed nothing, because the query that renders alongside it is
    // fetching the book at the same moment. Every open after that is a
    // reconnection, and a reconnection is by definition preceded by a stretch
    // where the server was publishing to a socket nobody was holding.
    let everOpened = false

    const connect = () => {
      ws = new WebSocket(url)

      ws.onopen = () => {
        attempt = 0
        if (everOpened) {
          queryClient.invalidateQueries({ queryKey: LIVE_DASHBOARD_KEY })
        }
        everOpened = true
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
            queryClient.invalidateQueries({ queryKey: LIVE_DASHBOARD_KEY })
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

        reconnectTimer = setTimeout(connect, reconnectDelay(attempt++))
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
