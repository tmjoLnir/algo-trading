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

    # ── api ─────────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_cors_origins: str = "http://localhost:5173"
    api_secret_key: SecretStr = SecretStr("")

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
