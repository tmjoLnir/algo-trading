/**
 * The backtest queries, and the one mutation in this app.
 *
 * **Polling is conditional on there being something to poll for.** Every other
 * screen here either polls on a fixed cadence (the live book, every five
 * minutes) or not at all (strategies, orders — rows that change when a worker
 * boots). This one is different: a queued run changes state within seconds and
 * then never again, so the interval is derived from the data rather than
 * configured. While any run is `queued` or `running` the list refetches every
 * few seconds; once they are all terminal it stops entirely, and a tab left open
 * on a page of finished backtests makes no requests at all.
 *
 * That is the same reasoning `useLiveDashboard` applies to a hidden tab, pointed
 * at a different axis: do not ask a question whose answer cannot have changed.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost } from '@/api/client'
import type {
  BacktestComparisonResponse,
  BacktestEquityCurveResponse,
  BacktestListResponse,
  BacktestOut,
  BacktestRequest,
  BacktestTradesResponse,
} from '@/api/types'

/**
 * How often to re-read a list that has a run in flight.
 *
 * Short, because the thing being watched is a progress bar and a five-second
 * bar reads as broken. Affordable for the same reason: the list request costs
 * one indexed query plus one Redis read per in-flight run, and there is at most
 * one of those — the queue worker runs a single job at a time.
 */
export const IN_FLIGHT_POLL_MS = 3000

/** The statuses that mean a run is still going to change. Mirrors `IN_FLIGHT`. */
const IN_FLIGHT: ReadonlySet<string> = new Set(['queued', 'running'])

export function isInFlight(run: Pick<BacktestOut, 'status'>): boolean {
  return IN_FLIGHT.has(run.status)
}

/** Cost models the form offers, and what each is for. */
export const COST_MODELS = [
  { value: 'alpaca_equities', label: 'Alpaca US equities (realistic)' },
  // Offered, and labelled with what it costs you. docs/BACKTESTING.md is
  // unambiguous that a zero-cost result is not evidence about a strategy, so the
  // option says so rather than reading as the faster choice.
  { value: 'zero', label: 'Zero cost — debugging only, not evidence' },
] as const

/**
 * How a quantity is decided, matching `runner.SIZING_METHODS`.
 *
 * `fixed_qty` is first because it is the server's default, not because it is
 * the right answer: docs/RISK.md is unambiguous that real sizing is risk-based,
 * and each label says what the value beside it means — the field changes
 * meaning per method, and a form that left that to be guessed would send a
 * share count where a fraction of equity was wanted.
 */
export const SIZING_METHODS = [
  { value: 'fixed_qty', label: 'Fixed share count', unit: 'shares per entry' },
  { value: 'fixed_notional', label: 'Fixed notional', unit: 'currency per entry' },
  { value: 'equity_pct', label: 'Percent of equity', unit: 'fraction, e.g. 0.05 for 5%' },
  {
    value: 'risk_pct',
    label: 'Risk per trade — needs a stop',
    unit: 'fraction of equity at risk, e.g. 0.01 for 1%',
  },
  {
    value: 'volatility_target',
    label: 'Volatility target — needs a volatility',
    unit: 'target fraction',
  },
] as const

/** Timeframes the engine supports, matching `atp_core.domain.Timeframe`. */
export const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d'] as const

export function useBacktests(strategyId: string) {
  return useQuery<BacktestListResponse>({
    queryKey: ['backtests', strategyId],
    queryFn: () => {
      const params = new URLSearchParams()
      if (strategyId) params.set('strategy_id', strategyId)
      const query = params.toString()
      return apiGet<BacktestListResponse>(`/api/v1/backtests${query ? `?${query}` : ''}`)
    },
    // Derived from what came back, not from a constant. `false` stops the timer
    // rather than slowing it, so a page of finished runs is genuinely idle.
    refetchInterval: (query) =>
      (query.state.data?.runs ?? []).some(isInFlight) ? IN_FLIGHT_POLL_MS : false,
  })
}

/**
 * One run's equity curve.
 *
 * Its own request rather than a field on the run, because it is large — a
 * five-year daily curve is over a thousand points and a minute run is hundreds
 * of thousands — and the list this screen polls must stay small. Fetched only
 * once a run is selected and finished.
 */
export function useBacktestCurve(runId: string | null, enabled: boolean) {
  return useQuery<BacktestEquityCurveResponse>({
    queryKey: ['backtest-curve', runId],
    queryFn: () => apiGet<BacktestEquityCurveResponse>(`/api/v1/backtests/${runId}/equity-curve`),
    enabled: Boolean(runId) && enabled,
  })
}

/**
 * One run's trades.
 *
 * docs/BACKTESTING.md's pre-belief checklist asks whether individual trades were
 * inspected — "no impossible fills" — and this is the only thing in the platform
 * that can answer it. Fetched on selection, for the same size reason as the
 * curve.
 */
export function useBacktestTrades(runId: string | null, enabled: boolean) {
  return useQuery<BacktestTradesResponse>({
    queryKey: ['backtest-trades', runId],
    queryFn: () => apiGet<BacktestTradesResponse>(`/api/v1/backtests/${runId}/trades`),
    enabled: Boolean(runId) && enabled,
  })
}

/**
 * Metrics for several runs side by side.
 *
 * A GET with repeated `run_ids`, and the method is not incidental: comparing
 * reads and changes nothing, so as a POST it would be refused to a read-only
 * session by `require_write_scope` — the very session most likely to be looking
 * (ADR 0009).
 */
export function useBacktestComparison(runIds: string[]) {
  return useQuery<BacktestComparisonResponse>({
    queryKey: ['backtest-compare', [...runIds].sort()],
    queryFn: () => {
      const params = new URLSearchParams()
      for (const id of runIds) params.append('run_ids', id)
      return apiGet<BacktestComparisonResponse>(`/api/v1/backtests/compare?${params.toString()}`)
    },
    enabled: runIds.length >= 2,
  })
}

/**
 * Queue a run.
 *
 * The only mutation on this screen and the only one in this app that is not the
 * kill switch. On success the list is invalidated rather than optimistically
 * updated: the server mints the run id and stamps `queued_at`, and inventing a
 * row here would put a client's guess at both on screen for a few hundred
 * milliseconds.
 */
export function useQueueBacktest() {
  const queryClient = useQueryClient()
  return useMutation<BacktestOut, Error, BacktestRequest>({
    mutationFn: (payload) => apiPost<BacktestOut>('/api/v1/backtests', payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['backtests'] })
    },
  })
}
