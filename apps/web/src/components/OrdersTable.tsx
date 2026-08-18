/**
 * What is still working at the venue — layout item 7.
 *
 * "Working" is the operative word: these are the orders the platform believes
 * are live, which is the set reconciliation compares against. A partially
 * filled order shows both halves, because an order is not binary (CLAUDE.md §5)
 * and a row reporting only `qty` would hide the position that already exists.
 */

import { UNKNOWN, formatDecimal, formatMoney, formatTime } from '@/lib/money'
import type { OrderView } from '@/api/types'

interface Props {
  orders: OrderView[]
}

const SIDE_TONE: Record<string, string> = {
  buy: 'text-emerald-300',
  sell: 'text-rose-300',
}

/** The price this order is resting at, whichever kind of price that is. */
function restingPrice(order: OrderView): string {
  if (order.limit_price !== null) return `limit ${formatMoney(order.limit_price)}`
  if (order.stop_price !== null) return `stop ${formatMoney(order.stop_price)}`
  return 'market'
}

export default function OrdersTable({ orders }: Props) {
  return (
    <section className="rounded border border-slate-800 bg-slate-900/40">
      <div className="flex items-baseline justify-between px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-300">Working orders</h2>
        <span className="text-xs text-slate-500">{orders.length} live</span>
      </div>

      {orders.length === 0 ? (
        <p className="px-4 pb-4 text-sm text-slate-500">Nothing working at the venue.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-t border-slate-800 text-sm">
            <thead>
              <tr className="bg-slate-900/60">
                <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                  Symbol
                </th>
                <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                  Side
                </th>
                <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-wide text-slate-500">
                  Filled / Qty
                </th>
                <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-wide text-slate-500">
                  Price
                </th>
                <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-wide text-slate-500">
                  Avg fill
                </th>
                <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                  Status
                </th>
                <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-wide text-slate-500">
                  Sent
                </th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order) => (
                <tr key={order.id} className="border-t border-slate-800/70 hover:bg-slate-800/30">
                  <td className="px-3 py-2 font-medium text-slate-100">{order.symbol}</td>
                  <td
                    className={`px-3 py-2 uppercase ${SIDE_TONE[order.side] ?? 'text-slate-300'}`}
                  >
                    {order.side}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                    {formatDecimal(order.filled_qty)} / {formatDecimal(order.qty)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                    {restingPrice(order)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                    {order.avg_fill_price === null ? UNKNOWN : formatMoney(order.avg_fill_price)}
                  </td>
                  <td className="px-3 py-2 text-slate-400">{order.status.replace(/_/g, ' ')}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-500">
                    {formatTime(order.ts)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
