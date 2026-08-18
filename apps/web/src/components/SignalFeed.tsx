/**
 * What the rules decided, and *why* — layout item 6, and requirement #7's
 * "review live trades selected based on preset rules".
 *
 * The rule this component exists for: **show `reason` on every signal**. "Why
 * is this trade on?" must be answerable without opening a log.
 *
 * The second, less obvious one: a *refused* signal is as much a feed entry as
 * an executed one, and the rule that refused it is shown by name. A strategy
 * blocked by a risk limit on every bar looks, from anywhere else in the system,
 * exactly like a strategy that had no ideas — and this is the screen where that
 * difference is meant to be visible.
 *
 * `no_action` is a third outcome and is styled apart from a refusal: an exit
 * for a position that is already flat is not the risk engine saying no, and
 * colouring it as one would inflate what an operator reads to decide whether
 * their limits are too tight.
 */

import { formatTime } from '@/lib/money'
import type { SignalView } from '@/api/types'

interface Props {
  signals: SignalView[]
}

const ACTION_TONE: Record<string, string> = {
  enter_long: 'bg-emerald-900/60 text-emerald-300',
  enter_short: 'bg-rose-900/60 text-rose-300',
  exit: 'bg-sky-900/60 text-sky-300',
  scale_in: 'bg-emerald-900/40 text-emerald-400',
  scale_out: 'bg-sky-900/40 text-sky-400',
}

/** Outcomes the router reports as "not submitted" but which nothing refused. */
const NO_ACTION = 'no_action'

function Outcome({ signal }: { signal: SignalView }) {
  if (signal.acted_on) {
    return (
      <span className="rounded bg-emerald-900/50 px-1.5 py-0.5 text-xs text-emerald-300">
        order sent
      </span>
    )
  }
  if (signal.rejected_by === NO_ACTION || signal.rejected_by === null) {
    return (
      <span className="rounded bg-slate-800 px-1.5 py-0.5 text-xs text-slate-400">no action</span>
    )
  }
  return (
    <span className="rounded bg-amber-900/50 px-1.5 py-0.5 text-xs text-amber-300">
      blocked · {signal.rejected_by}
    </span>
  )
}

export default function SignalFeed({ signals }: Props) {
  return (
    <section className="rounded border border-slate-800 bg-slate-900/40">
      <div className="flex items-baseline justify-between px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-300">Signals</h2>
        <span className="text-xs text-slate-500">{signals.length} recent</span>
      </div>

      {signals.length === 0 ? (
        <p className="px-4 pb-4 text-sm text-slate-500">
          No decisions recorded. A worker that is not trading emits none, and the feed is held in
          memory — a restart empties it.
        </p>
      ) : (
        <ul className="max-h-96 divide-y divide-slate-800/70 overflow-y-auto border-t border-slate-800">
          {signals.map((signal) => (
            <li key={signal.id} className="px-4 py-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-slate-100">{signal.symbol}</span>
                <span
                  className={`rounded px-1.5 py-0.5 text-xs ${
                    ACTION_TONE[signal.action] ?? 'bg-slate-800 text-slate-300'
                  }`}
                >
                  {signal.action.replace(/_/g, ' ')}
                </span>
                <Outcome signal={signal} />
                <span className="ml-auto text-xs tabular-nums text-slate-500">
                  {formatTime(signal.ts)}
                </span>
              </div>

              {/* The whole point of the panel. Never conditional. */}
              <p className="mt-1 text-sm text-slate-400">
                {signal.reason || <span className="italic text-slate-600">no reason recorded</span>}
              </p>

              {signal.rejection_reason && !signal.acted_on ? (
                <p className="mt-1 text-xs text-amber-300/80">{signal.rejection_reason}</p>
              ) : null}

              {Object.keys(signal.indicators ?? {}).length > 0 ? (
                <p className="mt-1 flex flex-wrap gap-x-3 text-xs tabular-nums text-slate-500">
                  {Object.entries(signal.indicators ?? {}).map(([name, value]) => (
                    <span key={name}>
                      {name} <span className="text-slate-400">{value}</span>
                    </span>
                  ))}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
