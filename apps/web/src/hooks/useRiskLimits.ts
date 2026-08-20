/**
 * The risk limits, and where the book stands against them.
 *
 * Two queries rather than one, and the second is the interesting half.
 *
 * `/risk/status` is the primary and normally the only request: it carries each
 * limit's ceiling *and* the current reading, so nothing else is needed while it
 * answers. `/risk/limits` is fetched **only when `/status` has failed**, which
 * is exactly the case that route exists for — it reads config and touches no
 * store, so it still answers when the book behind `/status` is unreachable.
 * The panel then degrades from "here is where you stand" to "here are the
 * ceilings, and we cannot currently see the book", which is a materially
 * different sentence from an empty screen.
 *
 * Not polled. Limits change when someone edits `.env` and restarts, and the
 * book behind the readings moves on the worker's evaluation interval rather
 * than on a cadence worth chasing from here — the Dashboard is the screen for
 * watching something move. The client's refetch-on-focus covers returning to
 * the tab.
 */

import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/api/client'
import type { RiskLimitsView, RiskStatusView } from '@/api/types'

export function useRiskStatus() {
  return useQuery<RiskStatusView>({
    queryKey: ['risk', 'status'],
    queryFn: () => apiGet<RiskStatusView>('/api/v1/risk/status'),
    // One attempt. A 503 here means the book could not be read, and the
    // fallback below is a better answer than three more tries at the same
    // unreachable store.
    retry: false,
  })
}

export function useRiskLimits(enabled: boolean) {
  return useQuery<RiskLimitsView>({
    queryKey: ['risk', 'limits'],
    queryFn: () => apiGet<RiskLimitsView>('/api/v1/risk/limits'),
    enabled,
    // Config, fixed for the life of the process. Refetching it would be asking
    // a question whose answer cannot have changed.
    staleTime: Infinity,
  })
}
