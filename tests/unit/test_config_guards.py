"""The live-trading guards. These must never be relaxed to make a test pass."""

from __future__ import annotations

import pytest


def test_live_mode_requires_second_flag() -> None:
    """`ATP_RUN_MODE=live` alone must raise (rule §1.8). One flag is one typo
    away from trading a half-finished strategy with real capital."""
    pytest.skip("TODO: construct Settings(run_mode=live, allow_live_trading=False)")


def test_default_run_mode_is_paper() -> None:
    pytest.skip("TODO")


def test_paper_and_live_use_different_urls() -> None:
    pytest.skip("TODO")


def test_redacted_settings_hide_secrets() -> None:
    """No credential may reach a log sink."""
    pytest.skip("TODO")
