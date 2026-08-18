/**
 * The login screen.
 *
 * Shows the run mode *before* anyone signs in. docs/DASHBOARD.md calls that
 * banner the most important pixel on the screen, and the moment before you
 * authenticate is not an exception — an operator about to open a live-money
 * system should know that while they are still deciding to. It comes from
 * `/api/v1/auth/context`, which is unauthenticated and carries the mode and
 * nothing else.
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/api/client'
import { ApiError } from '@/api/client'
import { useLogin } from '@/api/session'
import type { PreSessionContext } from '@/api/types'

function RunMode() {
  // NOT the API's root `/`. nginx serves the dashboard there, so a request to
  // `/` returns index.html and never reaches the API — which is correct, and
  // means anything the browser needs lives under `/api`.
  const { data } = useQuery({
    queryKey: ['pre-session-context'],
    queryFn: () => apiGet<PreSessionContext>('/api/v1/auth/context'),
    retry: false,
    staleTime: Infinity,
  })
  if (!data?.run_mode) return null

  if (data.run_mode === 'backtest') {
    return (
      <div className="bg-slate-700 px-4 py-1.5 text-center text-xs font-semibold text-slate-200">
        BACKTEST MODE — no venue is connected
      </div>
    )
  }
  if (data.run_mode === 'paper') {
    return (
      <div className="bg-amber-500/90 px-4 py-1.5 text-center text-xs font-semibold text-amber-950">
        PAPER TRADING — simulated money, live market data
      </div>
    )
  }
  // Same rule as the banner inside the app: an unrecognised mode falls through
  // to the loudest branch, because guessing wrong towards "this is real" costs
  // a moment's caution and guessing the other way costs an order.
  return (
    <div className="bg-rose-600 px-4 py-2 text-center text-sm font-bold tracking-wide text-white">
      ⚠ LIVE TRADING — REAL MONEY AT RISK
    </div>
  )
}

export default function Login() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const login = useLogin()

  /**
   * One message for every rejection, matching the API. Distinguishing "no such
   * user" from "wrong password" here would undo the server's care about not
   * confirming which usernames exist.
   */
  const message =
    login.error instanceof ApiError
      ? login.error.status === 401
        ? 'Incorrect username or password.'
        : `Could not sign in — the server answered ${login.error.status}.`
      : login.error
        ? 'Could not reach the server.'
        : null

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <RunMode />
      <div className="flex items-center justify-center px-4 py-24">
        <form
          onSubmit={(event) => {
            event.preventDefault()
            login.mutate({ username, password })
          }}
          className="w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900/60 p-6"
        >
          <h1 className="text-lg font-semibold">ATP</h1>
          <p className="mt-1 text-sm text-slate-400">Sign in to view the trading book.</p>

          <label className="mt-6 block text-xs font-medium text-slate-400" htmlFor="username">
            Username
          </label>
          <input
            id="username"
            name="username"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-sky-500"
          />

          <label className="mt-4 block text-xs font-medium text-slate-400" htmlFor="password">
            Password
          </label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-sky-500"
          />

          {message ? (
            <p role="alert" className="mt-4 text-sm text-rose-400">
              {message}
            </p>
          ) : null}

          <button
            type="submit"
            disabled={login.isPending || !username || !password}
            className="mt-6 w-full rounded bg-sky-600 px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
          >
            {login.isPending ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}
