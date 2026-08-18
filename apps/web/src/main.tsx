import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { ApiError } from './api/client'
import { SESSION_KEY } from './api/session'
import './index.css'

/**
 * A 401 from *any* query means the session ended, not that one request failed.
 *
 * Sessions expire on a timer (`API_SESSION_HOURS`), so the usual way to meet one
 * is a tab left open overnight: the 5-minute poll comes back 401 and every
 * subsequent one does too. Handled here rather than in each caller because the
 * response is the same wherever it happens — stop pretending to be logged in —
 * and because a component that quietly swallowed it would leave the last good
 * book on screen, labelled fresh, for a viewer the server no longer recognises.
 */
const queryCache = new QueryCache({
  onError: (error) => {
    if (error instanceof ApiError && error.status === 401) {
      queryClient.setQueryData(SESSION_KEY, null)
    }
  },
})

const queryClient = new QueryClient({
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

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
)
