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
 * Polled on the same cadence as the dashboard rather than left static: the
 * worker rewrites this row every evaluation, so it does move — and a screen an
 * operator is watching during an incident should show the age advancing rather
 * than a frozen number that might mean either "nothing changed" or "this tab
 * stopped asking".
 */

import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/api/client'
import type { StoredBookView } from '@/api/types'

const REFRESH_MS = 5 * 60 * 1000

export function useStoredBook() {
  const query = useQuery<StoredBookView>({
    queryKey: ['positions', 'stored'],
    queryFn: () => apiGet<StoredBookView>('/api/v1/positions'),
    refetchInterval: REFRESH_MS,
    // A hidden tab does not poll, matching the dashboard: twenty forgotten tabs
    // refreshing every five minutes is real load for no reader.
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  })

  return {
    ...query,
    /**
     * How stale the *fetch* is, which is a different question from how stale
     * the *book* is. Both are shown: a tab that refreshed a second ago against
     * a worker that stopped publishing an hour ago is fresh by one measure and
     * useless by the other (docs/DASHBOARD.md).
     */
    fetchedSecondsAgo: query.dataUpdatedAt
      ? Math.floor((Date.now() - query.dataUpdatedAt) / 1000)
      : null,
  }
}
