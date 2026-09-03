/**
 * Equity, day P&L, exposure, leverage — layout item 3 in docs/DASHBOARD.md.
 *
 * The first numbers a person reads, so the rules are strict here:
 *
 * - a figure the server could not compute renders as `—`, never as `0`. The
 *   API sends null for exactly that reason, and turning it into a zero on the
 *   way to the screen would undo the whole point;
 * - gain and loss carry an arrow as well as a colour, because colour alone is
 *   not a signal every reader can use;
 * - nothing here computes anything. Every figure below arrives ready.
 */

import { UNKNOWN, directionArrow, formatMoney, formatPercent, toneFor } from '@/lib/money'
import type { AccountView } from '@/api/types'

interface Props {
  account: AccountView | null
  marketOpen: boolean
  /** Null when the worker has published nothing — see `Dashboard`. */
  bookAgeSeconds: number | null
}

function Figure({
  label,
  value,
  tone,
  hint,
}: {
  label: string
  value: string
  tone?: string
  hint?: string
}) {
  return (
    <div className="min-w-0">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`truncate text-xl font-semibold tabular-nums ${tone ?? 'text-slate-100'}`}>
        {value}
      </div>
      {hint ? <div className="truncate text-xs text-slate-500">{hint}</div> : null}
    </div>
  )
}

export default function AccountSummary({ account, marketOpen, bookAgeSeconds }: Props) {
  if (account === null) {
    // Not "you have nothing". The worker has not said what you have, and those
    // are different sentences — only one of them is safe to act on.
    return (
      <section className="rounded border border-amber-700/60 bg-amber-950/30 p-4">
        <h2 className="text-sm font-semibold text-amber-300">No book published</h2>
        <p className="mt-1 text-sm text-amber-200/80">
          The worker has not reported an account. It may not be trading (no strategy chosen on the{' '}
          <strong>Config</strong> tab), or it may have only just started. This is{' '}
          <strong>not</strong> a statement that you hold nothing.
        </p>
      </section>
    )
  }

  const dayTone = toneFor(account.day_pnl)
  const unmarked = account.unmarked_symbols ?? []

  return (
    <section className="rounded border border-slate-800 bg-slate-900/40 p-4">
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-slate-300">Account</h2>
        <span className="text-xs text-slate-500">
          {marketOpen ? 'market open' : 'market closed'}
          {bookAgeSeconds !== null && bookAgeSeconds > 0 ? ` · book ${bookAgeSeconds}s old` : ''}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
        <Figure label="Equity" value={formatMoney(account.equity)} />
        <Figure
          label="Day P&L"
          value={
            account.day_pnl === null
              ? UNKNOWN
              : `${directionArrow(account.day_pnl)} ${formatMoney(account.day_pnl, { signed: true })}`
          }
          tone={dayTone}
          hint={
            account.day_pnl === null
              ? 'no session anchor yet'
              : formatPercent(account.day_pnl_pct, { signed: true })
          }
        />
        <Figure label="Cash" value={formatMoney(account.cash)} />
        <Figure
          label="Gross exposure"
          value={formatMoney(account.gross_exposure)}
          hint={`net ${formatMoney(account.net_exposure)}`}
        />
        <Figure
          label="Leverage"
          value={account.leverage === null ? UNKNOWN : `${formatMoney(account.leverage)}×`}
          hint={account.leverage === null ? 'undefined at zero equity' : undefined}
        />
        <Figure
          label="Open positions"
          value={String(account.open_position_count)}
          hint={`unrealised ${formatMoney(account.unrealized_pnl, { signed: true })}`}
        />
      </div>

      {unmarked.length > 0 ? (
        // Every figure above understates exposure and equity while this is
        // non-empty, which is the direction that makes a breached limit look
        // compliant. Said out loud rather than left to be inferred.
        <p className="mt-3 rounded border border-amber-700/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          ⚠ No price for {unmarked.join(', ')} — the figures above exclude{' '}
          {unmarked.length === 1 ? 'it' : 'them'} and therefore understate exposure.
        </p>
      ) : null}
    </section>
  )
}
