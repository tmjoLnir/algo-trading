/**
 * Seconds elapsed since a timestamp, recomputed once a second.
 *
 * **Why this exists at all.** Every age on this dashboard used to be computed
 * during render as `Date.now() - dataUpdatedAt`, which is only correct if
 * something re-renders. The 5-minute poll was that something: it re-rendered
 * the tree on its own schedule and the number advanced as a side effect.
 * Refreshing manually removes it, and without a replacement "Updated 8s ago"
 * would sit at 8s for as long as the tab is open — the reassurance that hides
 * exactly the failure the age is displayed to reveal (docs/DASHBOARD.md).
 *
 * So the clock is now explicit rather than incidental, which is also the honest
 * shape: the age of a reading advances because time passes, not because
 * something happened to fetch.
 *
 * **Call this from the component that renders the age, not from a page.** It
 * re-renders its caller every second; on a page it would re-render the position
 * table and the equity chart with it, once a second, forever, to move one
 * caption.
 */

import { useEffect, useState } from 'react'

const TICK_MS = 1000

export function useSecondsSince(sinceMs: number | null | undefined): number | null {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS)
    return () => clearInterval(id)
  }, [])

  // Zero is React Query's "never fetched" for `dataUpdatedAt`, and is a real
  // answer rather than a missing one: there is no age because there was no read.
  if (!sinceMs) return null

  // Clamped because `now` lags a fresh fetch by up to one tick, and "updated
  // -1s ago" reads as a bug in the very component whose job is to be trusted.
  return Math.max(0, Math.floor((now - sinceMs) / 1000))
}
