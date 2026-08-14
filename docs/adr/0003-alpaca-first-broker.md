# 3. Alpaca as the first broker adapter

**Status:** Accepted · 2026-08-14

## Context
Requirement #5 needs paper trading on live data. Options: Alpaca, Interactive
Brokers, crypto exchanges via CCXT, or broker-agnostic with only a simulator.

## Decision
Alpaca (US equities) as the first `BrokerPort` adapter, behind the port from day
one.

## Consequences
- Paper trading is a **separate endpoint with the same API**, so requirement #5
  is satisfied by configuration rather than by a simulator we would have to
  build and then trust.
- Free real-time data (IEX) and a simple REST/WS API — fastest path to a working
  end-to-end loop.
- Limited to US equities and crypto. No SG/HK equities, no futures, no options.
- The free IEX feed is ~2-3% of consolidated volume; SIP requires a paid
  subscription. Fills in paper will differ from a SIP-fed live account.
- Because everything is behind `BrokerPort`, adding IBKR later is one adapter.

## Alternatives
**IBKR** — multi-asset and multi-region, but the TWS/Gateway API requires a
running desktop gateway, which complicates containerised deployment
substantially. Worth adding later for SG/HK coverage.
**Crypto/CCXT** — 24/7, no market-hours complexity, but a different asset class
than intended.
**Simulator only** — no rewrite risk, but no path to a real trade.
