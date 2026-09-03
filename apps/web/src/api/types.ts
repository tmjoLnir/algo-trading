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
 * `/analytics/live-vs-backtest/{run_id}` — live held up against one stored run.
 *
 * Two thirds of this response is context rather than numbers, and the types
 * carry that shape: `divergence` is `live - backtest` per metric,
 * `comparability` says how far each of those rows can be trusted, and
 * `warnings` says why. A screen that renders the first without the other two
 * reintroduces exactly the misreading the response is built to prevent
 * (docs/ANALYTICS.md).
 *
 * **A `divergence` value of `null` means not available, never zero.** Zero is
 * the strongest claim this report can make — live matched the backtest exactly
 * — so the absence has to render as a dash. The nulls are routine: a stored run
 * nulls its non-finite metrics on the way into the JSON column, and an infinite
 * `profit_factor` is precisely the run somebody holds a live record up against.
 */
export type LiveVsBacktestResponse = Schemas['LiveVsBacktestResponse']
export type LiveSideView = Schemas['LiveSideView']
export type BacktestSideView = Schemas['BacktestSideView']
export type ComparisonWindowView = Schemas['ComparisonWindowView']

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
 * `BacktestOut.totals` is the ledger half of the same run and is the other side
 * of that boundary: every figure in it is a decimal string for `money.ts`,
 * while the identically-named `total_return` inside `metrics` is the float
 * statistic. They come from the same equity computed by the same engine — the
 * difference is the type, and which formatter may touch it. `totals` is null on
 * a run stored before the server recorded them, which is not zero: those runs
 * produced the figures and threw them away.
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
export type BacktestTotalsView = Schemas['BacktestTotalsView']
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

/**
 * `/worker/config` — what the worker trades, and what it is actually running.
 *
 * Two configurations, and the pair is the point. `saved` is the row the form
 * writes; `running` is what the worker published at its last start, or null if
 * none ever has. They differ for as long as nobody restarts the process, and a
 * screen that showed only the first would report settings no process is using.
 *
 * `sizing_value` and `stop_multiplier` are strings for the reason every decimal
 * on this API is: the server sends `Decimal` as a string so a JSON float cannot
 * corrupt it. The form edits them as text and sends them back as text — nothing
 * here calls `parseFloat` on either.
 */
export type WorkerConfigScreen = Schemas['WorkerConfigScreen']
export type WorkerConfigView = Schemas['WorkerConfigView']
export type SavedConfigView = Schemas['SavedConfigView']
export type RunningConfigView = Schemas['RunningConfigView']
export type WorkerOptionsView = Schemas['WorkerOptionsView']
export type StrategyOptionView = Schemas['StrategyOptionView']
export type WorkerOption = Schemas['OptionView']

/**
 * The risk ceilings, which ride in the same screen and the same save.
 *
 * Two names for one model because FastAPI emits two: on the way **out** a
 * `Decimal` is always a string, and on the way **in** it accepts either — so
 * the generated schema splits them, and the split is worth keeping rather than
 * papering over. The form reads `…Payload` and sends `…Input`.
 *
 * **The compiler does not stop a fraction going out as a float.** `…Input`
 * types every fraction as `number | string`, because the server genuinely
 * accepts both — so both halves of that union typecheck and only one of them
 * preserves the value. What holds the rule is `riskPayload` in
 * `WorkerConfigPanel`, and the test that asserts the *string* rather than the
 * number reaches the wire.
 *
 * The counts are numbers in both directions. They are counts, not money.
 */
export type RiskLimitsPayload = Schemas['RiskLimitsPayload-Output']
export type RiskLimitsInput = Schemas['RiskLimitsPayload-Input']

/**
 * One risk entry box and the sentence explaining it, from the server.
 *
 * The prose is not duplicated here for the reason the stop dropdown's is not:
 * docs/RISK.md's argument for a number belongs beside the box it is typed into,
 * and a copy in TypeScript goes stale the first time the argument changes.
 */
export type RiskLimitFieldView = Schemas['LimitFieldView']
