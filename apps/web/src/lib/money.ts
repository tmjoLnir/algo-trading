/**
 * Rendering money that arrives as a string, without ever making it a number.
 *
 * The rule this file exists for: **never render a monetary value from a float**
 * (docs/DASHBOARD.md). The API sends every `Decimal` as a string precisely so
 * that JSON's single binary-float numeric type cannot round it, and
 * `parseFloat` on the way in throws that away one line before it is displayed.
 *
 * The usual answer is a decimal library. This dashboard does not need one,
 * because it does no arithmetic on money at all: every derived figure —
 * `unrealized_pnl_pct`, `leverage`, `distance_to_stop_pct`, the position
 * ordering — is computed server-side and arrives ready. What is left is
 * *formatting*, which is string manipulation: split on the point, group the
 * integer part, pad or trim the fraction. That is what this module does, and
 * why there is no `Decimal` dependency in package.json.
 *
 * Rounding a fraction shorter is deliberately **truncation**, not rounding.
 * A P&L of -0.006 displayed to two places should read `-0.00`, not `-0.01`: the
 * display is a view of an exact value held elsewhere, and inventing a hundredth
 * that the ledger does not contain is how a screen and a statement come to
 * disagree by a cent nobody can find.
 *
 * The one place a number appears is `toChartNumber`, for plotting. A chart
 * pixel is not a ledger entry, and it is named so that its single legitimate
 * use is obvious and every other call reads as a mistake.
 */

/** What to show where a figure is genuinely unknown, rather than zero. */
export const UNKNOWN = '—'

interface Parts {
  negative: boolean
  integer: string
  fraction: string
}

/**
 * Split a decimal string into its parts, or null if it is not one.
 *
 * Null rather than a throw or a zero: a malformed figure is a server bug, and
 * the dashboard's job at that moment is to say it does not know rather than to
 * crash the panel or, worse, to render `0`.
 */
function parse(value: string): Parts | null {
  const trimmed = value.trim()
  if (!/^-?\d+(\.\d+)?$/.test(trimmed)) return null
  const negative = trimmed.startsWith('-')
  const unsigned = negative ? trimmed.slice(1) : trimmed
  const [integer = '0', fraction = ''] = unsigned.split('.')
  return { negative, integer, fraction }
}

/** Group an integer-part string in threes: `1234567` → `1,234,567`. */
function group(integer: string): string {
  return integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/** Pad or truncate a fraction to exactly `places` digits. */
function fixFraction(fraction: string, places: number): string {
  if (places <= 0) return ''
  return fraction.slice(0, places).padEnd(places, '0')
}

interface FormatOptions {
  /** Fixed decimal places. Omit to show whatever the server sent. */
  places?: number
  /** Prefix a `+` on positive values. For P&L, where the sign is the point. */
  signed?: boolean
}

/**
 * A decimal string, grouped and padded for display.
 *
 * Returns `UNKNOWN` for null, undefined, or anything that is not a decimal
 * string — the three ways "we do not know" reaches the screen.
 */
export function formatDecimal(
  value: string | null | undefined,
  { places, signed = false }: FormatOptions = {},
): string {
  if (value === null || value === undefined) return UNKNOWN
  const parts = parse(value)
  if (parts === null) return UNKNOWN

  const fraction = places === undefined ? parts.fraction : fixFraction(parts.fraction, places)
  const body = group(parts.integer) + (fraction ? `.${fraction}` : '')
  if (parts.negative) return `-${body}`
  return signed && !isZero(parts) ? `+${body}` : body
}

/** Money, to two places by default. */
export function formatMoney(value: string | null | undefined, options: FormatOptions = {}): string {
  return formatDecimal(value, { places: 2, ...options })
}

/**
 * A server-sent fraction rendered as a percentage.
 *
 * The server sends ratios as fractions — `0.0125` means 1.25% — so this is the
 * one place a scale factor is applied, and it is done by moving the decimal
 * point in the string rather than by multiplying a float by 100.
 */
export function formatPercent(
  value: string | null | undefined,
  { places = 2, signed = false }: FormatOptions = {},
): string {
  if (value === null || value === undefined) return UNKNOWN
  const parts = parse(value)
  if (parts === null) return UNKNOWN

  const shifted = shiftPointRight(parts, 2)
  const rendered = formatDecimal(shifted, { places, signed })
  return rendered === UNKNOWN ? UNKNOWN : `${rendered}%`
}

/** Move the decimal point `by` digits to the right, as a string. */
function shiftPointRight(parts: Parts, by: number): string {
  const digits = parts.integer + parts.fraction
  const pointAt = parts.integer.length + by
  const padded = digits.padEnd(pointAt, '0')
  const integer = padded.slice(0, pointAt).replace(/^0+(?=\d)/, '')
  const fraction = padded.slice(pointAt)
  return `${parts.negative ? '-' : ''}${integer}${fraction ? `.${fraction}` : ''}`
}

function isZero(parts: Parts): boolean {
  return /^0*$/.test(parts.integer) && /^0*$/.test(parts.fraction)
}

/**
 * The sign of a decimal string: 1, -1, or 0.
 *
 * String inspection, not a comparison against a parsed number. Used to pick a
 * colour AND an arrow — colour alone is not a signal a colour-blind reader can
 * use, which docs/DASHBOARD.md makes a rule.
 */
export function signOf(value: string | null | undefined): -1 | 0 | 1 {
  if (value === null || value === undefined) return 0
  const parts = parse(value)
  if (parts === null || isZero(parts)) return 0
  return parts.negative ? -1 : 1
}

/** `▲`, `▼` or an en dash — the non-colour half of a gain/loss indicator. */
export function directionArrow(value: string | null | undefined): string {
  const sign = signOf(value)
  return sign > 0 ? '▲' : sign < 0 ? '▼' : '–'
}

/** Tailwind classes for a gain/loss figure. Always paired with an arrow. */
export function toneFor(value: string | null | undefined): string {
  const sign = signOf(value)
  if (sign > 0) return 'text-emerald-400'
  if (sign < 0) return 'text-rose-400'
  return 'text-slate-300'
}

/**
 * A decimal string as a JavaScript number, **for chart geometry only**.
 *
 * The one legitimate float conversion in the dashboard: a chart maps values to
 * pixels, and a pixel has no cents. Never use this to compute, compare or
 * display a balance — that is the bug rule §1.1 exists to prevent, and the name
 * is deliberately unwieldy so a misuse reads as one.
 */
export function toChartNumber(value: string): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

/**
 * How stale something is, in words.
 *
 * Seconds up to a minute, then minutes, then hours. Precision past that is
 * noise — the question a reader is asking is "can I act on this", and the
 * answer changes at those boundaries and nowhere else.
 */
export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return UNKNOWN
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  return `${Math.floor(seconds / 3600)}h`
}

/** A UTC timestamp as local wall-clock time, or `UNKNOWN`. */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return UNKNOWN
  const at = new Date(iso)
  return Number.isNaN(at.getTime())
    ? UNKNOWN
    : at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/** A UTC timestamp as a local date and time, or `UNKNOWN`. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return UNKNOWN
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? UNKNOWN : at.toLocaleString()
}
