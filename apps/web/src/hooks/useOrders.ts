/**
 * The order-history query.
 *
 * Read from the order table rather than from the worker's published book. ADR
 * 0007 sends the *live* screen to the worker's snapshot because two processes
 * computing "what do we hold" at two instants disagree; a stored order is a
 * record of something that already happened, so reading it here cannot disagree
 * with anything the runner is doing — the same reasoning `/analytics` uses.
 *
 * Not polled. The dashboard refreshes every five minutes because the book
 * moves; this is a log, and the newest row on it is already the newest row.
 * `refetchOnWindowFocus` (the client default) covers coming back to the tab.
 */

import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/api/client'
import type { OrdersResponse } from '@/api/types'

/**
 * The statuses the filter offers, in lifecycle order.
 *
 * The two refusals are listed apart and worded apart on purpose. `rejected_risk`
 * is *our* engine refusing to send an order; `rejected` is the venue refusing
 * one we sent. They call for opposite responses — the first is a limit doing its
 * job, the second is a problem with the account or the instrument — and a single
 * "rejected" bucket would hide which happened.
 */
export const ORDER_STATUSES = [
  { value: '', label: 'Every status' },
  { value: 'pending_risk', label: 'Awaiting risk' },
  { value: 'rejected_risk', label: 'Refused by risk' },
  { value: 'pending_submit', label: 'Awaiting submit' },
  { value: 'submitted', label: 'Working' },
  { value: 'partially_filled', label: 'Partially filled' },
  { value: 'filled', label: 'Filled' },
  { value: 'cancelled', label: 'Cancelled' },
  { value: 'rejected', label: 'Refused by venue' },
  { value: 'expired', label: 'Expired' },
] as const

/** How many rows a page asks for. The server refuses anything above 500. */
export const PAGE_SIZES = [100, 250, 500] as const

export interface OrderFilters {
  status: string
  symbol: string
  strategyId: string
  since: string
  limit: number
}

export const NO_FILTERS: OrderFilters = {
  status: '',
  symbol: '',
  strategyId: '',
  since: '',
  limit: 100,
}

function toParams(filters: OrderFilters): URLSearchParams {
  const params = new URLSearchParams({ limit: String(filters.limit) })
  if (filters.status) params.set('status', filters.status)
  if (filters.symbol) params.set('symbol', filters.symbol)
  if (filters.strategyId) params.set('strategy_id', filters.strategyId)
  // A date input yields `YYYY-MM-DD`; the server reads it as the start of that
  // day. Sent as-is rather than converted to an instant here, so the boundary
  // stays somewhere a reader can see it.
  if (filters.since) params.set('since', filters.since)
  return params
}

export function useOrders(filters: OrderFilters) {
  return useQuery<OrdersResponse>({
    queryKey: [
      'orders',
      filters.status,
      filters.symbol,
      filters.strategyId,
      filters.since,
      filters.limit,
    ],
    queryFn: () => apiGet<OrdersResponse>(`/api/v1/orders?${toParams(filters)}`),
  })
}
