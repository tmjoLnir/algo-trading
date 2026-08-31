/**
 * The backtest queries, and the one mutation in this app.
 *
 * **Polling is conditional on there being something to poll for**, and this is
 * now the only polling left in the app. Since ADR 0022 every other screen is
 * read when its reader asks. This one is different, and the difference is why it
 * survived: a queued run changes state within seconds and then never again, with
 * nobody watching the screen at the moment it does. So the interval is derived
 * from the data rather than configured — while any run is `queued` or `running`
 * the list refetches every few seconds; once they are all terminal it stops
 * entirely, and a tab left open on a page of finished backtests makes no
 * requests at all.
 *
 * The principle is the one ADR 0022 applied everywhere else, pointed at a
 * different axis: do not ask a question whose answer cannot have changed.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost } from '@/api/client'
import { buildRunExport, hasResultBody, runExportFilename } from '@/lib/backtestExport'
import { saveJson } from '@/lib/download'
import type {
  BacktestComparisonResponse,
  BacktestEquityCurveResponse,
  BacktestListResponse,
  BacktestOut,
  BacktestRequest,
  BacktestTrade,
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

/**
 * How every entry is protected, matching `atp_core.domain.StopType`.
 *
 * The empty option is first and is the current behaviour of every stored run:
 * arm only what the strategy itself emits. It is offered rather than removed
 * because a run's protection is part of what it was, and defaulting one on
 * would change what a re-run of an old spec reports.
 *
 * `unit` says what the value means, because it changes per type — a multiple of
 * ATR and a fraction of price are both "2" to a text input.
 */
export const STOP_TYPES = [
  { value: '', label: 'None — only what the strategy emits', unit: '' },
  { value: 'atr', label: 'ATR (docs/RISK.md default)', unit: 'multiple of ATR, e.g. 2' },
  { value: 'chandelier', label: 'Chandelier — trailing, off ATR', unit: 'multiple of ATR, e.g. 3' },
  { value: 'fixed_pct', label: 'Fixed percent — has a target too', unit: 'fraction, e.g. 0.03' },
  { value: 'fixed_amount', label: 'Fixed amount — has a target too', unit: 'price distance' },
  { value: 'trailing_pct', label: 'Trailing percent', unit: 'fraction, e.g. 0.05' },
  { value: 'time', label: 'Time — exits after n bars', unit: 'bars to hold' },
] as const

/** The two whose value is a multiple of ATR, so they also need a period. */
export const ATR_STOP_TYPES: ReadonlySet<string> = new Set(['atr', 'chandelier'])

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
 * The two heavy reads, as query descriptors rather than inline in a hook.
 *
 * Named here because two callers want the same request under the same key: the
 * detail panel renders them, and the row's export writes them to a file. Sharing
 * the key is the point — a download clicked while the panel is loading joins
 * that request instead of making a second one, and one clicked just after it
 * reads the cache.
 */
const curveQuery = (runId: string) => ({
  queryKey: ['backtest-curve', runId],
  queryFn: () => apiGet<BacktestEquityCurveResponse>(`/api/v1/backtests/${runId}/equity-curve`),
})

const tradesQuery = (runId: string) => ({
  queryKey: ['backtest-trades', runId],
  queryFn: () => apiGet<BacktestTradesResponse>(`/api/v1/backtests/${runId}/trades`),
})

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
    ...curveQuery(runId ?? ''),
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
    ...tradesQuery(runId ?? ''),
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

/**
 * Write one run to a `.json` file the reader keeps.
 *
 * A `useMutation` although it changes nothing on the server: it is an imperative
 * action with a pending state and a failure worth showing, which is what the
 * hook models, and modelling it as a query would mean a `useEffect` firing on a
 * click (CLAUDE.md §4). Per row, so a slow export of one run does not grey out
 * the button on the others.
 *
 * **Fetched through `fetchQuery`, not `apiGet`.** Same keys as the detail panel,
 * so an open run's curve and trades are reused rather than re-fetched, a click
 * during the panel's own load joins that request, and the copies are released by
 * the cache's normal collection instead of being pinned here — which matters
 * when a minute run's curve is hundreds of thousands of points.
 *
 * **Nothing is fetched for a run without a result.** `hasResultBody` decides
 * from the status: the two endpoints answer a queued, running or failed run with
 * an empty list rather than a 404, so asking would buy two requests and then
 * record `[]` — claiming the run stored an empty result where it stored none.
 *
 * Not gated on write scope, deliberately. Reading a result is not an act, so a
 * read-only session exports exactly like a full one (ADR 0009).
 */
export function useDownloadBacktest() {
  const queryClient = useQueryClient()
  return useMutation<string, Error, BacktestOut>({
    mutationFn: async (run) => {
      const [curve, trades] = hasResultBody(run)
        ? await Promise.all([
            queryClient.fetchQuery(curveQuery(run.id)),
            queryClient.fetchQuery(tradesQuery(run.id)),
          ])
        : [null, null]
      const filename = runExportFilename(run)
      saveJson(
        filename,
        buildRunExport(run, {
          curve: curve?.points ?? null,
          // The server declares these as opaque JSON objects, so this cast is
          // the same reading `BacktestDetail` applies to the same payload —
          // see the note on `BacktestTrade` in `api/types.ts`.
          trades: (trades?.trades as BacktestTrade[] | undefined) ?? null,
          exportedAt: new Date().toISOString(),
        }),
      )
      // The name it wrote. The row reports it, because a browser saving
      // straight to a downloads folder gives no sign the click did anything.
      return filename
    },
  })
}
