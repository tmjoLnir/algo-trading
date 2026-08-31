/**
 * Holds the dashboard's WebSocket open for the whole signed-in session.
 *
 * Renders nothing. It exists because *where* the socket is held is a decision,
 * and holding it on one route was the wrong one.
 *
 * `useDashboardStream` used to be called by `Dashboard`, so React Router closed
 * the socket on navigation and the platform had a live connection only while
 * somebody was looking at `/`. On `/positions`, `/orders`, `/analytics` and the
 * rest — which is most of where an operator spends an incident — the API logged
 * `clients=0` and published to nobody.
 *
 * That is a contradiction the codebase states in three places without anywhere
 * enforcing it. `ws.py` delivers halts to every client whether it subscribed or
 * not, on the grounds that a halt is not something to opt into. `HaltBanner` is
 * mounted above the nav rather than on a page, on the grounds that whether
 * trading is stopped must never need scrolling or a click to discover. And
 * ADR 0022 kept halts on the push path when the refresh cadence went away, on
 * the grounds that a screen whose job is to interrupt somebody cannot require
 * being consulted first. All three are about the banner, and the banner is on
 * every screen — so the socket that feeds it has to be too, or the guarantee
 * holds on exactly one of seven routes and reads as if it holds on all of them.
 *
 * Mounted inside the authenticated tree and not at the top of `App`, because
 * hooks cannot sit behind `App`'s early returns and a socket opened before the
 * session is checked is one the API refuses with 1008 — which the reconnect
 * ladder correctly reads as "sign out", producing a login screen for a user who
 * was signing in.
 *
 * The subscription still follows the book's symbols. That query is already read
 * by `HaltBanner` on every screen, so asking for it here costs a cache hit
 * rather than a request.
 */

import { useDashboardStream, useLiveDashboard } from '@/hooks/useLiveDashboard'

export default function LiveStream() {
  const { data } = useLiveDashboard()
  // Subscribed to what the book holds, but the socket opens regardless — halts
  // reach every client whether it asked for anything or not, and a session
  // holding nothing is the one that most needs to hear about one.
  useDashboardStream(data?.positions?.map((p) => p.symbol) ?? [])
  return null
}
