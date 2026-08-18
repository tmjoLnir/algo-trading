/**
 * Is this real money?
 *
 * The most important pixel on the screen. Paper and live look identical
 * otherwise, and someone will eventually place a "test" order against a live
 * account. Loud, permanent, unmissable, never dismissible.
 *
 * The mode comes from the API's own configuration rather than from the worker's
 * published book, which is why this renders even when nothing is trading — a
 * banner that disappeared whenever the worker was down would be absent for
 * exactly the deployment somebody is poking at by hand.
 *
 * An unrecognised mode falls through to the *loudest* branch. Guessing wrong
 * towards "this is real" costs a moment's caution; guessing wrong the other way
 * costs an order.
 */

import { useLiveDashboard } from '@/hooks/useLiveDashboard'

export default function RunModeBanner() {
  const { data } = useLiveDashboard()
  if (!data) return null

  if (data.run_mode === 'backtest') {
    return (
      <div className="bg-slate-700 px-4 py-1.5 text-center text-xs font-semibold text-slate-200">
        BACKTEST MODE — no venue is connected; nothing here is a live account
      </div>
    )
  }
  if (data.run_mode === 'paper') {
    return (
      <div className="bg-amber-500/90 px-4 py-1.5 text-center text-xs font-semibold text-amber-950">
        PAPER TRADING — simulated money, live market data
      </div>
    )
  }
  return (
    <div className="bg-rose-600 px-4 py-2 text-center text-sm font-bold tracking-wide text-white">
      ⚠ LIVE TRADING — REAL MONEY AT RISK
    </div>
  )
}
