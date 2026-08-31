/**
 * The analytics queries — history, not the live book.
 *
 * **Why three requests here when the dashboard insists on one.**
 * docs/DASHBOARD.md refuses to assemble the live screen from six fetches,
 * because a P&L computed at one instant beside a price fetched at another
 * simply disagree and the reader cannot tell which to trust. Nothing like that
 * applies to a closed period: a round trip that finished last Tuesday has
 * finished, and its P&L is the same number in all three responses. The
 * endpoints themselves are built on that reasoning (ADR 0015 against ADR 0007),
 * and this is the client half of it.
 *
 * The window is therefore sent **explicitly** by all three rather than left to
 * the server's default, so the three panels are demonstrably describing the
 * same period rather than three periods that happen to coincide. One residual
 * is worth naming instead of hiding: a window whose end is *today* is still
 * open, so a round trip closing in the milliseconds between two of these
 * requests lands in one and not the other. It corrects itself on the next
 * refresh, and it is the reason the period is stated on screen.
 *
 * These are not polled, and nothing in the app is on a cadence any more
 * (ADR 0022) — but the reason these never were is stronger than the reason the
 * dashboard stopped: a report over a finished period cannot change, and re-running
 * it would repeat a reconstruction that reads the whole order history
 * (docs/ANALYTICS.md, "The read, and what it costs") to produce an identical
 * answer.
 */

import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/api/client'
import type { AttributionResponse, PerformanceResponse, TradesResponse } from '@/api/types'

/** The dimensions `/analytics/attribution` groups by. An unknown one is a 422. */
export const ATTRIBUTION_DIMENSIONS = [
  { value: 'strategy', label: 'Strategy' },
  { value: 'symbol', label: 'Symbol' },
  { value: 'exit_reason', label: 'Exit reason' },
  { value: 'weekday', label: 'Day of week' },
  { value: 'hour', label: 'Hour of day' },
] as const

export type AttributionDimension = (typeof ATTRIBUTION_DIMENSIONS)[number]['value']

/**
 * A period, as two inclusive dates.
 *
 * Dates rather than instants because that is what the endpoints take, and
 * because "the 19th" is the period a person asks for. The server reads `end` as
 * inclusive — through the nineteenth, not up to midnight on it.
 */
export interface AnalyticsWindow {
  start: string
  end: string
  /** Narrow to one strategy, or every one of them. */
  strategyId: string | null
}

/**
 * The earliest date "all time" reaches back to.
 *
 * A literal rather than an omitted parameter, because omitting `start` does not
 * mean "everything" — the server defaults it to thirty days before the end,
 * which is the opposite of what this preset says. No market data in this
 * platform predates it.
 */
export const ALL_TIME_START = '1970-01-01'

/** `YYYY-MM-DD` for a date, in UTC — the timezone every stored instant is in. */
export function toIsoDate(at: Date): string {
  return at.toISOString().slice(0, 10)
}

/** A window ending today and reaching `days` back. */
export function lastDays(days: number, today: Date = new Date()): AnalyticsWindow {
  const start = new Date(today)
  start.setUTCDate(start.getUTCDate() - days)
  return { start: toIsoDate(start), end: toIsoDate(today), strategyId: null }
}

function windowParams(period: AnalyticsWindow): URLSearchParams {
  const params = new URLSearchParams({ start: period.start, end: period.end })
  if (period.strategyId) params.set('strategy_id', period.strategyId)
  return params
}

/** Part of a query key, so a changed window refetches and a stable one does not. */
function windowKey(period: AnalyticsWindow): string[] {
  return [period.start, period.end, period.strategyId ?? '']
}

/** The full metric set over the period. */
export function usePerformance(period: AnalyticsWindow) {
  return useQuery<PerformanceResponse>({
    queryKey: ['analytics', 'performance', ...windowKey(period)],
    queryFn: () =>
      apiGet<PerformanceResponse>(`/api/v1/analytics/performance?${windowParams(period)}`),
  })
}

/**
 * Completed round trips, newest first.
 *
 * `limit` is the server's cap on how many come back, and it is applied *before*
 * excursions are measured — so a smaller number is a cheaper request rather
 * than the same request truncated.
 */
export function useTrades(period: AnalyticsWindow, limit = 200) {
  return useQuery<TradesResponse>({
    queryKey: ['analytics', 'trades', ...windowKey(period), limit],
    queryFn: () => {
      const params = windowParams(period)
      params.set('limit', String(limit))
      return apiGet<TradesResponse>(`/api/v1/analytics/trades?${params}`)
    },
  })
}

/**
 * P&L grouped by one dimension.
 *
 * `strategy_id` is deliberately not forwarded: the endpoint does not take one —
 * it attributes over everything in the window — and grouping a single-strategy
 * filter by strategy would produce a one-row table that says nothing.
 */
export function useAttribution(period: AnalyticsWindow, by: AttributionDimension) {
  return useQuery<AttributionResponse>({
    queryKey: ['analytics', 'attribution', by, period.start, period.end],
    queryFn: () => {
      const params = new URLSearchParams({ start: period.start, end: period.end, by })
      return apiGet<AttributionResponse>(`/api/v1/analytics/attribution?${params}`)
    },
  })
}
