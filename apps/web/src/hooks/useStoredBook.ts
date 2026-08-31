/**
 * The stored book — what the worker last wrote to the database.
 *
 * Deliberately a different source from `useLiveDashboard`, which reads the book
 * the worker *published to Redis*. That one is the right source for the live
 * screen and is gone the moment the worker stops publishing: `/dashboard/live`
 * then correctly reports no book at all. This one survives, because the same
 * book is written to Postgres at every evaluation.
 *
 * So this answers the question the live screen cannot at the moment it matters
 * most — what am I holding, asked because the worker just died. The price is
 * that the answer can be arbitrarily old, which is why `age_seconds` travels
 * with it and why nothing on this screen renders without it.
 *
 * Read on demand rather than on a cadence (ADR 0022): the reader reloads, or
 * returns to the tab, and this re-reads. The worker rewrites this row every
 * evaluation, so what is on screen does go out of date between reads — which is
 * why the page shows how long ago it was read *and* how old the book was when
 * it was, and keeps both advancing on their own clock. A frozen number that
 * might mean either "nothing changed" or "this tab stopped asking" is the one
 * outcome this screen cannot afford.
 */

import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/api/client'
import type { StoredBookView } from '@/api/types'

export function useStoredBook() {
  const query = useQuery<StoredBookView>({
    queryKey: ['positions', 'stored'],
    queryFn: () => apiGet<StoredBookView>('/api/v1/positions'),
    // Matching the dashboard: no cadence, and `staleTime: 0` so returning to
    // this tab — in the app or in the window manager — re-reads rather than
    // replaying whatever was cached the last time somebody looked.
    staleTime: 0,
    refetchOnWindowFocus: true,
  })

  // `dataUpdatedAt` is returned raw rather than as an age. How stale the *fetch*
  // is and how stale the *book* is are two different questions, both shown, and
  // both have to keep advancing while nobody fetches anything — so the arithmetic
  // belongs in the component that renders them, next to its clock.
  return query
}
