/**
 * API payload types.
 *
 * These are hand-written PLACEHOLDERS for the skeleton. Once the API is
 * running, generate them instead:
 *
 *     make gen-types      # openapi-typescript → src/api/schema.d.ts
 *
 * and re-export from the generated schema. Hand-maintained duplicates of a
 * server contract drift, and the drift shows up as a runtime undefined in a
 * P&L figure rather than a compile error (CLAUDE.md §4).
 *
 * Note every monetary field is `string`, not `number`: the backend serialises
 * Decimal as a string so JSON's float representation cannot corrupt it in
 * transit. Parse with a decimal library for arithmetic; never `parseFloat` a
 * balance and add to it.
 */

export interface AccountView {
  equity: string
  cash: string
  buying_power: string
  gross_exposure: string
  net_exposure: string
  leverage: string
  day_pnl: string
  day_pnl_pct: string
  open_position_count: number
}

export interface PositionView {
  symbol: string
  qty: string
  avg_entry_price: string
  last_price: string
  market_value: string
  unrealized_pnl: string
  unrealized_pnl_pct: string
  stop_loss_price: string | null
  take_profit_price: string | null
  distance_to_stop_pct: string | null
  strategy_id: string | null
  strategy_name: string | null
  opened_at: string
}

export interface SignalView {
  id: string
  ts: string
  strategy_name: string
  symbol: string
  action: string
  reason: string
  indicators: Record<string, number>
  acted_on: boolean
  rejection_reason: string | null
}

export interface OrderView {
  id: string
  ts: string
  symbol: string
  side: string
  order_type: string
  qty: string
  filled_qty: string
  limit_price: string | null
  avg_fill_price: string | null
  status: string
  strategy_name: string | null
}

export interface HaltView {
  scope: string
  reason: string
  engaged_at: string
  engaged_by: string
  detail: string
  target: string | null
}

export interface LiveDashboard {
  as_of: string
  run_mode: 'backtest' | 'paper' | 'live'
  market_open: boolean
  account: AccountView
  positions: PositionView[]
  recent_signals: SignalView[]
  working_orders: OrderView[]
  active_halts: HaltView[]
  refresh_seconds: number
  data_feed_healthy: boolean
  last_data_at: string | null
}
