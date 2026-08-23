/**
 * One backtest run, as a file.
 *
 * The run panel can hand a reader a `.json` of any single run. This module owns
 * what goes in it; `lib/download.ts` owns handing it to the browser, and
 * `useDownloadBacktest` owns fetching the parts. The split is the usual one in
 * this app — `lib/` is pure and testable without a DOM, hooks do the I/O.
 *
 * **Why a client-assembled file rather than an export endpoint.** Everything in
 * it is already served: the run comes from the list this screen polls, the curve
 * and the trades from the two reads the detail panel makes. A new endpoint would
 * add a fifth read to `apps/api` — which is meant to stay thin (CLAUDE.md §2) —
 * and would still have to be fetched with the session cookie and turned into a
 * blob here, because a plain `<a href>` to the API does not carry a credentialed
 * cross-origin request. The one thing it would buy, a shape declared in the
 * OpenAPI document, is bought instead by `format` below.
 *
 * **Nothing here parses a number.** Money arrives from the API as decimal
 * strings — `starting_cash`, every point on the equity curve, every price, fee
 * and P&L on a trade — and this module never touches them, so what lands on disk
 * is byte-for-byte what the engine computed (rule §1.1). The metric set is float
 * statistics and stays float: `JSON.parse` → `JSON.stringify` round-trips an IEEE
 * double exactly, so the value in the file is the value the server sent even
 * where its shortest text form differs.
 *
 * **The run is copied verbatim, including `progress`.** The export's job is
 * fidelity — what the API said about this run at the moment it was asked — and a
 * client that edited the record on the way to disk would be a worse record.
 * `exported_at` and the run's own `status` are what frame it: an export of a run
 * still in flight is a snapshot of something still moving, and says so.
 */

import type { BacktestOut, BacktestTrade } from '@/api/types'

/**
 * What this file is, written into every file.
 *
 * Versioned because the shape below is a contract the moment somebody writes a
 * script against it, and an unversioned JSON blob gives that script no way to
 * notice it is reading a different document than it was written for.
 */
export const EXPORT_FORMAT = 'atp.backtest-run/1'

export interface BacktestRunExport {
  format: string
  /**
   * When the file was made — not when the run finished, which is
   * `run.finished_at` and is the timestamp that means something about the
   * result.
   */
  exported_at: string
  /** The run exactly as `GET /api/v1/backtests` served it. */
  run: BacktestOut
  /**
   * `null` and `[]` mean different things and the difference is the server's,
   * not a convenience here.
   *
   * `null` is "this run has no result body to export": a queued or running run
   * has not produced one, and `RunRepository.fail` explicitly *clears* the curve
   * and the trades on failure — a partial curve under a failed status is a chart
   * of two of the five years somebody asked about. `[]` is a stored result that
   * is genuinely empty: a finished run that closed no round trip took none,
   * which is a result.
   */
  equity_curve: string[][] | null
  trades: BacktestTrade[] | null
}

/** The parts fetched alongside the run, or nulls when there are none to fetch. */
export interface ExportParts {
  curve: string[][] | null
  trades: BacktestTrade[] | null
  exportedAt: string
}

/**
 * Only a `done` run has a result body.
 *
 * Consulted before the fetch, not after: the two endpoints answer an unfinished
 * run with an empty list rather than a 404, so asking would cost two requests to
 * learn something the status already says — and would record an empty curve as
 * `[]`, claiming the run stored one.
 */
export function hasResultBody(run: Pick<BacktestOut, 'status'>): boolean {
  return run.status === 'done'
}

export function buildRunExport(run: BacktestOut, parts: ExportParts): BacktestRunExport {
  return {
    format: EXPORT_FORMAT,
    exported_at: parts.exportedAt,
    run,
    equity_curve: parts.curve,
    trades: parts.trades,
  }
}

/**
 * Everything a filename is allowed to contain here.
 *
 * A strategy id is chosen by whoever registered the strategy and reaches this
 * function from the database, so it is not the front end's to trust with a path
 * separator: `../` in a download name is a filename the browser should never be
 * offered. Anything outside this set collapses to a single dash.
 *
 * **The dot is not in the set**, which is the part worth stating. Dropping only
 * the separators turns `../..` into `..-..` — no longer a path, and still a
 * filename with two parent references spelled out in it. The one dot that means
 * anything here is the extension, and this function does not write it.
 */
function slug(value: string): string {
  const cleaned = value.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '')
  // A strategy id that was *entirely* punctuation leaves nothing to name the
  // file after. The run id below still makes it unique, so a placeholder is
  // enough — an empty segment would produce `backtest--2026-08-20-…`.
  return cleaned === '' ? 'run' : cleaned
}

/**
 * `backtest-<strategy>-<queued date>-<run id>.json`.
 *
 * The run id is last and is what makes it unique — two runs of one strategy on
 * one day are the normal case, and a downloads folder that silently turns the
 * second into `(1)` has lost which is which. The date is there because a
 * directory of these sorts usefully by name, and `queued_at` is the one
 * timestamp every run has (a queued run has no start and no finish).
 */
export function runExportFilename(run: BacktestOut): string {
  return `backtest-${slug(run.strategy_id)}-${run.queued_at.slice(0, 10)}-${slug(run.id)}.json`
}
