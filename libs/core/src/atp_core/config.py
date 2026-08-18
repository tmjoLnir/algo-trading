"""Typed configuration, loaded from environment / `.env`.

The only place credentials enter the process. Never log a `Settings` instance —
use `settings.redacted()`.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from atp_core.domain.enums import RunMode


class RiskLimits(BaseSettings):
    """Account-wide hard ceilings.

    A strategy may configure something tighter; it can never configure something
    looser. These are the last line of defence before a bug becomes a loss.
    """

    model_config = SettingsConfigDict(env_prefix="RISK_", env_file=".env", extra="ignore")

    max_position_pct: Decimal = Decimal("0.10")
    max_gross_exposure_pct: Decimal = Decimal("1.00")
    max_daily_loss_pct: Decimal = Decimal("0.03")
    max_orders_per_minute: int = 30
    max_open_positions: int = 20
    #: A quote older than this is not a quiet market, it is a dead feed.
    #: Lives here rather than on the rule so an operator can tune it.
    max_quote_age_seconds: int = 30
    #: A fallback, not a recommendation: docs/RISK.md is explicit that a fixed
    #: percentage stop is too tight on a volatile name and too loose on a dull
    #: one, and that ATR-based stops are the default.
    default_stop_loss_pct: Decimal = Decimal("0.02")
    default_take_profit_pct: Decimal = Decimal("0.06")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ── run mode ────────────────────────────────────────────────────────────
    run_mode: RunMode = Field(default=RunMode.PAPER, alias="ATP_RUN_MODE")
    allow_live_trading: bool = Field(default=False, alias="ATP_ALLOW_LIVE_TRADING")
    env: Literal["development", "staging", "production"] = Field(
        default="development", alias="ATP_ENV"
    )
    log_level: str = Field(default="INFO", alias="ATP_LOG_LEVEL")
    log_format: Literal["console", "json"] = Field(default="console", alias="ATP_LOG_FORMAT")

    # ── broker ──────────────────────────────────────────────────────────────
    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_api_secret: SecretStr = SecretStr("")
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_live_base_url: str = "https://api.alpaca.markets"
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_stream_url: str = "wss://stream.data.alpaca.markets/v2/iex"
    alpaca_data_feed: Literal["iex", "sip"] = "iex"

    # ── datastores ──────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://atp:atp@localhost:5432/atp"
    redis_url: str = "redis://localhost:6379/0"

    # ── engine ──────────────────────────────────────────────────────────────
    engine_tick_interval_seconds: int = 60
    dashboard_refresh_seconds: int = 300  # requirement #7

    # ── worker ──────────────────────────────────────────────────────────────
    #: The ingestor's watchlist, comma-separated (`WORKER_SYMBOLS=SPY,QQQ`).
    #: No default universe: subscribing a process to symbols nobody asked for
    #: spends the one market-data connection and writes bars for a watchlist
    #: that was never chosen. Empty means the worker reports that it has nothing
    #: to ingest rather than opening a socket to subscribe to nothing.
    #:
    #: Unaliased, like every setting here that is not one of the four `ATP_`
    #: process switches: the env var is the field name upper-cased. An alias
    #: would also make `Settings(worker_symbols=...)` silently do nothing under
    #: this model's `extra="ignore"`.
    worker_symbols: str = ""
    #: How long the feed may be silent *during a session* before the watchdog
    #: halts trading. Distinct from `risk.max_quote_age_seconds`, which is how
    #: old a quote may be when an order is priced against it: this one is about
    #: a connection that has stopped delivering, and is necessarily the looser
    #: of the two — a symbol can legitimately go a minute without printing.
    worker_max_silence_seconds: int = 60

    #: The strategy this worker trades, by registry name (`WORKER_STRATEGY=
    #: sma_crossover`). **Empty means it places no orders** — the same posture
    #: as an empty `worker_symbols`: a worker that starts trading because it was
    #: deployed, rather than because somebody chose to, is the accident this
    #: default exists to prevent. Ingestion and the schedule run either way.
    worker_strategy: str = ""

    #: Strategy parameters as JSON (`WORKER_STRATEGY_PARAMS={"fast":20}`). Empty
    #: means the strategy's own defaults.
    worker_strategy_params: str = ""

    #: How the runner sizes an order: one of the `PositionSizeSpec` methods and
    #: its value. `risk_pct` with 0.01 is docs/RISK.md's default pair — size so
    #: that hitting the stop loses 1% of equity.
    worker_sizing_method: Literal[
        "fixed_qty", "fixed_notional", "equity_pct", "risk_pct", "volatility_target"
    ] = "risk_pct"
    worker_sizing_value: Decimal = Decimal("0.01")

    #: The protective stop armed on every entry. ATR at 2× is docs/RISK.md's
    #: recommendation over a fixed percentage, which is too tight on a volatile
    #: name and too loose on a dull one.
    worker_stop_type: Literal[
        "fixed_pct", "fixed_amount", "trailing_pct", "atr", "time", "chandelier"
    ] = "atr"
    worker_stop_multiplier: Decimal = Decimal("2")
    worker_stop_period: int = 14

    #: The **third** lock, and it exists only for live. `ATP_RUN_MODE=live` and
    #: `ATP_ALLOW_LIVE_TRADING=true` between them say "this process may trade
    #: real money"; this one says "this worker, specifically, may place the
    #: orders". They are different decisions made by different people at
    #: different times — enabling live mode is a deployment choice, and letting
    #: an unattended loop act on it is an operational one. Paper is unaffected:
    #: `worker_strategy` alone is the opt-in there.
    worker_allow_live_orders: bool = False

    # ── api ─────────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_cors_origins: str = "http://localhost:5173"
    api_secret_key: SecretStr = SecretStr("")

    #: The single operator. There is no users table and deliberately none: this
    #: platform is run by one person, and inventing user CRUD to say so would be
    #: machinery standing in for a requirement nobody has (ADR 0008).
    #:
    #: The password is stored ONLY as a bcrypt hash — never the password itself,
    #: in this file or any other. `scripts/hash_password.py` produces one.
    #: `SecretStr` so a settings dump cannot print it: the hash is not a
    #: credential you can log in with, but it is one you can attack offline.
    api_user: str = "operator"
    api_password_hash: SecretStr = SecretStr("")

    #: How long a login lasts. Short enough that a forgotten open tab is not a
    #: standing key, long enough not to interrupt a trading session — the
    #: dashboard polls every 5 minutes and a re-login mid-incident is exactly
    #: the wrong moment to demand one.
    api_session_hours: int = 12

    #: How many sign-in attempts one client address may make per window before
    #: the endpoint refuses to try. Guessing is the only attack the login
    #: endpoint has; bcrypt's quarter-second verification is a brake and this is
    #: the lock (ADR 0010).
    #:
    #: Counted per address rather than per username on purpose: counting per
    #: username lets anyone who knows the operator's name lock them out of their
    #: own trading platform by failing to log in as them, which turns a
    #: brute-force defence into a denial of service.
    api_login_attempts: int = 10
    api_login_window_seconds: int = 300

    risk: RiskLimits = Field(default_factory=RiskLimits)

    @model_validator(mode="after")
    def _guard_live_trading(self) -> Settings:
        """Two independent locks must both be open before real money moves.

        Rule §1.8. A single flag is one typo — or one careless `-e` on a docker
        run — away from trading a half-finished strategy with real capital.
        """
        if self.run_mode is RunMode.LIVE and not self.allow_live_trading:
            raise ValueError(
                "ATP_RUN_MODE=live requires ATP_ALLOW_LIVE_TRADING=true. "
                "Read docs/SAFETY.md before setting it."
            )
        if self.run_mode is not RunMode.BACKTEST and not self.alpaca_api_key.get_secret_value():
            raise ValueError(f"run_mode={self.run_mode} needs ALPACA_API_KEY")
        return self

    @property
    def broker_base_url(self) -> str:
        """Paper and live are different hosts — this is the whole of what makes
        requirement #5 work (rule: same code path, different endpoint)."""
        return (
            self.alpaca_live_base_url
            if self.run_mode is RunMode.LIVE
            else self.alpaca_paper_base_url
        )

    @property
    def is_live(self) -> bool:
        return self.run_mode is RunMode.LIVE and self.allow_live_trading

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def worker_symbol_list(self) -> list[str]:
        """The watchlist, normalised.

        Upper-cased because `symbol` is always an uppercase ticker here and the
        ingestor rejects anything else — `ATP_WORKER_SYMBOLS=spy` is a shell
        convenience, not a different instrument. De-duplicated in first-seen
        order: a symbol listed twice would otherwise be subscribed twice and
        counted twice against the vendor's symbol limit.
        """
        seen = [s.strip().upper() for s in self.worker_symbols.split(",") if s.strip()]
        return list(dict.fromkeys(seen))

    def redacted(self) -> dict[str, Any]:
        """Safe to log: secrets replaced with '***'."""
        return {
            k: "***" if isinstance(v, SecretStr) else v
            for k, v in self.model_dump(exclude={"risk"}).items()
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Call `get_settings.cache_clear()` in tests."""
    return Settings()
