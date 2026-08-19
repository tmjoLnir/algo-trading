/**
 * Rendering *statistics*, which arrive as JSON numbers rather than as strings.
 *
 * This module is the deliberate counterpart to `money.ts`, and the boundary
 * between them is the point of both files existing.
 *
 * `money.ts` formats decimal **strings** and never parses one, because a
 * balance is exact and IEEE 754 cannot hold it (CLAUDE.md §1.1). The analytics
 * endpoints send something else: `/analytics/performance` returns a metric set
 * of JSON numbers, because a Sharpe ratio, a win rate and a volatility are
 * float statistics computed by `backtest/metrics.py` — shared with the backtest
 * engine on purpose, so that a live Sharpe can be compared to the backtested
 * one (docs/ANALYTICS.md).
 *
 * **Five of those metrics are money-denominated and still arrive as floats** —
 * `expectancy`, `avg_win`, `avg_loss`, `largest_win`, `largest_loss`. That is
 * not a leak to be papered over here: `compute_all` computes them in float
 * space, so the precision is already gone before the response is serialised,
 * and formatting them through `formatMoney` would dress a float up as a ledger
 * figure. They are presented as statistics — which is what they are — and the
 * screen says so. `money.ts` accepts only strings, so the compiler refuses the
 * confusion rather than relying on anybody remembering it.
 *
 * Everything a *trade* carries — every price, quantity, fee and P&L on
 * `/analytics/trades` — is a decimal string and goes through `money.ts`
 * untouched. Nothing in this module ever sees one.
 */

import { UNKNOWN } from './money'

interface StatOptions {
  /** Decimal places. Statistics are rounded, unlike money, which is truncated. */
  places?: number
  /** Prefix a `+` on positive values, for figures whose sign is the point. */
  signed?: boolean
}

/** True for anything that cannot be shown as a figure at all. */
function unusable(value: number | null | undefined): value is null | undefined {
  return value === null || value === undefined || !Number.isFinite(value)
}

/**
 * A float statistic, grouped and fixed to `places`.
 *
 * `UNKNOWN` for null, undefined, NaN and both infinities. The last three matter
 * more here than they would elsewhere: a profit factor over a period with no
 * losing trades is a division by zero, and `Infinity` reaching the screen as
 * the word "Infinity" reads as a broken panel rather than as a statistic that
 * has no value yet.
 */
export function formatStat(
  value: number | null | undefined,
  { places = 2, signed = false }: StatOptions = {},
): string {
  if (unusable(value)) return UNKNOWN
  const body = value.toLocaleString(undefined, {
    minimumFractionDigits: places,
    maximumFractionDigits: places,
  })
  return signed && value > 0 ? `+${body}` : body
}

/**
 * A fraction rendered as a percentage: `0.0125` → `1.25%`.
 *
 * The metric set states its ratios as fractions — `win_rate`, `total_return`,
 * `cagr`, `max_drawdown`, `volatility` and, despite its name, `exposure_pct`.
 * `contribution_pct` on an attribution row is the exception and is already
 * scaled; it goes through `formatStat` with a literal `%` instead.
 */
export function formatStatPercent(
  value: number | null | undefined,
  options: StatOptions = {},
): string {
  if (unusable(value)) return UNKNOWN
  const rendered = formatStat(value * 100, options)
  return rendered === UNKNOWN ? UNKNOWN : `${rendered}%`
}

/** A whole number — a trade count, a duration in days. */
export function formatCount(value: number | null | undefined): string {
  if (unusable(value)) return UNKNOWN
  return Math.round(value).toLocaleString()
}

/**
 * A holding period in hours, in the units a reader thinks in.
 *
 * Minutes below an hour, hours below a day, then days. A round trip held for
 * 0.03 hours is a scalp and "0.03h" makes nobody picture two minutes.
 */
export function formatDuration(hours: number | null | undefined): string {
  if (unusable(hours)) return UNKNOWN
  if (hours < 1) return `${Math.round(hours * 60)}m`
  if (hours < 24) return `${formatStat(hours, { places: 1 })}h`
  return `${formatStat(hours / 24, { places: 1 })}d`
}

/** The sign of a statistic: 1, -1 or 0. The float counterpart of `signOf`. */
export function statSign(value: number | null | undefined): -1 | 0 | 1 {
  if (unusable(value) || value === 0) return 0
  return value < 0 ? -1 : 1
}

/**
 * Tailwind classes for a statistic whose sign matters. Always paired with an
 * arrow — colour is never the only signal (docs/DASHBOARD.md).
 */
export function statTone(value: number | null | undefined): string {
  const sign = statSign(value)
  if (sign > 0) return 'text-emerald-400'
  if (sign < 0) return 'text-rose-400'
  return 'text-slate-300'
}

/** `▲`, `▼` or an en dash — the non-colour half of the indicator. */
export function statArrow(value: number | null | undefined): string {
  const sign = statSign(value)
  return sign > 0 ? '▲' : sign < 0 ? '▼' : '–'
}
