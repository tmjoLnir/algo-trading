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

/** One row of a strategy picker: something a backtest can name. */
export interface StrategyChoice {
  /** What `POST /backtests` receives as `strategy_id`. */
  id: string
  name: string
  /** The Python class, or null for a stored rule set — there is no class. */
  className: string | null
  /**
   * Whether a `strategies` row exists for it.
   *
   * False means the code registers the class and nothing has ever loaded it,
   * which is the ordinary state of every strategy nobody has pointed a worker
   * at. It is offered anyway: `POST /backtests` writes the row when it queues
   * the first run, so the only thing this flag changes is what the form says
   * about the strategy — not whether it can be run.
   */
  stored: boolean
}

/**
 * Every strategy this platform can backtest, from both halves of the response.
 *
 * **The two halves are one list here and two lists on the Strategies tab**, and
 * the difference is what each screen is for. That screen exists to show the
 * *gap* — a class the code has that no worker has loaded. This one has to name
 * something to run, and a registered class is runnable whether or not anybody
 * has run it before, so hiding the never-run half made the picker a list of
 * accidents: whichever strategies had happened to go through a worker or the
 * seed script. On most installs, one.
 *
 * Keyed on the **trimmed** id, which is what the form sends and what the server
 * resolves against — a stored row carries whatever `Strategy.name` the worker
 * booted with, so it can arrive padded, and keying on the raw value would offer
 * `sma_crossover` twice: once from the row and once from the registry.
 *
 * A stored row wins over the registry entry of the same name, because it is the
 * fuller answer: it carries the id the run must point at, and for a rule set
 * there is no registry entry at all.
 *
 * Sorted by name. The registry is a dict and the table is ordered by creation,
 * so an unsorted union would move rows around between reads for no reason a
 * reader could see.
 */
export function strategyChoices(data: StrategiesResponse | undefined): StrategyChoice[] {
  const byId = new Map<string, StrategyChoice>()

  for (const row of data?.strategies ?? []) {
    byId.set(row.id.trim(), {
      id: row.id,
      name: row.name,
      className: row.class_name,
      stored: true,
    })
  }

  for (const entry of data?.available ?? []) {
    if (byId.has(entry.name.trim())) continue
    byId.set(entry.name.trim(), {
      id: entry.name,
      name: entry.name,
      className: entry.class_name,
      stored: false,
    })
  }

  return [...byId.values()].sort((a, b) => a.name.localeCompare(b.name))
}

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
