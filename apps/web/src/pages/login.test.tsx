import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'

/**
 * What the app does before anyone is signed in.
 *
 * The distinction worth testing is not "logged in or not" — it is the *third*
 * state. "Nobody is signed in" and "the server did not answer" look identical
 * to a naive gate, and collapsing them puts the operator at a login form typing
 * a password at a server that cannot check it, reading the failure as their own
 * mistake. Everything below exists to keep those three apart.
 *
 * Design: ADR 0008.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/**
 * Route fetches by exact path, so one test can answer /auth/me and /auth/login
 * differently.
 *
 * Exact, and not `endsWith`, because a suffix match makes `'/'` match every URL
 * ever requested. That is not hypothetical: it is how the run-mode banner
 * passed its test while being broken in a real browser, where the request it
 * actually made hit nginx's SPA fallback and came back as HTML.
 */
function stubApi(routes: Record<string, { status: number; body?: unknown }>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://test').pathname
    const match = Object.keys(routes).find((path) => url === path)
    const route = match ? routes[match]! : { status: 404, body: { detail: 'not stubbed' } }
    return {
      ok: route.status < 400,
      status: route.status,
      statusText: 'stub',
      json: async () => route.body ?? {},
    } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('the gate', () => {
  it('shows the sign-in form when nobody is signed in', async () => {
    stubApi({ '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } } })
    renderApp()

    expect(await screen.findByRole('button', { name: /sign in/i })).toBeTruthy()
    expect(screen.getByLabelText(/username/i)).toBeTruthy()
    expect(screen.getByLabelText(/password/i)).toBeTruthy()
  })

  it('does NOT show the sign-in form when the server is unreachable', async () => {
    // The one that matters. A network failure is not a sign-in problem, and
    // presenting it as one invites the operator to blame their password.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch')
      }),
    )
    renderApp()

    expect(await screen.findByText(/cannot reach the api/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /sign in/i })).toBeNull()
  })

  it('renders the dashboard shell once signed in', async () => {
    stubApi({
      '/api/v1/auth/me': { status: 200, body: { user: 'operator' } },
      '/api/v1/dashboard/live': { status: 503, body: { detail: 'no store here' } },
      '/api/v1/dashboard/equity-curve': { status: 503, body: { detail: 'no store here' } },
    })
    renderApp()

    // The navigation is the shell; it must not exist before authentication.
    expect(await screen.findByRole('link', { name: 'Positions' })).toBeTruthy()
    expect(screen.getByText('operator')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^sign in$/i })).toBeNull()
  })
})

describe('signing in', () => {
  it('reports one message for a rejected credential, without saying which half was wrong', async () => {
    stubApi({
      '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } },
      '/api/v1/auth/login': { status: 401, body: { detail: 'invalid username or password' } },
    })
    renderApp()

    fireEvent.change(await screen.findByLabelText(/username/i), { target: { value: 'operator' } })
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: 'wrong' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/incorrect username or password/i)
    // Neither field is named as the culprit — the server declines to confirm
    // which usernames exist, and the screen must not undo that.
    expect(alert.textContent).not.toMatch(/no such user|unknown user/i)
  })

  it('distinguishes a server error from a rejected credential', async () => {
    stubApi({
      '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } },
      '/api/v1/auth/login': { status: 500, body: { detail: 'boom' } },
    })
    renderApp()

    fireEvent.change(await screen.findByLabelText(/username/i), { target: { value: 'operator' } })
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: 'hunter2' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/500/)
    expect(alert.textContent).not.toMatch(/incorrect username or password/i)
  })

  it('will not submit an empty form', async () => {
    stubApi({ '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } } })
    renderApp()

    const button = (await screen.findByRole('button', { name: /sign in/i })) as HTMLButtonElement
    expect(button.disabled).toBe(true)
  })

  it('shows the shell after a successful sign-in, without a reload', async () => {
    stubApi({
      '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } },
      '/api/v1/auth/login': { status: 200, body: { user: 'operator' } },
      '/api/v1/dashboard/live': { status: 503, body: { detail: 'no store here' } },
      '/api/v1/dashboard/equity-curve': { status: 503, body: { detail: 'no store here' } },
    })
    renderApp()

    fireEvent.change(await screen.findByLabelText(/username/i), { target: { value: 'operator' } })
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: 'hunter2' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))

    await waitFor(() => expect(screen.getByRole('link', { name: 'Positions' })).toBeTruthy())
  })
})

describe('the run mode is visible before signing in', () => {
  it('names live trading on the login screen', async () => {
    // docs/DASHBOARD.md calls this the most important pixel on the screen. The
    // moment before you sign in is not an exception — it is when you are still
    // deciding whether to.
    stubApi({
      '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } },
      '/api/v1/auth/context': { status: 200, body: { run_mode: 'live' } },
    })
    renderApp()

    expect(await screen.findByText(/real money at risk/i)).toBeTruthy()
  })

  it('falls through to the loudest branch for a mode it does not recognise', async () => {
    stubApi({
      '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } },
      '/api/v1/auth/context': { status: 200, body: { run_mode: 'something-new' } },
    })
    renderApp()

    expect(await screen.findByText(/real money at risk/i)).toBeTruthy()
  })
})
