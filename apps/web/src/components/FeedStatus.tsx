/**
 * Is market data still arriving?
 *
 * A quiet feed and a dead feed look identical from the outside, which is the
 * whole reason this exists as a visible indicator rather than as something an
 * operator infers from prices that have stopped moving.
 *
 * Three states, and the third is the one usually missing from a status light:
 *
 * - **healthy** — a tick inside the freshness budget the risk engine also uses;
 * - **stale** — silence during a session, which is what `StalenessMonitor`
 *   halts on;
 * - **unknown** — nothing has published a book to judge, so nothing is claimed.
 *   A green light here would be a guess, and a red one would cry wolf on every
 *   deployment before the worker's first pass.
 *
 * Out of hours the server reports healthy, because silence at 02:00 on a Sunday
 * is correct — a light that goes red every evening is a light nobody reads.
 */

import { UNKNOWN, formatTime } from '@/lib/money'

interface Props {
  healthy: boolean | null
  lastDataAt: string | null
  marketOpen: boolean
}

export default function FeedStatus({ healthy, lastDataAt, marketOpen }: Props) {
  const tone = healthy === null ? 'text-slate-500' : healthy ? 'text-emerald-400' : 'text-amber-400'
  // A shape as well as a colour: red/green alone is not a signal every reader
  // can use (docs/DASHBOARD.md).
  const mark = healthy === null ? '○' : healthy ? '●' : '▲'
  const label =
    healthy === null
      ? 'feed unknown'
      : healthy
        ? 'feed live'
        : marketOpen
          ? 'feed stale'
          : 'feed quiet'

  return (
    <span
      className={`flex items-center gap-1.5 text-xs ${tone}`}
      title={`last tick ${formatTime(lastDataAt)}`}
    >
      <span aria-hidden>{mark}</span>
      <span>{label}</span>
      <span className="text-slate-600">{lastDataAt ? formatTime(lastDataAt) : UNKNOWN}</span>
    </span>
  )
}
