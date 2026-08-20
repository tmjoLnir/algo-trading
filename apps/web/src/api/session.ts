/**
 * Who is logged in, and how to change that.
 *
 * The page cannot read the session cookie — it is `HttpOnly`, which is the
 * point — so "am I logged in" is a question only the server can answer. That
 * makes it a query like any other rather than a piece of client state, and it
 * means a session that expires while a tab is open resolves itself on the next
 * fetch instead of leaving the app convinced it is still authenticated.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, apiGet, apiPost } from './client'
import type { WhoAmI } from './types'

export const SESSION_KEY = ['session'] as const

/**
 * How often to re-ask while the API is unreachable.
 *
 * Short enough that an operator who has just started the stack sees the login
 * screen appear by itself rather than reaching for a reload, long enough that a
 * dashboard left open against a dead API is not hammering it.
 */
export const UNREACHABLE_RETRY_MS = 5_000

/**
 * The current session, or null when there is none.
 *
 * A 401 is an *answer*, not a failure: it means "nobody is logged in", which is
 * exactly what the login screen needs to know. Anything else is left to throw,
 * because a dashboard that renders a login form when the API is merely down
 * would have the operator typing a password at a server that cannot check it.
 */
export function useSession() {
  const query = useQuery({
    queryKey: SESSION_KEY,
    queryFn: async (): Promise<WhoAmI | null> => {
      try {
        return await apiGet<WhoAmI>('/api/v1/auth/me')
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null
        throw error
      }
    },
    // `retry` is deliberately NOT set here, and used to be `false`.
    //
    // The intent behind that was right and the reach was wrong: a 401 is
    // settled, and retrying it delays the login screen for nothing. But the
    // client's own default already refuses exactly that and retries everything
    // else twice (`queryClient.ts`), so `false` here bought nothing for the 401
    // and turned every *other* failure into a permanent one — including the
    // connection refused that `make up` produces for the few seconds between
    // the dev server accepting requests and the API being ready to answer them.
    // One unlucky first paint and the gate below is stuck on "cannot reach the
    // API" for the life of the page.
    //
    // `staleTime: 0` because the alternative is a cached "yes" outliving the
    // session it describes.
    staleTime: 0,
    // What makes "it will retry on its own" true.
    //
    // Retries cover a blip that resolves inside a couple of seconds; they do
    // nothing for an API that comes back a minute later, and until this existed
    // nothing did. The screen recovered only if the operator happened to
    // refocus the tab or reload — so an API that fixed itself left a dashboard
    // that did not, saying it would.
    //
    // Only while the answer is an error: polling a settled session every five
    // seconds would be a request per operator per five seconds, forever, to
    // re-learn something `staleTime: 0` already refreshes on demand.
    refetchInterval: (query) => (query.state.error ? UNREACHABLE_RETRY_MS : false),
  })

  return {
    user: query.data?.user ?? null,
    scope: query.data?.scope ?? null,
    // What this session may do, as the server last reported it. The screen uses
    // it to avoid offering controls that would only be refused; the server does
    // not take the screen's word for it and re-checks every request (ADR 0009).
    mayAct: query.data?.scope === 'full',
    isAuthenticated: query.data != null,
    isPending: query.isPending,
    error: query.error,
  }
}

export function useLogin() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (credentials: { username: string; password: string; read_only: boolean }) =>
      apiPost<WhoAmI>('/api/v1/auth/login', credentials),
    onSuccess: (who) => {
      // Seed rather than invalidate: the response already says who logged in,
      // so re-asking would be a round trip to learn what is in hand.
      queryClient.setQueryData(SESSION_KEY, who)
    },
  })
}

export function useLogout() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => apiPost<void>('/api/v1/auth/logout'),
    onSuccess: () => {
      queryClient.setQueryData(SESSION_KEY, null)
      // Everything else on the screen was fetched as the person logging out.
      // Clearing it means the next login cannot briefly render their book.
      queryClient.clear()
    },
  })
}
