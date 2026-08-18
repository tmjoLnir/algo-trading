/**
 * The app's React Query client, and the two rules that live on it.
 *
 * Built here rather than inline in `main.tsx` because both rules below are
 * behaviour rather than bootstrapping — "an expired session sends you to the
 * login screen" is a thing the app does, and a thing worth a test. While this
 * lived in the entry point no test could reach it, so the 401 handling was
 * asserted nowhere and the tests quietly ran against a client that did not have
 * it.
 */

import { QueryCache, QueryClient } from '@tanstack/react-query'
import { ApiError } from './client'
import { SESSION_KEY } from './session'

export function createQueryClient(): QueryClient {
  const queryCache = new QueryCache({
    /**
     * A 401 from *any* query means the session ended, not that one request
     * failed.
     *
     * Sessions expire on a timer (`API_SESSION_HOURS`), so the usual way to meet
     * one is a tab left open overnight: the 5-minute poll comes back 401 and so
     * does every one after it. Handled centrally because the response is the
     * same wherever it happens — stop pretending to be logged in — and because a
     * component that swallowed it would leave the last good book on screen,
     * labelled fresh, for a viewer the server no longer recognises.
     *
     * **401 only, deliberately — never 403.** A 403 is a read-only session being
     * refused a write, or a step-up asking for the password again (ADR 0009).
     * The credential is fine and the session is still good; dropping it would
     * throw away a working login to "fix" a refusal that was correct, and send
     * the operator to a login screen that cannot help them.
     */
    onError: (error) => {
      if (error instanceof ApiError && error.status === 401) {
        client.setQueryData(SESSION_KEY, null)
      }
    },
  })

  const client = new QueryClient({
    queryCache,
    defaultOptions: {
      queries: {
        // Trading data is never worth showing indefinitely without a refresh;
        // per-query settings override this where a different cadence applies.
        staleTime: 30_000,
        // A 401 is settled — retrying it twice only delays the login screen by
        // two round trips. Everything else keeps the original two attempts.
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.status === 401) return false
          return failureCount < 2
        },
        refetchOnWindowFocus: true,
      },
    },
  })

  return client
}
