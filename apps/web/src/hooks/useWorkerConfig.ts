/**
 * The worker's trading configuration — reading it, and saving it.
 *
 * One query, not four. The saved row, the running worker's report and the
 * option catalogue arrive together for the reason the dashboard's aggregate
 * does: assembled from three requests they could disagree about which
 * strategies exist, and the reader could not tell which answer to trust.
 *
 * Not polled. This changes when somebody saves it — which is this hook — and
 * when a worker restarts, which is not on a cadence worth chasing from a
 * browser. The client's refetch-on-focus covers coming back to the tab, which
 * is exactly the moment after a `docker compose restart worker`.
 *
 * The mutation deliberately does **not** optimistically update. A save can be
 * refused by the server's own validation or by a wrong password, and a form
 * that had already redrawn itself as saved would be telling the operator their
 * stop multiplier changed when it did not.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPut } from '@/api/client'
import type { RiskLimitsInput, WorkerConfigScreen } from '@/api/types'

const KEY = ['worker', 'config']

export function useWorkerConfig() {
  return useQuery<WorkerConfigScreen>({
    queryKey: KEY,
    queryFn: () => apiGet<WorkerConfigScreen>('/api/v1/worker/config'),
  })
}

/** What the form sends. Every field, always — see `WorkerConfigUpdate`. */
export interface WorkerConfigSave {
  symbols: string[]
  max_silence_seconds: number
  strategy: string
  strategy_params: Record<string, unknown>
  timeframe: string
  sizing_method: string
  sizing_value: string
  stop_type: string
  stop_multiplier: string
  stop_period: number
  allow_live_orders: boolean
  /**
   * The account-wide ceilings, saved in the same request.
   *
   * One save rather than two, because an operator who widens a stop and lifts a
   * position limit in one sitting made one decision — and one request means one
   * revision, one audit entry, and one "your worker is older than this" notice
   * covering all of it.
   */
  risk: RiskLimitsInput
  password?: string
}

export function useSaveWorkerConfig() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: WorkerConfigSave) =>
      apiPut<WorkerConfigScreen>('/api/v1/worker/config', body),
    onSuccess: (screen) => {
      // The response *is* the new screen — saved row, running report and
      // options — so it is written straight into the cache rather than
      // invalidated and re-fetched. One round trip, and no window in which the
      // form renders the old revision beside a success message.
      qc.setQueryData(KEY, screen)
      // The risk queries are a different matter: `/risk/status` and
      // `/risk/limits` feed the panel on Strategies, and this save is the only
      // thing in the application that changes what they return. Without this
      // they keep serving the pre-save ceilings until something else refetches
      // them, so an operator who tightens a limit here and checks it there is
      // shown the number they just replaced.
      void qc.invalidateQueries({ queryKey: ['risk'] })
    },
  })
}
