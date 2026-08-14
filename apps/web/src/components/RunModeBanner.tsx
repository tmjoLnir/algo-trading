/**
 * Is this real money?
 *
 * The most important pixel on the screen. Paper and live look identical
 * otherwise, and someone will eventually place a "test" order against a live
 * account. Loud, permanent, unmissable, never dismissible.
 */

import { useLiveDashboard } from '@/hooks/useLiveDashboard'

export default function RunModeBanner() {
  const { data } = useLiveDashboard()
  if (!data) return null

  if (data.run_mode === 'live') {
    return (
      <div className="bg-rose-600 px-4 py-2 text-center text-sm font-bold tracking-wide text-white">
        ⚠ LIVE TRADING — REAL MONEY AT RISK
      </div>
    )
  }
  return (
    <div className="bg-amber-500/90 px-4 py-1.5 text-center text-xs font-semibold text-amber-950">
      PAPER TRADING — simulated money, live market data
    </div>
  )
}
