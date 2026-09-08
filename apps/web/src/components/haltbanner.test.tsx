import { QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createQueryClient } from '../api/queryClient'
import HaltBanner from './HaltBanner'
import type { HaltView } from '../api/types'

/**
 * The halt banner, and the two things it used to leave off.
 *
 * docs/SAFETY.md calls this screen the place you halt from, so it is also the
 * place an operator looks to find out what a halt means. Two fields decide that
 * and neither was rendered before ADR 0029:
 *
 * `reason` is the reason *in force*, while `engaged_by`, `engaged_at` and
 * `detail` describe the halt's origin — not the same incident once a halt has
 * escalated. Rendering the four alone produced a banner reading
 * `reconciliation_mismatch` beside `ops` and "pausing for lunch": three true
 * fields composing one false sentence.
 *
 * `unproven_symbols` changes what the operator can *do*. The platform refuses
 * to close those, protective stops included, so a banner without them cannot
 * say why a flatten came back refused.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const PLAIN: HaltView = {
  scope: 'global',
  reason: 'manual',
  detail: 'pausing for lunch',
  engaged_at: '2026-08-18T12:00:00Z',
  engaged_by: 'ops',
  target: null,
}

function renderWith(halts: HaltView[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        ({
          ok: true,
          status: 200,
          statusText: 'stub',
          json: async () => ({
            as_of: '2026-08-18T12:05:00Z',
            run_mode: 'paper',
            market_open: true,
            active_halts: halts,
            stale_after_seconds: 30,
          }),
        }) as Response,
    ),
  )
  const client = createQueryClient()
  return render(
    <QueryClientProvider client={client}>
      <HaltBanner />
    </QueryClientProvider>,
  )
}

describe('what a halt says it cannot prove', () => {
  it('names the symbols and what that means for closing them', async () => {
    renderWith([{ ...PLAIN, reason: 'reconciliation_mismatch', unproven_symbols: ['QQQ', 'SPY'] }])

    expect(await screen.findByText(/QQQ, SPY/)).toBeTruthy()
    expect(screen.getByText(/will not close these/i)).toBeTruthy()
    expect(screen.getByText(/broker's own UI/i)).toBeTruthy()
  })

  it('says the rest of the book is unaffected', async () => {
    // Without this the operator reads a symbol list as a full freeze on exits,
    // which is the failure the per-symbol design exists to avoid.
    renderWith([{ ...PLAIN, unproven_symbols: ['SPY'] }])

    expect(await screen.findByText(/every other position still closes/i)).toBeTruthy()
  })

  it('says nothing extra for the overwhelming majority of halts', async () => {
    renderWith([PLAIN])

    expect(await screen.findByText(/manual/)).toBeTruthy()
    expect(screen.queryByText(/cannot prove/i)).toBeNull()
  })

  it('tolerates the field being absent on the wire', async () => {
    // It has a server-side default, so it is optional in the generated schema —
    // and a banner that threw here would take the whole screen down at the
    // moment it matters most.
    renderWith([PLAIN])

    expect(await screen.findByText(/ALL TRADING HALTED/)).toBeTruthy()
  })
})

describe('an escalated halt is not three fields from two incidents', () => {
  it('says where the reason came from', async () => {
    renderWith([
      {
        ...PLAIN,
        reason: 'reconciliation_mismatch',
        escalated_from: 'manual',
        escalated_by: 'reconciler',
        unproven_symbols: ['SPY'],
      },
    ])

    expect(await screen.findByText(/escalated from manual by reconciler/)).toBeTruthy()
    // …while the origin still reads as the origin. That is the latch.
    expect(screen.getByText(/by ops at/)).toBeTruthy()
  })

  it('stays quiet when the reason never moved', async () => {
    renderWith([PLAIN])

    expect(await screen.findByText(/manual/)).toBeTruthy()
    expect(screen.queryByText(/escalated from/)).toBeNull()
  })
})
