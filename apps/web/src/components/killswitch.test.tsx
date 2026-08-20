import { QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '../api/queryClient'
import KillSwitchButton from './KillSwitchButton'

/**
 * The emergency stop, from the side a person sees.
 *
 * The button has exactly one job and two ways to lie about having done it. It
 * can look like it stopped trading when the halt was never written, and it can
 * look like nothing happened when in fact the switch failed closed and nothing
 * is trading right now. Both leave an operator acting on a false picture during
 * the minute they can least afford one, so the failure path gets more tests
 * here than the happy one — pressing the button and having it work is a single
 * POST.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** The 503 the API answers when the switch could not be written. */
const NOT_RECORDED =
  'the halt was NOT recorded: Connection refused. Orders are being refused for as ' +
  'long as the store is unreachable, because the switch fails closed — but nothing ' +
  'was written, so trading resumes on its own when it recovers.'

function stubHalt(route: { status: number; body?: unknown }) {
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

function renderButton(halted = false) {
  const queryClient = createQueryClient()
  queryClient.setDefaultOptions({
    queries: { ...queryClient.getDefaultOptions().queries, retry: false },
    mutations: { retry: false },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <KillSwitchButton halted={halted} />
    </QueryClientProvider>,
  )
}

describe('the kill switch button', () => {
  it('posts a global halt when pressed', async () => {
    const fetchMock = stubHalt({ status: 200, body: { scope: 'global' } })
    renderButton()

    fireEvent.click(screen.getByRole('button', { name: /halt trading/i }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [url, init] = fetchMock.mock.calls[0]!
    expect(String(url)).toContain('/api/v1/risk/halt')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({ scope: 'global', reason: 'manual' })
  })

  it('asks for no confirmation', async () => {
    // Stated as its own test because it is the kind of thing a later reviewer
    // "fixes" by adding an `Are you sure?`. Hesitation is the expensive part
    // (docs/RISK.md); the halt is the cheap, reversible half.
    const fetchMock = stubHalt({ status: 200 })
    renderButton()

    fireEvent.click(screen.getByRole('button', { name: /halt trading/i }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  })

  it('shows the server’s reason when the halt was not recorded', async () => {
    // The whole point of the error branch: this message says trading is
    // stopped *now* but will resume on its own, which no status code and no
    // generic wording can convey.
    stubHalt({ status: 503, body: { detail: NOT_RECORDED } })
    renderButton()

    fireEvent.click(screen.getByRole('button', { name: /halt trading/i }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/NOT recorded/)
    expect(alert.textContent).toMatch(/fails closed/)
    expect(alert.textContent).toMatch(/resumes on its own/)
  })

  it('does not silently return to its resting state after a failure', async () => {
    // A button that just goes back to saying HALT TRADING reads as "I stopped
    // it". The label is allowed to return — the alert beside it is what must
    // not disappear.
    stubHalt({ status: 503, body: { detail: NOT_RECORDED } })
    renderButton()

    fireEvent.click(screen.getByRole('button', { name: /halt trading/i }))

    await screen.findByRole('alert')
    const button = screen.getByRole('button', { name: /halt trading/i }) as HTMLButtonElement
    expect(button.disabled).toBe(false)
  })

  it('reports a failure that never reached the API', async () => {
    // No JSON body at all — the dev server's proxy or nginx answering for an
    // API it could not reach. `client.ts` turns that into a sentence about a
    // machine; the button must render whatever it produced rather than an
    // empty alert.
    stubHalt({ status: 502 })
    renderButton()

    fireEvent.click(screen.getByRole('button', { name: /halt trading/i }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent?.trim()).not.toBe('')
  })

  it('shows the halted state instead of the button once trading is stopped', () => {
    renderButton(true)

    expect(screen.getByText(/trading halted/i)).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
  })
})
