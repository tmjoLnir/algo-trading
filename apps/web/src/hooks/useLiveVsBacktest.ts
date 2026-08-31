/**
 * The live-against-backtest query, and the run list that feeds its picker.
 *
 * **Keyed on a run id, not on a window**, which is what makes this hook a
 * different shape from the three in `useAnalytics.ts`. Those describe a period
 * the reader chose; this one asks whether a strategy has held up against what
 * one specific backtest promised, and the strategy is read off that run rather
 * than passed alongside — so the two halves cannot be about different
 * strategies (docs/ANALYTICS.md).
 *
 * **The page's date range is deliberately not forwarded.** The endpoint's live
 * window is open at the start by default, and that default is load-bearing: the
 * denominator for "has this held up" is the whole live record, and quietly
 * sending the last 30 days of a three-month paper run would answer a narrower
 * question in a way nothing in the response distinguishes from the broader one.
 * Passing this screen's window would be the client re-introducing the default
 * the server rejected.
 *
 * Not polled, for the reason the other analytics reads are not: a finished
 * backtest does not change, and a reconstruction that reads the whole order
 * history (docs/ANALYTICS.md, "The read, and what it costs") should not be
 * re-run to produce an identical answer.
 */

import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/api/client'
import type { BacktestListResponse, BacktestOut, LiveVsBacktestResponse } from '@/api/types'

/**
 * Only a completed run can be compared.
 *
 * The endpoint answers 400 for anything else — a queued or failed run has no
 * metrics, and comparing against a column of nulls would report every live
 * metric as an unexplained divergence. Filtering here means the picker cannot
 * offer a choice the server will refuse.
 */
export function isComparable(run: Pick<BacktestOut, 'status' | 'metrics'>): boolean {
  return run.status === 'done' && run.metrics !== null && run.metrics !== undefined
}

/**
 * Every stored run, for the picker.
 *
 * The same endpoint the Backtests tab lists, without its polling: this screen
 * is not watching anything run, it is choosing among things that have finished.
 * `staleTime` is left at the client default so switching tabs re-reads a list
 * that may have gained a run since — which after ADR 0022 is how the rest of the
 * app behaves too.
 */
export function useComparableRuns() {
  return useQuery<BacktestListResponse>({
    queryKey: ['backtests', 'comparable'],
    queryFn: () => apiGet<BacktestListResponse>('/api/v1/backtests'),
  })
}

/**
 * Live against one stored run.
 *
 * `periodsPerYear` pins both sides to one annualisation basis. Null means the
 * server infers the live basis from the equity curve's own spacing, which is
 * the honest default and also the one that makes every annualised metric differ
 * by that factor before the strategy has done anything — hence the warning, and
 * hence the control that answers it.
 */
export function useLiveVsBacktest(runId: string | null, periodsPerYear: number | null) {
  return useQuery<LiveVsBacktestResponse>({
    queryKey: ['analytics', 'live-vs-backtest', runId, periodsPerYear],
    // Nothing to ask until a run is named. The picker starts empty on purpose:
    // this comparison turns on *which* backtest, and defaulting to the newest
    // would be the screen choosing a run nobody approved anything against.
    enabled: runId !== null,
    queryFn: () => {
      const params = new URLSearchParams()
      if (periodsPerYear !== null) params.set('periods_per_year', String(periodsPerYear))
      const query = params.toString()
      return apiGet<LiveVsBacktestResponse>(
        `/api/v1/analytics/live-vs-backtest/${encodeURIComponent(runId as string)}${
          query ? `?${query}` : ''
        }`,
      )
    },
  })
}
