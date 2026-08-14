-- Runs once on first container start.
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- The hypertable itself is created by an Alembic migration, after the table
-- exists. Keeping schema in migrations and only the extension here means one
-- source of truth for the schema.
--
-- The migration should run:
--
--   SELECT create_hypertable('bars', 'ts', chunk_time_interval => INTERVAL '7 days');
--
--   ALTER TABLE bars SET (
--     timescaledb.compress,
--     timescaledb.compress_segmentby = 'symbol, timeframe'
--   );
--   SELECT add_compression_policy('bars', INTERVAL '30 days');
--
-- 7-day chunks suit minute bars; use 30 days if you only store dailies.
-- Compression after 30 days typically gives 10-20x on OHLCV, and old bars are
-- read for backtests but never updated.
