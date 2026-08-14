# 4. TimescaleDB for market data

**Status:** Accepted · 2026-08-14

## Context
OHLCV bars are the only table that grows without bound: one year of 1-minute
bars for 500 symbols is roughly 50M rows. Everything else is small.

## Decision
Postgres with the TimescaleDB extension. `bars` is a hypertable partitioned on
`ts`; all other tables are ordinary Postgres.

## Consequences
- Time-range queries stay fast at hundreds of millions of rows.
- Native compression gives large savings on old bars.
- One database for both time-series and relational data — no second system to
  operate, back up, or keep consistent.
- Ties us to a Postgres extension; managed Postgres offerings may not have it
  (Timescale Cloud, self-hosted, or AWS RDS with the extension enabled all work).

## Alternatives
**Plain Postgres** — fine to ~10M rows, then range queries degrade. Would work
for daily bars only.
**Parquet on disk/S3** — excellent for backtesting, poor for the point lookups
the live path needs.
**ClickHouse / InfluxDB** — faster for pure time-series, but a second datastore
to run alongside Postgres for everything else.
