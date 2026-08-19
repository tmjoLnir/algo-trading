/**
 * Analytics — requirement #6: what did each strategy actually do, and was it
 * worth running?
 *
 * The endpoints behind this screen have existed and been tested since #58 with
 * nothing reading them; docs/ANALYTICS.md listed "No UI" under *Not built yet*.
 * This is that half. Everything here is a fold over stored fills — no new
 * server capability was added to put it on screen.
 *
 * **This is history, and the dashboard is the book.** The distinction runs
 * through the whole screen and is worth holding onto while reading it: the
 * dashboard answers *what do we hold right now*, from a snapshot the worker
 * published at one instant (ADR 0007). This answers *what happened*, over a
 * period that has finished, by reconstructing round trips from the orders
 * table on request (ADR 0015). An open position appears on the dashboard and
 * nowhere here — it has no exit, so its P&L is not realised, its holding period
 * has no end, and its exit reason does not exist yet.
 *
 * **Each panel fails on its own.** Attribution failing must not take the trade
 * list with it: they are three independent reads of the same stored history,
 * and a reader who can still see their trades is better served than one looking
 * at an error page. The same instinct as the dashboard keeping its last good
 * book on screen, applied to a screen with no single source.
 */

import { useState } from 'react'
import { ApiError } from '@/api/client'
import PerformancePanel from '@/components/PerformancePanel'
import AttributionTable from '@/components/AttributionTable'
import TradesTable from '@/components/TradesTable'
import {
  ALL_TIME_START,
  ATTRIBUTION_DIMENSIONS,
  type AnalyticsWindow,
  type AttributionDimension,
  lastDays,
  toIsoDate,
  useAttribution,
  usePerformance,
  useTrades,
} from '@/hooks/useAnalytics'

/** How many round trips the table asks for. The server caps at 1000. */
const TRADE_LIMIT = 200

const PRESETS: { label: string; days: number | 'all' }[] = [
  { label: '7d', days: 7 },
  { label: '30d', days: 30 },
  { label: '90d', days: 90 },
  { label: '1y', days: 365 },
  { label: 'All', days: 'all' },
]

/**
 * What went wrong, in a sentence a reader can act on.
 *
 * The server's own `detail` is shown rather than a generic message, and the
 * attribution endpoint is why. It answers an unknown dimension with a 422
 * naming the ones that exist, rather than with an empty list, precisely so that
 * "you asked for something that does not exist" cannot read as "this period
 * made nothing" — and that only reaches the reader if the text survives the
 * trip to the screen.
 */
function PanelError({ error, what }: { error: unknown; what: string }) {
  const api = error instanceof ApiError ? error : null
  return (
    <p className="px-4 py-6 text-center text-sm text-amber-400">
      Could not load {what}.
      <span className="mt-1 block text-xs text-amber-200/70">
        {api ? api.detail : String(error)}
      </span>
    </p>
  )
}

function Panel({
  title,
  children,
  control,
}: {
  title: string
  children: React.ReactNode
  control?: React.ReactNode
}) {
  return (
    <section className="rounded border border-slate-800 bg-slate-900/20">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-300">{title}</h2>
        {control}
      </div>
      {children}
    </section>
  )
}

export default function Analytics() {
  const [period, setPeriod] = useState<AnalyticsWindow>(() => lastDays(30))
  const [by, setBy] = useState<AttributionDimension>('exit_reason')
  // Held separately from the window so typing a strategy name does not refetch
  // on every keystroke — three reconstructions of the whole order history per
  // character is not a cost worth paying for live filtering.
  const [strategyDraft, setStrategyDraft] = useState('')

  const performance = usePerformance(period)
  const trades = useTrades(period, TRADE_LIMIT)
  const attribution = useAttribution(period, by)

  const applyPreset = (days: number | 'all') => {
    setPeriod((current) =>
      days === 'all'
        ? { ...current, start: ALL_TIME_START, end: toIsoDate(new Date()) }
        : { ...lastDays(days), strategyId: current.strategyId },
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label
              className="block text-xs uppercase tracking-wide text-slate-500"
              htmlFor="window-start"
            >
              From
            </label>
            <input
              id="window-start"
              type="date"
              value={period.start}
              onChange={(event) => setPeriod((w) => ({ ...w, start: event.target.value }))}
              className="mt-1 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-300"
            />
          </div>
          <div>
            {/* Inclusive, matching the server: `end=2026-08-19` means through
                the nineteenth. A range that dropped the last day would leave
                today's trades out of every report asked for today. */}
            <label
              className="block text-xs uppercase tracking-wide text-slate-500"
              htmlFor="window-end"
            >
              To (inclusive)
            </label>
            <input
              id="window-end"
              type="date"
              value={period.end}
              onChange={(event) => setPeriod((w) => ({ ...w, end: event.target.value }))}
              className="mt-1 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-300"
            />
          </div>

          <div className="flex gap-1">
            {PRESETS.map((preset) => (
              <button
                key={preset.label}
                type="button"
                onClick={() => applyPreset(preset.days)}
                className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:text-slate-200"
              >
                {preset.label}
              </button>
            ))}
          </div>
        </div>

        <form
          className="flex items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            setPeriod((w) => ({ ...w, strategyId: strategyDraft.trim() || null }))
          }}
        >
          <div>
            {/* Typed rather than picked from a list: `/strategies` is still a
                stub, so there is no directory of them to read. The id is the
                strategy's *name*, which is what `Signal.strategy_id` carries
                everywhere in the platform — the attribution table below, grouped
                by strategy, is where to read the ones that traded. */}
            <label
              className="block text-xs uppercase tracking-wide text-slate-500"
              htmlFor="strategy-filter"
            >
              Strategy
            </label>
            <input
              id="strategy-filter"
              type="text"
              value={strategyDraft}
              placeholder="all strategies"
              onChange={(event) => setStrategyDraft(event.target.value)}
              className="mt-1 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-300"
            />
          </div>
          <button
            type="submit"
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:text-slate-200"
          >
            Apply
          </button>
        </form>
      </div>

      {period.strategyId ? (
        <p className="text-xs text-slate-500">
          Filtered to <span className="text-slate-300">{period.strategyId}</span>. Attribution below
          is over every strategy regardless — the endpoint groups the whole period, and a
          single-strategy grouping by strategy would be one row saying nothing.
        </p>
      ) : null}

      {performance.isLoading ? (
        <p className="p-8 text-center text-sm text-slate-500">Loading…</p>
      ) : performance.error ? (
        <PanelError error={performance.error} what="the metric set" />
      ) : performance.data ? (
        <PerformancePanel data={performance.data} />
      ) : null}

      <Panel
        title="Attribution"
        control={
          <div className="flex items-center gap-2">
            <label className="sr-only" htmlFor="attribution-by">
              Group by
            </label>
            <select
              id="attribution-by"
              value={by}
              onChange={(event) => setBy(event.target.value as AttributionDimension)}
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-300"
            >
              {ATTRIBUTION_DIMENSIONS.map((dimension) => (
                <option key={dimension.value} value={dimension.value}>
                  {dimension.label}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {attribution.isLoading ? (
          <p className="px-4 py-6 text-center text-sm text-slate-500">Loading…</p>
        ) : attribution.error ? (
          <PanelError error={attribution.error} what="the attribution breakdown" />
        ) : attribution.data ? (
          <AttributionTable data={attribution.data} />
        ) : null}
      </Panel>

      <Panel title="Closed trades">
        {trades.isLoading ? (
          <p className="px-4 py-6 text-center text-sm text-slate-500">Loading…</p>
        ) : trades.error ? (
          <PanelError error={trades.error} what="the trade list" />
        ) : trades.data ? (
          <TradesTable data={trades.data} />
        ) : null}
      </Panel>
    </div>
  )
}
