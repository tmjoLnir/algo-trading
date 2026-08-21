/**
 * The strategies query.
 *
 * One request for both halves — the `strategies` table and the code's registry
 * — because the answer a reader wants is the *difference*: a class that exists
 * and has never been loaded by a worker. Two queries would leave this hook
 * diffing two lists that arrived at two instants, for a fact the server can
 * state in one.
 *
 * Not polled. A strategy row changes when a worker boots, which is not
 * something that happens while somebody watches this screen; the client's
 * default refetch-on-focus covers coming back to the tab.
 */

import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/api/client'
import type { StrategiesResponse, StrategyState } from '@/api/types'

/**
 * The rungs of the promotion ratchet, in ratchet order.
 *
 * **A `Record` over the generated union rather than a hand-written array, and
 * that is the whole point of it.** This list used to be written out by hand and
 * had drifted from the server in both directions at once: it offered `backtest`
 * and `active` — neither of which `StrategyState` has ever contained — and
 * omitted `live` and `halted` entirely. Four of its five options could not
 * match a row by construction, and the fifth matched only because the
 * repository was writing a state that was not a member either.
 *
 * Typed this way, a rung added to the server's enum fails `tsc` here until
 * somebody gives it a label, and a label for something that is not a rung fails
 * too. The drift becomes a build error instead of an empty filter nobody
 * questions.
 *
 * Only `draft` is reachable today: `ensure` writes it on a first boot and the
 * write endpoints that would move a strategy up the ratchet are all stubs. That
 * is a real state of affairs rather than a gap in this list — see the page for
 * why `state` is not "is it running now".
 */
const STATE_LABEL: Record<StrategyState, string> = {
  draft: 'Draft',
  backtesting: 'Backtesting',
  paper: 'Paper',
  live: 'Live',
  paused: 'Paused',
  halted: 'Halted',
}

/** The ratchet's rungs in order, behind an "everything" option. */
export const STRATEGY_STATES: readonly { value: StrategyState | ''; label: string }[] = [
  { value: '', label: 'Every state' },
  ...(Object.keys(STATE_LABEL) as StrategyState[]).map((value) => ({
    value,
    label: STATE_LABEL[value],
  })),
]

export function useStrategies(state: string) {
  return useQuery<StrategiesResponse>({
    queryKey: ['strategies', state],
    queryFn: () => {
      const params = new URLSearchParams()
      if (state) params.set('state', state)
      const query = params.toString()
      return apiGet<StrategiesResponse>(`/api/v1/strategies${query ? `?${query}` : ''}`)
    },
  })
}
