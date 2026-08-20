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
import type { StrategiesResponse } from '@/api/types'

/**
 * Lifecycle states the filter offers.
 *
 * `active` is the only one a worker ever writes, and it writes it once — see
 * the page for why that is not "running now". The others exist in the schema
 * and are reachable only once something can edit a strategy, which is a write
 * and is not built.
 */
export const STRATEGY_STATES = [
  { value: '', label: 'Every state' },
  { value: 'draft', label: 'Draft' },
  { value: 'backtest', label: 'Backtest' },
  { value: 'paper', label: 'Paper' },
  { value: 'active', label: 'Active' },
  { value: 'paused', label: 'Paused' },
] as const

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
