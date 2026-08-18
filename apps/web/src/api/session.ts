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
    // No retry: a 401 is settled, and retrying it three times only delays the
    // login screen. `staleTime: 0` because the alternative is a cached "yes"
    // outliving the session it describes.
    retry: false,
    staleTime: 0,
  })

  return {
    user: query.data?.user ?? null,
    isAuthenticated: query.data != null,
    isPending: query.isPending,
    error: query.error,
  }
}

export function useLogin() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (credentials: { username: string; password: string }) =>
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
