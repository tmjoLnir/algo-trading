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

/** One row of the audit trail, and a page of them. */
export type AuditEntryView = Schemas['AuditEntryView']
export type AuditPage = Schemas['AuditPage']
