import { describe, expect, it } from 'vitest'
import {
  UNKNOWN,
  directionArrow,
  formatAge,
  formatDecimal,
  formatMoney,
  formatPercent,
  signOf,
  toneFor,
} from './money'

/**
 * Formatting money that never becomes a number.
 *
 * The backend sends every `Decimal` as a string precisely so JSON's single
 * binary-float type cannot round it. This module is the last place that
 * guarantee can be thrown away, so what is pinned here is not "does it look
 * nice" — it is that a value survives display unrounded, that an unknown figure
 * never renders as a zero, and that a loss is legible without colour.
 */

describe('formatMoney', () => {
  it('groups thousands and fixes two places', () => {
    expect(formatMoney('1234567.5')).toBe('1,234,567.50')
  })

  it('keeps a value a double could not hold', () => {
    // 100.333333333333333333 is beyond a float's 15-17 significant digits. The
    // formatter must be reading the string, not a parsed number.
    expect(formatMoney('100.333333333333333333', { places: 18 })).toBe('100.333333333333333333')
  })

  it('truncates rather than rounds', () => {
    // The display is a view of an exact value held elsewhere. Rounding -0.006
    // up to -0.01 invents a hundredth the ledger does not contain, which is how
    // a screen and a statement come to disagree by a cent nobody can find.
    expect(formatMoney('-0.006')).toBe('-0.00')
    expect(formatMoney('0.999')).toBe('0.99')
  })

  it('renders an unknown figure as a dash, never as zero', () => {
    // The API sends null for a figure it could not compute — an unmarked
    // position, leverage against no equity. Zero is a value a reader acts on.
    expect(formatMoney(null)).toBe(UNKNOWN)
    expect(formatMoney(undefined)).toBe(UNKNOWN)
  })

  it('renders a malformed figure as a dash rather than crashing the panel', () => {
    expect(formatMoney('not a number')).toBe(UNKNOWN)
    expect(formatMoney('1e5')).toBe(UNKNOWN)
  })

  it('signs a positive value only when asked', () => {
    expect(formatMoney('12.5', { signed: true })).toBe('+12.50')
    expect(formatMoney('12.5')).toBe('12.50')
    expect(formatMoney('-12.5', { signed: true })).toBe('-12.50')
  })

  it('does not sign zero', () => {
    // "+0.00" reads as a gain that rounded away. It is neither.
    expect(formatMoney('0', { signed: true })).toBe('0.00')
    expect(formatMoney('-0.000', { signed: true })).toBe('-0.00')
  })
})

describe('formatDecimal', () => {
  it('shows what the server sent when no precision is asked for', () => {
    expect(formatDecimal('10')).toBe('10')
    expect(formatDecimal('10.5')).toBe('10.5')
  })

  it('pads a short fraction out to the requested places', () => {
    expect(formatDecimal('7.5', { places: 4 })).toBe('7.5000')
  })
})

describe('formatPercent', () => {
  it('reads a server fraction as a percentage', () => {
    // The server sends ratios as fractions: 0.0125 means 1.25%.
    expect(formatPercent('0.0125')).toBe('1.25%')
  })

  it('shifts the point rather than multiplying a float', () => {
    // 0.07 * 100 is 7.000000000000001 in IEEE 754. A string shift is exact.
    expect(formatPercent('0.07')).toBe('7.00%')
  })

  it('handles a negative fraction', () => {
    expect(formatPercent('-0.0325', { signed: true })).toBe('-3.25%')
  })

  it('handles a value above one', () => {
    expect(formatPercent('2.5', { places: 0 })).toBe('250%')
  })

  it('is a dash when unknown', () => {
    expect(formatPercent(null)).toBe(UNKNOWN)
  })
})

describe('signOf and directionArrow', () => {
  it('reads the sign from the string', () => {
    expect(signOf('-0.01')).toBe(-1)
    expect(signOf('0.01')).toBe(1)
    expect(signOf('0.00')).toBe(0)
    expect(signOf('-0.000')).toBe(0)
    expect(signOf(null)).toBe(0)
  })

  it('gives a loss an arrow as well as a colour', () => {
    // Colour is not the only signal — docs/DASHBOARD.md makes that a rule,
    // because a red-green screen is unreadable to a good fraction of people.
    expect(directionArrow('-5')).toBe('▼')
    expect(directionArrow('5')).toBe('▲')
    expect(toneFor('-5')).not.toBe(toneFor('5'))
  })
})

describe('formatAge', () => {
  it('changes unit where the reader’s question changes', () => {
    expect(formatAge(42)).toBe('42s')
    expect(formatAge(90)).toBe('1m')
    expect(formatAge(7200)).toBe('2h')
  })

  it('never reports a negative age', () => {
    // Clock skew between the worker and the API is ordinary; "-3s ago" reads as
    // a bug in the dashboard rather than as the skew it is.
    expect(formatAge(-3)).toBe('0s')
  })

  it('is a dash when unknown', () => {
    expect(formatAge(null)).toBe(UNKNOWN)
  })
})
