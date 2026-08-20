import { QueryClientProvider } from '@tanstack/react-query'
import { createQueryClient } from '../api/queryClient'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { UNREACHABLE_RETRY_MS } from '../api/session'

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
  const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
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
  // The app's real client, not a bare one — the 401 rule lives on it, and a
  // test that built its own would be asserting against a client production
  // never uses.
  const queryClient = createQueryClient()
  queryClient.setDefaultOptions({
    queries: { ...queryClient.getDefaultOptions().queries, retry: false },
    mutations: { retry: false },
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
      '/api/v1/auth/me': { status: 200, body: { user: 'operator', scope: 'full' } },
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
      '/api/v1/auth/login': { status: 200, body: { user: 'operator', scope: 'full' } },
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

describe('read-only sessions', () => {
  it('asks for one when the box is ticked', async () => {
    const fetchMock = stubApi({
      '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } },
      '/api/v1/auth/login': { status: 200, body: { user: 'operator', scope: 'read' } },
      '/api/v1/dashboard/live': { status: 503, body: { detail: 'no store here' } },
      '/api/v1/dashboard/equity-curve': { status: 503, body: { detail: 'no store here' } },
    })
    renderApp()

    fireEvent.change(await screen.findByLabelText(/username/i), { target: { value: 'operator' } })
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: 'hunter2' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /read-only/i }))
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))

    await waitFor(() => expect(screen.getByRole('link', { name: 'Positions' })).toBeTruthy())

    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/auth/login'))
    expect(call).toBeTruthy()
    const body = JSON.parse(String(call![1]?.body))
    expect(body.read_only).toBe(true)
  })

  it('defaults to a full session when the box is left alone', async () => {
    const fetchMock = stubApi({
      '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } },
      '/api/v1/auth/login': { status: 200, body: { user: 'operator', scope: 'full' } },
      '/api/v1/dashboard/live': { status: 503, body: { detail: 'no store here' } },
      '/api/v1/dashboard/equity-curve': { status: 503, body: { detail: 'no store here' } },
    })
    renderApp()

    fireEvent.change(await screen.findByLabelText(/username/i), { target: { value: 'operator' } })
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: 'hunter2' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))

    await waitFor(() => expect(screen.getByRole('link', { name: 'Positions' })).toBeTruthy())

    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/auth/login'))
    const body = JSON.parse(String(call![1]?.body))
    expect(body.read_only).toBe(false)
  })

  it('says so on screen, because otherwise the difference is invisible', async () => {
    stubApi({
      '/api/v1/auth/me': { status: 200, body: { user: 'operator', scope: 'read' } },
      '/api/v1/dashboard/live': { status: 503, body: { detail: 'no store here' } },
      '/api/v1/dashboard/equity-curve': { status: 503, body: { detail: 'no store here' } },
    })
    renderApp()

    expect(await screen.findByText(/read-only/i)).toBeTruthy()
  })

  it('shows no badge on a full session', async () => {
    stubApi({
      '/api/v1/auth/me': { status: 200, body: { user: 'operator', scope: 'full' } },
      '/api/v1/dashboard/live': { status: 503, body: { detail: 'no store here' } },
      '/api/v1/dashboard/equity-curve': { status: 503, body: { detail: 'no store here' } },
    })
    renderApp()

    await screen.findByRole('link', { name: 'Positions' })
    expect(screen.queryByText(/read-only/i)).toBeNull()
  })
})

describe('a refusal is not a logout', () => {
  it('stays signed in when a request comes back 403', async () => {
    // The distinction that matters. A 403 is a read-only session being refused a
    // write, or a step-up wanting the password — the credential is fine. Dropping
    // the session over one would throw away a working login to "fix" a refusal
    // that was correct, and would send the operator to a login screen that cannot
    // help them.
    stubApi({
      '/api/v1/auth/me': { status: 200, body: { user: 'operator', scope: 'read' } },
      '/api/v1/dashboard/live': { status: 403, body: { detail: 'this session is read-only' } },
      '/api/v1/dashboard/equity-curve': { status: 403, body: { detail: 'read-only' } },
    })
    renderApp()

    expect(await screen.findByRole('link', { name: 'Positions' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^sign in$/i })).toBeNull()
  })

  it('does send you to sign in when the session has actually expired', async () => {
    // Every endpoint 401s, because that is what an expired session looks like —
    // the cookie is stale for `/auth/me` exactly as it is for the book. An
    // earlier version of this test stubbed `/auth/me` as 200 while a data query
    // 401'd, to isolate the global handler. That state cannot occur, and the
    // test failed for the right reason: the session query re-established
    // authentication from the 200 as fast as the handler cleared it.
    stubApi({
      '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } },
      '/api/v1/dashboard/live': { status: 401, body: { detail: 'not authenticated' } },
      '/api/v1/dashboard/equity-curve': { status: 401, body: { detail: 'not authenticated' } },
    })
    renderApp()

    expect(await screen.findByRole('button', { name: /sign in/i })).toBeTruthy()
  })
})

describe('an API that was not there yet', () => {
  /**
   * The gap between the dev server accepting requests and the API answering
   * them, which `make up` produces on every cold start: `web` depends on `api`
   * without waiting for it to be healthy, so the browser can load the dashboard
   * and have its very first `/auth/me` refused at the socket.
   *
   * These render with the app's REAL retry rule. The helper above deliberately
   * stubs it out to keep the other tests fast, and that is precisely why it
   * could not see this: with `retry: false` forced globally, "retries" and
   * "gives up instantly" look identical. `retryDelay: 0` is the only thing
   * changed, so what is under test — whether it retries at all — is untouched.
   */
  function renderWithRealRetries() {
    const queryClient = createQueryClient()
    queryClient.setDefaultOptions({
      queries: { ...queryClient.getDefaultOptions().queries, retryDelay: 0 },
      mutations: { retry: false },
    })
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  /** A stub whose answers can change mid-test, as a restarting API's do. */
  function mutableApi(routes: Record<string, { status: number; body?: unknown }>) {
    const state = { routes, fail: false }
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        if (state.fail) throw new TypeError('Failed to fetch')
        const url = new URL(String(input), 'http://test').pathname
        const route = state.routes[url] ?? { status: 404, body: { detail: 'not stubbed' } }
        return {
          ok: route.status < 400,
          status: route.status,
          statusText: 'stub',
          json: async () => route.body ?? {},
        } as Response
      }),
    )
    return state
  }

  it('reaches the sign-in form when the first attempt is refused at the socket', async () => {
    // One failure, then the API is up — the ordinary cold start. Before this
    // was fixed the single failure was terminal and the operator was told the
    // API could not be reached by a stack that was, by then, running.
    let attempt = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        attempt += 1
        if (attempt === 1) throw new TypeError('Failed to fetch')
        return {
          ok: false,
          status: 401,
          statusText: 'stub',
          json: async () => ({ detail: 'not authenticated' }),
        } as Response
      }),
    )

    renderWithRealRetries()

    expect(await screen.findByRole('button', { name: /sign in/i })).toBeTruthy()
    expect(screen.queryByText(/cannot reach the api/i)).toBeNull()
  })

  it('recovers on its own once the API answers, with nobody touching the page', async () => {
    // The promise the screen makes — "It will retry on its own" — asserted as
    // behaviour. Nothing here refocuses the tab, reloads, or clicks: recovery
    // has to come from the page itself or the sentence is not true.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const api = mutableApi({
        '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } },
      })
      api.fail = true

      renderWithRealRetries()
      expect(await screen.findByText(/cannot reach the api/i)).toBeTruthy()

      // The API comes back. No interaction of any kind follows.
      api.fail = false
      await vi.advanceTimersByTimeAsync(UNREACHABLE_RETRY_MS + 500)

      expect(await screen.findByRole('button', { name: /sign in/i })).toBeTruthy()
      expect(screen.queryByText(/cannot reach the api/i)).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('stops polling once it has an answer', async () => {
    // The other half of the rule. Recovery must not turn into a request every
    // five seconds, forever, for every operator with the dashboard open.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const fetchMock = stubApi({
        '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } },
        '/api/v1/auth/context': { status: 200, body: { run_mode: 'paper' } },
      })
      renderWithRealRetries()
      await screen.findByRole('button', { name: /sign in/i })

      const settled = fetchMock.mock.calls.length
      await vi.advanceTimersByTimeAsync(UNREACHABLE_RETRY_MS * 4)

      expect(fetchMock.mock.calls.length).toBe(settled)
    } finally {
      vi.useRealTimers()
    }
  })
})
