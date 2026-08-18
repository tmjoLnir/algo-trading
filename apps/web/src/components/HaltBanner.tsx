/**
 * Is trading stopped?
 *
 * Layout item 2, directly under the run-mode banner and above everything else:
 * if trading is halted, nothing else on the screen matters first. Not
 * dismissible, and never collapsed to a count — the *reason* is what an
 * operator acts on, and "who stopped it and when" is the first question asked
 * afterwards (docs/SAFETY.md, layer 6).
 *
 * The halt list is read live from the kill switch on every poll rather than
 * from the worker's published book. That is deliberate on the server side and
 * worth restating here: a banner sourced from a snapshot that nobody is
 * publishing would say "not halted" at exactly the moment that matters most.
 *
 * Halts also arrive over the WebSocket, which every client receives whether or
 * not it subscribed to anything — so this normally appears within a second
 * rather than at the next five-minute poll.
 */

import { formatDateTime } from '@/lib/money'
import { useLiveDashboard } from '@/hooks/useLiveDashboard'
import type { HaltView } from '@/api/types'

const SCOPE_LABEL: Record<string, string> = {
  global: 'ALL TRADING HALTED',
  strategy: 'STRATEGY HALTED',
  symbol: 'SYMBOL HALTED',
}

function scopeLabel(halt: HaltView): string {
  const base = SCOPE_LABEL[halt.scope] ?? 'TRADING HALTED'
  return halt.target ? `${base} — ${halt.target}` : base
}

export default function HaltBanner() {
  const { data } = useLiveDashboard()
  const halts = data?.active_halts ?? []
  if (halts.length === 0) return null

  return (
    <div className="border-b border-rose-800 bg-rose-950/80">
      {halts.map((halt) => (
        <div
          key={`${halt.scope}:${halt.target ?? 'global'}`}
          className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-2 text-sm"
        >
          <span className="font-bold tracking-wide text-rose-200">⛔ {scopeLabel(halt)}</span>
          <span className="text-rose-300">{halt.reason.replace(/_/g, ' ')}</span>
          {halt.detail ? <span className="text-rose-300/80">— {halt.detail}</span> : null}
          <span className="ml-auto text-xs text-rose-400/80">
            by {halt.engaged_by} at {formatDateTime(halt.engaged_at)}
          </span>
        </div>
      ))}
      <p className="px-4 pb-2 text-xs text-rose-400/70">
        Clearing a halt is deliberate and needs a named human — see docs/RUNBOOK.md. Reconcile
        before you clear.
      </p>
    </div>
  )
}
