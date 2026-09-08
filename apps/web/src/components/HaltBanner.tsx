/**
 * Is trading stopped?
 *
 * Layout item 2, directly under the run-mode banner and above everything else:
 * if trading is halted, nothing else on the screen matters first. Not
 * dismissible, and never collapsed to a count — the *reason* is what an
 * operator acts on, and "who stopped it and when" is the first question asked
 * afterwards (docs/SAFETY.md, layer 6).
 *
 * The halt list is read live from the kill switch on every read rather than
 * from the worker's published book. That is deliberate on the server side and
 * worth restating here: a banner sourced from a snapshot that nobody is
 * publishing would say "not halted" at exactly the moment that matters most.
 *
 * **This banner is why halts stayed on a push path when the refresh cadence went
 * away (ADR 0022).** Halts arrive over the WebSocket, which every client receives
 * whether or not it subscribed to anything, and that message re-reads the
 * dashboard. So this appears within a second of trading stopping. Had it been
 * left to the reader to ask, the screen that exists to interrupt somebody would
 * have waited to be consulted.
 *
 * **Two fields here are not decoration (ADR 0029).** `reason` is the reason *in
 * force*, while `engaged_by`, `engaged_at` and `detail` describe the halt's
 * origin — and those stop being the same incident once a halt escalates.
 * Rendering the four alone produced a banner reading `reconciliation_mismatch`
 * beside `ops` and "pausing for lunch": three true fields composing one false
 * sentence. `escalated_from` explains the mismatch on screen.
 *
 * And `unproven_symbols` changes what the operator can *do*. The platform
 * refuses to close those — protective stops included — so without them this
 * screen, which docs/SAFETY.md calls the place you halt from, cannot say why a
 * flatten came back refused. It is called out separately from `detail` rather
 * than folded into it because the summary in `detail` names symbols for
 * findings that impugn nothing, and conflating the two is exactly the mistake
 * docs/RUNBOOK.md had to be corrected for.
 */

import { formatDateTime } from '@/lib/money'
import { useLiveDashboard } from '@/hooks/useLiveDashboard'
import ResumeButton from '@/components/ResumeButton'
import type { HaltView } from '@/api/types'

const SCOPE_LABEL: Record<string, string> = {
  global: 'ALL TRADING HALTED',
  strategy: 'STRATEGY HALTED',
  symbol: 'SYMBOL HALTED',
}

/** Optional on the wire because it has a server-side default, and empty for the
 *  overwhelming majority of halts. Read once so the two uses cannot disagree. */
function unproven(halt: HaltView): string[] {
  return halt.unproven_symbols ?? []
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
          {halt.escalated_from ? (
            <span className="text-rose-300/80">
              (escalated from {halt.escalated_from.replace(/_/g, ' ')}
              {halt.escalated_by ? ` by ${halt.escalated_by}` : ''})
            </span>
          ) : null}
          {halt.detail ? <span className="text-rose-300/80">— {halt.detail}</span> : null}
          <span className="ml-auto text-xs text-rose-400/80">
            by {halt.engaged_by} at {formatDateTime(halt.engaged_at)}
          </span>
          <ResumeButton halt={halt} />
          {unproven(halt).length > 0 ? (
            <p className="w-full text-xs text-amber-300">
              Cannot prove: <span className="font-bold">{unproven(halt).join(', ')}</span> — the
              platform will not close these, protective stops included. Use the broker's own UI.
              Every other position still closes normally.
            </p>
          ) : null}
        </div>
      ))}
      <p className="px-4 pb-2 text-xs text-rose-400/70">
        Clearing a halt is deliberate and needs the account password — see docs/RUNBOOK.md.
        Reconcile before you clear.
      </p>
    </div>
  )
}
