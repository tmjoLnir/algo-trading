/**
 * Orders — the order table on a screen.
 *
 * The dashboard already shows *working* orders, from the book the worker
 * published. This is the other question: what did we send, and what happened to
 * it. The rows that justify the screen are the ones that never filled.
 *
 * **A refused order is invisible everywhere else in this platform.** It moved no
 * quantity, so `filled_orders` excludes it and no reconstructed round trip
 * contains it; the book never held it and the equity curve never moved for it.
 * Before this screen, a strategy whose every order was refused looked — from the
 * dashboard, from analytics, from the equity chart — exactly like a strategy
 * that never placed one. Those two call for opposite responses.
 *
 * The screen says which run mode it is showing, because paper and live orders
 * share a table and a page that did not say would be unreadable on a machine
 * that has run both.
 */

import { useState } from 'react'
import { ApiError } from '@/api/client'
import OrderHistoryTable from '@/components/OrderHistoryTable'
import {
  NO_FILTERS,
  ORDER_STATUSES,
  PAGE_SIZES,
  type OrderFilters,
  useOrders,
} from '@/hooks/useOrders'

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string
  htmlFor: string
  children: React.ReactNode
}) {
  return (
    <div>
      <label className="block text-xs uppercase tracking-wide text-slate-500" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
    </div>
  )
}

const CONTROL = 'mt-1 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-300'

export default function Orders() {
  const [filters, setFilters] = useState<OrderFilters>(NO_FILTERS)
  // The text fields are held apart from the query so typing a symbol does not
  // refetch on every keystroke.
  const [symbolDraft, setSymbolDraft] = useState('')
  const [strategyDraft, setStrategyDraft] = useState('')

  const query = useOrders(filters)
  const orders = query.data?.orders ?? []

  const apply = (event: React.FormEvent) => {
    event.preventDefault()
    setFilters((current) => ({
      ...current,
      symbol: symbolDraft.trim(),
      strategyId: strategyDraft.trim(),
    }))
  }

  return (
    <div className="space-y-4">
      <form className="flex flex-wrap items-end gap-3" onSubmit={apply}>
        <Field label="Status" htmlFor="status-filter">
          <select
            id="status-filter"
            value={filters.status}
            onChange={(event) => setFilters((f) => ({ ...f, status: event.target.value }))}
            className={CONTROL}
          >
            {ORDER_STATUSES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Symbol" htmlFor="symbol-filter">
          <input
            id="symbol-filter"
            type="text"
            value={symbolDraft}
            placeholder="all symbols"
            onChange={(event) => setSymbolDraft(event.target.value)}
            className={CONTROL}
          />
        </Field>

        <Field label="Strategy" htmlFor="strategy-filter">
          <input
            id="strategy-filter"
            type="text"
            value={strategyDraft}
            placeholder="all strategies"
            onChange={(event) => setStrategyDraft(event.target.value)}
            className={CONTROL}
          />
        </Field>

        <Field label="Since" htmlFor="since-filter">
          <input
            id="since-filter"
            type="date"
            value={filters.since}
            onChange={(event) => setFilters((f) => ({ ...f, since: event.target.value }))}
            className={CONTROL}
          />
        </Field>

        <Field label="Rows" htmlFor="limit-filter">
          <select
            id="limit-filter"
            value={filters.limit}
            onChange={(event) => setFilters((f) => ({ ...f, limit: Number(event.target.value) }))}
            className={CONTROL}
          >
            {PAGE_SIZES.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
        </Field>

        <button
          type="submit"
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:text-slate-200"
        >
          Apply
        </button>
      </form>

      <section className="rounded border border-slate-800 bg-slate-900/20">
        <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-300">Orders</h2>
          {query.data ? (
            <span className="text-xs text-slate-500">
              {orders.length} shown, newest first ·{' '}
              {/* Which book these belong to. Paper and live share a table. */}
              <span className="text-slate-400">{query.data.run_mode}</span>
            </span>
          ) : null}
        </div>

        {query.isLoading ? (
          <p className="px-4 py-6 text-center text-sm text-slate-500">Loading…</p>
        ) : query.error ? (
          <p className="px-4 py-6 text-center text-sm text-amber-400">
            Could not load the order history.
            <span className="mt-1 block text-xs text-amber-200/70">
              {/* The server's own detail: a 422 names the statuses that exist,
                  and flattening that into a generic failure would undo the
                  reason it answers that way. */}
              {query.error instanceof ApiError ? query.error.detail : String(query.error)}
            </span>
          </p>
        ) : (
          <>
            {query.data?.limit_reached ? (
              // A page that came back full looks identical to a list that
              // ended, and only one of them means "this is everything".
              <p className="mx-4 mb-3 rounded border border-slate-700 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
                Showing the newest {filters.limit}. There are older orders than these — narrow the
                filters or raise the row count to reach them.
              </p>
            ) : null}
            <OrderHistoryTable orders={orders} />
          </>
        )}
      </section>
    </div>
  )
}
