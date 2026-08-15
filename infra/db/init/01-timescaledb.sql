-- Runs once on first container start.
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- That is deliberately all this file does.
--
-- The `bars` hypertable, its chunk interval and its compression policy live in
-- the initial Alembic migration, because the table has to exist before it can
-- be partitioned and because keeping schema in migrations means one source of
-- truth for it. The extension is different: it is a property of the database,
-- not of the schema, and creating it needs privileges a migration should not
-- assume it has.
--
-- The migration checks for this extension and refuses to run without it rather
-- than quietly creating `bars` as an ordinary table. Anything that provisions a
-- database outside compose — a CI service container, a managed instance — has
-- to create the extension itself; see tests/integration/test_migrations.py,
-- which does exactly that before migrating.
