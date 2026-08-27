import { describe, expect, it } from 'vitest'
import { EXPORT_FORMAT, buildRunExport, hasResultBody, runExportFilename } from './backtestExport'
import type { BacktestOut, BacktestSpecView } from '@/api/types'

/**
 * What a downloaded run is allowed to contain.
 *
 * A file outlives the screen that made it, which is what these assertions are
 * about. Three properties matter and none of them is cosmetic:
 *
 * 1. **A decimal never becomes a float.** Every monetary figure on the wire is a
 *    string so JSON's single binary numeric type cannot round it (rule §1.1),
 *    and an export is the last place that guarantee can be thrown away — a
 *    starting cash or a fill price that went through a double on the way to disk
 *    is a wrong number in a file somebody will later trust.
 * 2. **An absent result and an empty one are different facts.** `null` means the
 *    run has no result body — `RunRepository.fail` clears the curve and the
 *    trades, and a queued run never had them. `[]` means it stored one and it is
 *    genuinely empty, which is what a finished run that closed no round trip
 *    looks like.
 * 3. **A filename is not a path.** A strategy id comes from the database and is
 *    chosen by whoever registered the strategy, so it reaches this module
 *    untrusted with a separator.
 */

const SPEC: BacktestSpecView = {
  strategy_id: 'sma_crossover',
  symbols: ['SPY'],
  start: '2024-01-01T00:00:00Z',
  end: '2025-01-01T00:00:00Z',
  timeframe: '1d',
  starting_cash: '100000.00',
  cost_model: 'alpaca_equities',
  params: {},
  qty: '100',
  sizing_method: 'fixed_qty',
  sizing_value: '100',
  stop_type: '',
  stop_value: '',
  stop_period: 14,
  stop_bars: 0,
}

function run(overrides: Partial<BacktestOut> = {}): BacktestOut {
  return {
    id: 'run-1',
    strategy_id: 'sma_crossover',
    status: 'done',
    spec: SPEC,
    metrics: { total_return: 0.184, sharpe: 1.21, profit_factor: null },
    error: null,
    queued_at: '2026-08-20T09:00:00Z',
    started_at: '2026-08-20T09:00:05Z',
    finished_at: '2026-08-20T09:02:10Z',
    progress: null,
    warnings: [],
    totals: {
      starting_equity: '100000',
      ending_equity: '118400',
      total_return: '0.184',
      realized_pnl: '16400',
      unrealized_pnl: '2000',
      fees: '128.55',
      open_positions: 1,
      orders: 240,
      filled_orders: 236,
      signals: 251,
    },
    ...overrides,
  }
}

const AT = '2026-08-21T10:00:00.000Z'

describe('what the file contains', () => {
  it('stamps what it is and when it was taken', () => {
    // Versioned, because the shape is a contract the moment somebody scripts
    // against it. `exported_at` is when the file was made — `finished_at` on the
    // run is the timestamp that says something about the result.
    const file = buildRunExport(run(), { curve: [], trades: [], exportedAt: AT })

    expect(file.format).toBe(EXPORT_FORMAT)
    expect(file.exported_at).toBe(AT)
    expect(file.run.finished_at).toBe('2026-08-20T09:02:10Z')
  })

  it("carries the run's money, as the strings the server sent", () => {
    // The file used to be missing every money figure a run produced, because
    // the queued path did not store them (ADR 0019). It rides along now for
    // free — the run is copied verbatim — and this is what keeps that true:
    // never a `parseFloat` on a balance between the wire and the disk.
    const file = buildRunExport(run(), { curve: [], trades: [], exportedAt: AT })

    expect(file.run.totals?.realized_pnl).toBe('16400')
    expect(file.run.totals?.unrealized_pnl).toBe('2000')
    expect(file.run.totals?.open_positions).toBe(1)
  })

  it('exports an old run without inventing a split it never recorded', () => {
    const file = buildRunExport(run({ totals: null }), {
      curve: [],
      trades: [],
      exportedAt: AT,
    })

    expect(file.run.totals).toBeNull()
  })

  it('copies the run without editing it', () => {
    // Including `progress`, which is transient. The export's job is fidelity —
    // what the API said about this run at that moment — and a client that
    // dropped a field on the way to disk would be a worse record, not a tidier
    // one.
    const source = run({
      status: 'running',
      finished_at: null,
      metrics: null,
      progress: { bars_done: 125, bars_total: 500, fraction: 0.25, at: '2026-08-20T09:01:00Z' },
    })

    const file = buildRunExport(source, { curve: null, trades: null, exportedAt: AT })

    expect(file.run).toEqual(source)
    expect(file.run.progress?.bars_done).toBe(125)
  })

  it('carries the spec, the warnings and the failure reason', () => {
    const file = buildRunExport(
      run({ status: 'failed', error: 'no bars for SPY', metrics: null, warnings: ['thin sample'] }),
      { curve: null, trades: null, exportedAt: AT },
    )

    expect(file.run.error).toBe('no bars for SPY')
    expect(file.run.warnings).toEqual(['thin sample'])
    expect(file.run.spec.timeframe).toBe('1d')
  })
})

describe('money in the file', () => {
  it('leaves every decimal a string, through a serialisation round trip', () => {
    // The whole point of the export. A value beyond a double's 15-17 significant
    // digits is the proof: if anything on this path parsed it, the digits below
    // could not come back.
    const file = buildRunExport(
      run({ spec: { ...SPEC, starting_cash: '100000.333333333333333333' } }),
      {
        curve: [['2024-01-02T00:00:00Z', '100000.333333333333333333']],
        trades: [{ entry_price: '450.125', net_pnl: '-0.006', qty: '100' }],
        exportedAt: AT,
      },
    )

    const written = JSON.parse(JSON.stringify(file)) as typeof file

    expect(written.run.spec.starting_cash).toBe('100000.333333333333333333')
    expect(written.equity_curve?.[0]?.[1]).toBe('100000.333333333333333333')
    expect(written.trades?.[0]?.net_pnl).toBe('-0.006')
    expect(typeof written.trades?.[0]?.entry_price).toBe('string')
  })

  it('keeps a metric a float, and the same float', () => {
    // The metric set is float statistics, not ledger figures, and stays that
    // way — including the null that means "infinite or undefined", which must
    // not arrive as a zero somebody averages.
    const file = buildRunExport(run({ metrics: { sharpe: 0.1 + 0.2, profit_factor: null } }), {
      curve: null,
      trades: null,
      exportedAt: AT,
    })

    const written = JSON.parse(JSON.stringify(file)) as typeof file

    expect(written.run.metrics?.sharpe).toBe(0.1 + 0.2)
    expect(written.run.metrics?.profit_factor).toBeNull()
  })
})

describe('a result that is absent and one that is empty', () => {
  it('records no result body as null, not as an empty list', () => {
    // `[]` here would claim the run stored an empty curve. It stored none:
    // `RunRepository.fail` clears both columns, and a queued run never wrote
    // them.
    const file = buildRunExport(run({ status: 'failed' }), {
      curve: null,
      trades: null,
      exportedAt: AT,
    })

    expect(file.equity_curve).toBeNull()
    expect(file.trades).toBeNull()
  })

  it('records a stored result that is genuinely empty as an empty list', () => {
    // A finished run that closed no round trip took none. That is a result.
    const file = buildRunExport(run(), { curve: [], trades: [], exportedAt: AT })

    expect(file.trades).toEqual([])
    expect(file.equity_curve).toEqual([])
  })

  it('says which runs have a result body to fetch', () => {
    expect(hasResultBody(run())).toBe(true)
    for (const status of ['queued', 'running', 'failed', 'interrupted']) {
      expect(hasResultBody(run({ status }))).toBe(false)
    }
  })
})

describe('the filename', () => {
  it('names the strategy, the day it was queued and the run', () => {
    expect(runExportFilename(run())).toBe('backtest-sma_crossover-2026-08-20-run-1.json')
  })

  it('ends with the run id, so two runs of one strategy on one day do not collide', () => {
    const first = runExportFilename(run({ id: 'aaa' }))
    const second = runExportFilename(run({ id: 'bbb' }))

    expect(first).not.toBe(second)
    expect(first.endsWith('-aaa.json')).toBe(true)
  })

  it('refuses to put a path separator in a download name', () => {
    // A strategy id is chosen by whoever registered the strategy and arrives
    // here from the database. `../` in a filename offered to a browser is not
    // this module's to pass along.
    const name = runExportFilename(run({ strategy_id: '../../etc/passwd' }))

    expect(name.includes('/')).toBe(false)
    expect(name).toBe('backtest-etc-passwd-2026-08-20-run-1.json')
  })

  it('still names a file when the strategy id is entirely punctuation', () => {
    // Nothing left to name it after, and an empty segment would read as a
    // mangled filename. The run id keeps it unique either way.
    expect(runExportFilename(run({ strategy_id: '///' }))).toBe(
      'backtest-run-2026-08-20-run-1.json',
    )
  })
})
