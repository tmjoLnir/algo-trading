/**
 * API payload types — aliases over the GENERATED schema.
 *
 * These used to be hand-written placeholders. They are not any more:
 * `src/api/schema.d.ts` is produced from the server's own OpenAPI document by
 *
 *     make gen-types
 *
 * which dumps the schema straight from the FastAPI app (no running server
 * needed) and runs `openapi-typescript` over it. Re-run it whenever a response
 * model changes and commit the result.
 *
 * The point of the indirection is that a hand-maintained duplicate of a server
 * contract drifts, and the drift shows up as a runtime `undefined` inside a P&L
 * figure rather than as a compile error (CLAUDE.md §4). Everything below is a
 * name, not a definition — if one of these stops compiling, the server changed
 * and the components that read it need to change too. That is the alarm
 * working.
 *
 * Note every monetary field is `string`, not `number`: the backend serialises
 * `Decimal` as a string so JSON's float representation cannot corrupt it in
 * transit. Never `parseFloat` a balance — see `src/lib/money.ts`, which formats
 * these for display without ever making one a number.
 */

import type { components } from './schema'

type Schemas = components['schemas']

export type AccountView = Schemas['AccountView']
export type PositionView = Schemas['PositionView']
export type SignalView = Schemas['SignalView']
export type OrderView = Schemas['OrderView']
export type HaltView = Schemas['HaltView']
export type LiveDashboard = Schemas['LiveDashboard']
export type EquityCurveView = Schemas['EquityCurveView']
export type EquityPointView = Schemas['EquityPointView']

/**
 * The run modes the UI branches on.
 *
 * The generated type is a bare `string` — FastAPI serialises the enum's value
 * and does not narrow it — so this is the one place the union is restated. It
 * is a display concern (which banner to show), not a contract: an unrecognised
 * mode falls through to the loudest branch rather than to none.
 */
export type RunMode = 'backtest' | 'paper' | 'live'

/** Who the session belongs to (`/auth/me`, `/auth/login`). */
export type WhoAmI = Schemas['WhoAmI']

/** What the login screen may know before there is a session. */
export type PreSessionContext = Schemas['PreSessionContext']

/**
 * `/risk/rejections` — the decisions the risk chain refused.
 *
 * `RejectionView` is a *signal*, not an order: a refused signal never becomes
 * an order, so the orders table cannot show it. `indicators` are strings for
 * the reason every decimal on the wire is one.
 */
export type RejectionView = Schemas['RejectionView']
export type RejectionsResponse = Schemas['RejectionsResponse']

/**
 * The rungs of the promotion ratchet, straight from the server's own enum.
 *
 * Generated, so `useStrategies.ts` can be *checked* against it rather than
 * hand-maintaining a parallel list. It used to hand-maintain one, and it drifted
 * — the filter offered `backtest` and `active`, neither of which
 * `StrategyState` has ever contained, and omitted `live` and `halted`.
 */
export type StrategyState = Schemas['StrategyState']

/**
 * The risk read endpoints (`/risk/limits`, `/risk/status`).
 *
 * `LimitUsageView.current` and `.ceiling` are decimal strings even where the
 * limit is a count, because one column of a table cannot change type per row —
 * `unit` says how to read the pair. They go through `src/lib/money.ts` like
 * every other decimal string: never `parseFloat`.
 */
export type RiskLimitsView = Schemas['RiskLimitsView']
export type LimitUsageView = Schemas['LimitUsageView']
export type RiskStatusView = Schemas['RiskStatusView']

/** One row of the audit trail, and a page of them. */
export type AuditEntryView = Schemas['AuditEntryView']
export type AuditPage = Schemas['AuditPage']

/**
 * The analytics endpoints — history, not the live book.
 *
 * `TradeView` is all decimal strings and goes through `src/lib/money.ts`.
 * `PerformanceResponse.metrics` is a bag of JSON numbers and goes through
 * `src/lib/stats.ts` — the split is explained at the top of that file.
 */
export type TradeView = Schemas['TradeView']
export type TradesResponse = Schemas['TradesResponse']
export type PerformanceResponse = Schemas['PerformanceResponse']
export type AttributionRowView = Schemas['AttributionRowView']
export type AttributionResponse = Schemas['AttributionResponse']

/**
 * The order history (`/orders`).
 *
 * `OrderHistoryView` is not `OrderView`: the latter is the *working* order the
 * worker published in the live book, and this one is read from the order table
 * and has to describe a finished order too — including the ones that never
 * filled, which is what the screen exists for.
 */
export type OrderHistoryView = Schemas['OrderHistoryView']
export type OrdersResponse = Schemas['OrdersResponse']

/**
 * The stored book (`/positions`) — the copy the worker wrote to Postgres, as
 * opposed to the one it published to Redis for `/dashboard/live`. Same
 * `PositionView` rows, because both are built from the same
 * `atp_core.dashboard` expressions; what differs is the source and the age.
 */
export type StoredBookView = Schemas['StoredBookView']

/**
 * Strategies (`/strategies`) — the stored rows and the registered classes in
 * one response, because the useful fact is the difference between them.
 */
export type StoredStrategyView = Schemas['StoredStrategyView']
export type AvailableStrategyView = Schemas['AvailableStrategyView']
export type StrategiesResponse = Schemas['StrategiesResponse']

/**
 * Backtests (`/backtests`) — the only screen in this app that *starts* work.
 *
 * `BacktestOut.metrics` is a bag of JSON numbers and goes through
 * `src/lib/stats.ts`, exactly like `PerformanceResponse.metrics` and for the
 * same reason: these are float statistics over a return series, not ledger
 * figures. A null inside it means the metric was infinite or undefined — a
 * profit factor with no losing trade — and renders as `—`, never as zero.
 *
 * `BacktestSpecView.starting_cash` and `qty` are decimal **strings**, as is
 * every value on the equity curve and every money field on a trade. Those go
 * through `money.ts`, which accepts only strings.
 *
 * `BacktestTradesResponse.trades` is deliberately untyped by the server — the
 * rows are `TradeRecord`s serialised generically, so the schema carries them as
 * a JSON object. `BacktestTrade` below is this app's reading of that shape, and
 * it is the one place in the front end that describes a payload the OpenAPI
 * document does not: it is a hand-written type by necessity, so every field is
 * optional and nothing here assumes one is present.
 */
export type BacktestOut = Schemas['BacktestOut']
export type BacktestSpecView = Schemas['BacktestSpecView']
export type BacktestProgressView = Schemas['BacktestProgressView']
export type BacktestListResponse = Schemas['BacktestListResponse']
export type BacktestTradesResponse = Schemas['BacktestTradesResponse']
export type BacktestEquityCurveResponse = Schemas['BacktestEquityCurveResponse']
export type BacktestComparisonResponse = Schemas['BacktestComparisonResponse']
export type BacktestRequest = Schemas['BacktestRequest']

/**
 * One reconstructed round trip from a backtest.
 *
 * The same shape `/analytics/trades` serves for a live trade — the same fold
 * produces both (`PerformanceAnalyzer.build_trades`) — which is what makes the
 * two comparable. Written by hand because the server serialises it as an opaque
 * JSON object rather than a declared model, so treat every field as absent until
 * proven otherwise.
 */
export interface BacktestTrade {
  trade_id?: string
  symbol?: string
  side?: string
  entry_ts?: string
  exit_ts?: string | null
  entry_price?: string
  exit_price?: string | null
  qty?: string
  net_pnl?: string
  fees?: string
  return_pct?: string
  holding_period_hours?: number
  exit_reason?: string
}
