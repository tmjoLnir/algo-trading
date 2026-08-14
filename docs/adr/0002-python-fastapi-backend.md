# 2. Python + FastAPI backend, React/TypeScript frontend

**Status:** Accepted · 2026-08-14

## Context
The platform needs backtesting, indicator maths, statistics, a real-time
execution loop, and a web dashboard. Candidates: Python throughout, TypeScript
throughout, or a split with a Go gateway.

## Decision
Python 3.12 + FastAPI for the backend and engine; React + TypeScript for the
dashboard. `uv` workspace monorepo.

## Consequences
- The quantitative ecosystem (numpy, pandas, pandas-market-calendars, hypothesis)
  is available without reimplementation — decisive for backtesting.
- FastAPI gives an OpenAPI schema for free, so frontend types are generated
  rather than hand-maintained.
- Two languages: more tooling, and shared types need generation.
- Python's latency floor rules out HFT. Accepted — explicitly out of scope.
- GIL constrains in-process parallelism; strategies scale as processes.

## Alternatives
**Full TypeScript** — one language, but the backtesting and TA libraries are far
weaker and we would write that maths ourselves, which is exactly where subtle
correctness bugs cost money.
**Python + Go gateway** — better latency ceiling, three languages to maintain.
Revisit only if latency becomes a measured problem.
