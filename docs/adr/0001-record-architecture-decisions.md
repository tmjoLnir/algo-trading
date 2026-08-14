# 1. Record architecture decisions

**Status:** Accepted · 2026-08-14

## Context
Trading systems accumulate decisions whose reasons are invisible in code — why
next-bar fills, why Decimal, why one worker. Six months on, someone "simplifies"
one and reintroduces a bug that cost real money to find.

## Decision
Record significant architectural decisions as ADRs in `docs/adr/`.

## Consequences
A small overhead per decision. In exchange, the reasoning survives staff
turnover and the author's own memory, and reviewers can point at a document
instead of re-arguing.

## Alternatives
Comments in code (do not scale, get deleted). A wiki (drifts from the code).
