/**
 * The order table, from `/orders`.
 *
 * Distinct from `OrdersTable`, which renders the *working* orders in the live
 * book. This one renders history, and the rows that matter most are the ones
 * that never filled: a refused order appears in no other read in the platform —
 * not in the book, not in a reconstructed round trip, not on the equity curve.
 *
 * Three rules shape it.
 *
 * **A refusal shows why it was refused.** The same rule docs/DASHBOARD.md makes
 * for signals — show `reason` on every one, including refused ones. A rejection
 * on screen with no reason tells the reader something went wrong and not what,
 * which is the half of the message they cannot act on. A refused order whose
 * reason was never recorded says *that*, rather than rendering the dash that
 * means "nothing refused this".
 *
 * **A partial fill is a proportion, not a status.** `filled_qty` against `qty`
 * is what says whether an order moved 5 shares or 500 of the 500 it asked for,
 * and the status word alone does not: `cancelled` covers both an order that
 * never traded and one that filled 90% before the cancel landed, and those are
 * different positions.
 *
 * **Colour is an accent on text that already says it.** Every row names its
 * status and its purpose in words.
 */

import { UNKNOWN, formatDateTime, formatDecimal, formatMoney, formatTime } from '@/lib/money'
import type { OrderHistoryView } from '@/api/types'

interface Props {
  orders: OrderHistoryView[]
}

/** Statuses that mean something refused this order rather than it resolving. */
const REFUSED = new Set(['rejected_risk', 'rejected'])

const STATUS_TONE: Record<string, string> = {
  filled: 'text-emerald-400',
  partially_filled: 'text-sky-400',
  submitted: 'text-sky-400',
  pending_risk: 'text-slate-400',
  pending_submit: 'text-slate-400',
  cancelled: 'text-slate-400',
  expired: 'text-slate-400',
  rejected_risk: 'text-rose-400',
  rejected: 'text-rose-400',
}

/**
 * The status in words a person uses, rather than the wire value.
 *
 * The two refusals read differently on purpose: our own risk engine declining to
 * send an order and the venue declining one we sent call for opposite responses,
 * and "rejected" for both would hide which happened.
 */
const STATUS_LABEL: Record<string, string> = {
  pending_risk: 'awaiting risk',
  rejected_risk: 'refused by risk',
  pending_submit: 'awaiting submit',
  submitted: 'working',
  partially_filled: 'partially filled',
  filled: 'filled',
  cancelled: 'cancelled',
  rejected: 'refused by venue',
  expired: 'expired',
}

/** What an order was for. Null on rows stored before the column existed. */
const PURPOSE_LABEL: Record<string, string> = {
  entry: 'entry',
  exit: 'exit',
  stop_loss: 'stop',
  take_profit: 'target',
  time_exit: 'time exit',
  flatten: 'flatten',
  manual: 'manual',
}

function Header({ children, align = 'right' }: { children: string; align?: 'left' | 'right' }) {
  return (
    <th
      className={`px-3 py-2 text-xs font-medium uppercase tracking-wide text-slate-500 ${
        align === 'left' ? 'text-left' : 'text-right'
      }`}
    >
      {children}
    </th>
  )
}

/**
 * How much of what was asked for actually traded.
 *
 * A bar as well as the two numbers, because "did this fill" is a question about
 * proportion and the status word does not answer it — a cancelled order that
 * filled 90% first is a position, and a cancelled order that filled none is not.
 */
function FillProgress({ filled, asked }: { filled: string; asked: string }) {
  // Geometry only — never a displayed figure. The numbers beside it are the
  // server's strings, untouched.
  const fraction = Number(asked) > 0 ? Number(filled) / Number(asked) : 0
  const complete = fraction >= 1
  const none = fraction <= 0
  return (
    <div className="flex items-center justify-end gap-2">
      <span className="tabular-nums text-slate-300">
        {formatDecimal(filled)}
        <span className="text-slate-600"> / </span>
        {formatDecimal(asked)}
      </span>
      <div className="h-1.5 w-10 overflow-hidden rounded bg-slate-800">
        <div
          className={`h-full ${complete ? 'bg-emerald-500' : none ? 'bg-slate-700' : 'bg-sky-500'}`}
          style={{ width: `${Math.max(0, Math.min(1, fraction)) * 100}%` }}
        />
      </div>
    </div>
  )
}

/**
 * Why an order was refused, or why the cell is empty.
 *
 * Three states, not two. Nothing refused this order — a dash. Something refused
 * it and said why — the reason. Something refused it and no reason was
 * recorded — said out loud, because a silent dash there is indistinguishable
 * from the first case and means the opposite.
 */
function Reason({ order }: { order: OrderHistoryView }) {
  if (order.reject_reason) {
    return <span className="text-rose-300">{order.reject_reason}</span>
  }
  if (REFUSED.has(order.status)) {
    return <span className="text-amber-400">refused, but no reason was recorded</span>
  }
  return <span className="text-slate-600">{UNKNOWN}</span>
}

/** The price an order asked for, which depends on its type. */
function RequestedPrice({ order }: { order: OrderHistoryView }) {
  if (order.limit_price !== null && order.stop_price !== null) {
    // A stop-limit names two, and which is which changes what the order does.
    // Both are labelled rather than left to be inferred from their order.
    return (
      <>
        <span className="text-slate-500">stop </span>
        {formatMoney(order.stop_price)}
        <div className="text-xs text-slate-500">limit {formatMoney(order.limit_price)}</div>
      </>
    )
  }
  if (order.limit_price !== null) return <>{formatMoney(order.limit_price)}</>
  if (order.stop_price !== null) return <>{formatMoney(order.stop_price)}</>
  // A market order names no price, which is not a missing figure.
  return <span className="text-slate-600">at market</span>
}

function Row({ order }: { order: OrderHistoryView }) {
  return (
    <tr className="border-t border-slate-800/70 align-top hover:bg-slate-800/30">
      <td className="whitespace-nowrap px-3 py-2 text-left text-slate-400 tabular-nums">
        {formatDateTime(order.created_at)}
        {order.filled_at ? (
          <div className="text-xs text-slate-500">filled {formatTime(order.filled_at)}</div>
        ) : null}
      </td>
      <td className="px-3 py-2 text-left font-medium text-slate-100">
        {order.symbol}
        <span className="ml-2 text-xs text-slate-500">{order.side.toUpperCase()}</span>
        <div className="text-xs text-slate-500">
          {order.purpose === null ? (
            // Not guessed into a bucket: labelling a historical exit an "entry"
            // is worse than admitting the record does not say.
            <span className="text-amber-400/70">purpose not recorded</span>
          ) : (
            (PURPOSE_LABEL[order.purpose] ?? order.purpose)
          )}
          {order.strategy_id ? ` · ${order.strategy_id}` : ''}
        </div>
      </td>
      <td className="px-3 py-2 text-right text-slate-400">
        {order.order_type}
        <div className="text-xs text-slate-500">{order.time_in_force}</div>
      </td>
      <td className="px-3 py-2 text-right">
        <FillProgress filled={order.filled_qty} asked={order.qty} />
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-slate-300">
        <RequestedPrice order={order} />
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-slate-300">
        {formatMoney(order.avg_fill_price)}
      </td>
      <td
        className={`whitespace-nowrap px-3 py-2 text-left font-medium ${
          STATUS_TONE[order.status] ?? 'text-slate-300'
        }`}
      >
        {STATUS_LABEL[order.status] ?? order.status}
      </td>
      <td className="px-3 py-2 text-left text-xs">
        <Reason order={order} />
      </td>
    </tr>
  )
}

export default function OrderHistoryTable({ orders }: Props) {
  if (orders.length === 0) {
    return (
      <p className="px-4 py-6 text-center text-sm text-slate-500">
        No orders match these filters.
        <span className="mt-1 block text-xs">
          This is the whole order table, refusals included — an empty result means nothing was
          placed, not that nothing filled.
        </span>
      </p>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-slate-900/60">
            <Header align="left">Decided</Header>
            <Header align="left">Symbol</Header>
            <Header>Type</Header>
            <Header>Filled / asked</Header>
            <Header>Price</Header>
            <Header>Avg fill</Header>
            <Header align="left">Status</Header>
            <Header align="left">Reason</Header>
          </tr>
        </thead>
        <tbody>
          {orders.map((order) => (
            <Row key={order.id} order={order} />
          ))}
        </tbody>
      </table>
    </div>
  )
}
