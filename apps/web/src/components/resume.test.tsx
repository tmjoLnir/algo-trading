import { QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '../api/queryClient'
import ResumeButton from './ResumeButton'
import type { HaltView } from '../api/types'

/**
 * Clearing a halt, from the side a person sees.
 *
 * The mirror of `killswitch.test.tsx`, and the failure it has to get right is
 * the opposite one. A halt that silently failed leaves trading about to restart
 * by itself; a resume that silently failed leaves the platform stopped while
 * the operator believes it is running — so the tests below spend most of their
 * time on the password gate and on what the form does when the server says no.
 *
 * The one property with no counterpart on the halt side is the key: a halt is
 * keyed on (scope, target), so the request this form sends has to name the halt
 * it is sitting on rather than "global" — a button that always cleared
 * everything would look identical on a screen with one halt and be wrong on
 * every screen with two.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** The 503 the API answers when the clear could not be written. */
const NOT_RESUMED =
  'trading was NOT resumed: Connection refused. The halt is still in force and the ' +
  'switch fails closed, so nothing is trading — this failed safe.'

const GLOBAL_HALT: HaltView = {
  scope: 'global',
  reason: 'daily_loss_limit',
  detail: '-3.2%',
  engaged_at: '2026-08-18T12:00:00Z',
  engaged_by: 'risk',
  target: null,
}

const SYMBOL_HALT: HaltView = { ...GLOBAL_HALT, scope: 'symbol', target: 'SPY' }

function stubResume(route: { status: number; body?: unknown }) {
  const fetchMock = vi.fn(
    async (_input: RequestInfo | URL, _init?: RequestInit) =>
      ({
        ok: route.status < 400,
        status: route.status,
        statusText: 'stub',
        json: async () => route.body ?? {},
      }) as Response,
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderButton(halt: HaltView = GLOBAL_HALT) {
  const queryClient = createQueryClient()
  queryClient.setDefaultOptions({
    queries: { ...queryClient.getDefaultOptions().queries, retry: false },
    mutations: { retry: false },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ResumeButton halt={halt} />
    </QueryClientProvider>,
  )
}

/** Open the form and type a password into it. */
function fillIn(password: string) {
  fireEvent.click(screen.getByRole('button', { name: /resume/i }))
  fireEvent.change(screen.getByLabelText(/account password/i), { target: { value: password } })
}

describe('the resume control', () => {
  it('asks for a password before it will do anything', () => {
    // The asymmetry, as a screen. `KillSwitchButton` posts on the first click;
    // this one cannot, and the test exists because "make it one click like the
    // halt button" is a plausible-sounding simplification (ADR 0009).
    const fetchMock = stubResume({ status: 200 })
    renderButton()

    fireEvent.click(screen.getByRole('button', { name: /resume/i }))

    expect(screen.getByLabelText(/account password/i)).toBeTruthy()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('will not submit an empty password', () => {
    stubResume({ status: 200 })
    renderButton()
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))

    const submit = screen.getByRole('button', { name: /^RESUME$/ }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
  })

  it('sends the password in the body, never the URL', async () => {
    // A query string is written to nginx's access log verbatim, which would put
    // the account password in a file nobody thinks of as a secret store.
    const fetchMock = stubResume({ status: 200, body: { was_halted: true } })
    renderButton()
    fillIn('a-perfectly-ordinary-password')

    fireEvent.click(screen.getByRole('button', { name: /^RESUME$/ }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [url, init] = fetchMock.mock.calls[0]!
    expect(String(url)).not.toContain('password')
    expect(JSON.parse(String(init?.body)).password).toBe('a-perfectly-ordinary-password')
  })

  it('clears the halt it is sitting on, not whatever is halted', async () => {
    // The property with no counterpart on the halt side. Two halts on screen
    // means two of these, and each has to name its own key.
    const fetchMock = stubResume({ status: 200, body: { was_halted: true } })
    renderButton(SYMBOL_HALT)
    fillIn('pw')

    fireEvent.click(screen.getByRole('button', { name: /^RESUME$/ }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const body = JSON.parse(String(fetchMock.mock.calls[0]![1]?.body))
    expect(body.scope).toBe('symbol')
    expect(body.target).toBe('SPY')
  })

  it('shows the server’s reason when trading was not resumed', async () => {
    // The inverse of the halt button's error case, and the words are the point:
    // this failure left the platform stopped, which is safe, and a reader who
    // cannot tell that from a red box will re-clear into a state nobody
    // understands.
    stubResume({ status: 503, body: { detail: NOT_RESUMED } })
    renderButton()
    fillIn('pw')

    fireEvent.click(screen.getByRole('button', { name: /^RESUME$/ }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/NOT resumed/)
    expect(alert.textContent).toMatch(/still in force/)
    expect(alert.textContent).toMatch(/failed safe/)
  })

  it('keeps the form open and the password typed after a wrong password', async () => {
    // A 403 here is overwhelmingly a typo. Closing the form, or blanking the
    // field, makes the operator retype the whole thing to fix one character —
    // during an incident, which is the only time this screen is ever used.
    stubResume({ status: 403, body: { detail: 'password required for this action' } })
    renderButton()
    fillIn('wrong')

    fireEvent.click(screen.getByRole('button', { name: /^RESUME$/ }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/password required/)
    const field = screen.getByLabelText(/account password/i) as HTMLInputElement
    expect(field.value).toBe('wrong')
  })

  it('drops the password once the resume succeeds', async () => {
    const fetchMock = stubResume({ status: 200, body: { was_halted: true } })
    renderButton()
    fillIn('a-perfectly-ordinary-password')

    fireEvent.click(screen.getByRole('button', { name: /^RESUME$/ }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    // Back to the closed state, which is the only place the field is not
    // holding the password any more.
    await waitFor(() => expect(screen.queryByLabelText(/account password/i)).toBeNull())
  })

  it('reports a failure that never reached the API', async () => {
    stubResume({ status: 502 })
    renderButton()
    fillIn('pw')

    fireEvent.click(screen.getByRole('button', { name: /^RESUME$/ }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent?.trim()).not.toBe('')
  })

  it('masks what is typed', () => {
    // Someone is reading this screen over the operator's shoulder during an
    // incident far more often than at any other time.
    renderButton()
    fireEvent.click(screen.getByRole('button', { name: /resume/i }))

    expect((screen.getByLabelText(/account password/i) as HTMLInputElement).type).toBe('password')
  })
})
