"""Alpaca historical provider.

No test here touches the network — `respx` intercepts httpx at the transport
layer (CLAUDE.md §1.7). The payloads below are shaped like real Alpaca
responses, floats and all, because how those floats are parsed is one of the
things being tested.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from atp_core.config import Settings
from atp_core.data.providers.alpaca import AlpacaHistoricalProvider
from atp_core.domain import Timeframe
from atp_core.errors import DataError, DataGapError

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
START = datetime(2024, 1, 2, tzinfo=UTC)
END = datetime(2024, 1, 5, tzinfo=UTC)


def make_settings() -> Settings:
    return Settings(
        alpaca_api_key=SecretStr("test-key-id"),
        alpaca_api_secret=SecretStr("test-secret"),
        alpaca_data_url="https://data.alpaca.markets",
        alpaca_data_feed="iex",
    )


def provider(**kwargs: Any) -> AlpacaHistoricalProvider:
    #: Zero backoff: the retry paths are under test, not the wall clock.
    return AlpacaHistoricalProvider(make_settings(), backoff_base_seconds=0.0, **kwargs)


def bar(day: int, close: float | str = 101.5, *, hour: int = 0) -> dict[str, Any]:
    """One raw Alpaca bar. Numbers are JSON numbers, exactly as they arrive.

    High and low bracket open and close rather than being fixed, so that a test
    picking an arbitrary close still produces a bar `Bar.__post_init__` accepts —
    the fixture should not have to think about the OHLC invariant.
    """
    open_ = 100.25
    close_f = float(close)
    return {
        "t": f"2024-01-{day:02d}T{hour:02d}:00:00Z",
        "o": open_,
        "h": max(open_, close_f),
        "l": min(open_, close_f),
        "c": close,
        "v": 1_000_000,
        "n": 5432,
        "vw": 101.0,
    }


def page(bars: dict[str, list[dict[str, Any]]], token: str | None = None) -> dict[str, Any]:
    return {"bars": bars, "next_page_token": token}


class TestPagination:
    """The reason this class exists. A first page that looks complete is
    indistinguishable from a complete series to everything downstream."""

    @respx.mock
    async def test_follows_next_page_token_to_exhaustion(self) -> None:
        route = respx.get(BARS_URL).mock(
            side_effect=[
                httpx.Response(200, json=page({"SPY": [bar(2)]}, token="page-2")),
                httpx.Response(200, json=page({"SPY": [bar(3)]}, token="page-3")),
                httpx.Response(200, json=page({"SPY": [bar(4)]}, token=None)),
            ]
        )

        result = await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        assert route.call_count == 3
        assert [b.ts.day for b in result["SPY"]] == [2, 3, 4]

    @respx.mock
    async def test_page_token_is_sent_on_subsequent_requests_only(self) -> None:
        route = respx.get(BARS_URL).mock(
            side_effect=[
                httpx.Response(200, json=page({"SPY": [bar(2)]}, token="tok-abc")),
                httpx.Response(200, json=page({"SPY": [bar(3)]})),
            ]
        )

        await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        first, second = route.calls
        assert "page_token" not in first.request.url.params
        assert second.request.url.params["page_token"] == "tok-abc"

    @respx.mock
    async def test_single_page_makes_one_request(self) -> None:
        route = respx.get(BARS_URL).mock(
            return_value=httpx.Response(200, json=page({"SPY": [bar(2)]}))
        )

        await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        assert route.call_count == 1

    @respx.mock
    async def test_pages_merge_across_multiple_symbols(self) -> None:
        respx.get(BARS_URL).mock(
            side_effect=[
                httpx.Response(200, json=page({"SPY": [bar(2)], "QQQ": [bar(2)]}, token="p2")),
                httpx.Response(200, json=page({"SPY": [bar(3)], "QQQ": [bar(3)]})),
            ]
        )

        result = await provider().get_bars(["SPY", "QQQ"], Timeframe.D1, START, END, adjusted=False)

        assert len(result["SPY"]) == 2
        assert len(result["QQQ"]) == 2

    @respx.mock
    async def test_overlapping_pages_are_deduplicated_and_sorted(self) -> None:
        """Page boundaries have been seen to repeat a bar. A duplicate becomes a
        double-counted candle in every indicator computed over it."""
        respx.get(BARS_URL).mock(
            side_effect=[
                httpx.Response(200, json=page({"SPY": [bar(3), bar(2)]}, token="p2")),
                httpx.Response(200, json=page({"SPY": [bar(3), bar(4)]})),
            ]
        )

        result = await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        assert [b.ts.day for b in result["SPY"]] == [2, 3, 4]


class TestDecimalFidelity:
    """CLAUDE.md §1.1. The bug this guards against is invisible: prices that are
    *almost* right, accumulating error into every P&L number downstream."""

    @respx.mock
    async def test_prices_are_exact_decimals_not_floats(self) -> None:
        respx.get(BARS_URL).mock(
            return_value=httpx.Response(200, json=page({"SPY": [bar(2, close=0.1)]}))
        )

        result = await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        close = result["SPY"][0].close
        assert isinstance(close, Decimal)
        # Decimal(0.1) via float would be 0.1000000000000000055511151231257827.
        assert close == Decimal("0.1")
        assert str(close) == "0.1"

    @respx.mock
    async def test_every_price_field_is_decimal(self) -> None:
        respx.get(BARS_URL).mock(return_value=httpx.Response(200, json=page({"SPY": [bar(2)]})))

        b = (await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False))["SPY"][0]

        assert all(
            isinstance(v, Decimal) for v in (b.open, b.high, b.low, b.close, b.volume, b.vwap)
        )
        assert b.trade_count == 5432


class TestAdjustment:
    """Backtest on adjusted, trade on raw (CLAUDE.md §5). A bar must carry both
    or the two can never be reconciled."""

    @staticmethod
    def _by_adjustment(request: httpx.Request) -> httpx.Response:
        adjustment = request.url.params["adjustment"]
        close = 100.0 if adjustment == "raw" else 50.0
        return httpx.Response(200, json=page({"SPY": [bar(2, close=close)]}))

    @respx.mock
    async def test_adjusted_fetches_both_passes_and_keeps_close_raw(self) -> None:
        route = respx.get(BARS_URL).mock(side_effect=self._by_adjustment)

        result = await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=True)

        assert route.call_count == 2
        got = result["SPY"][0]
        assert got.close == Decimal("100.0"), "close must stay raw"
        assert got.adj_close == Decimal("50.0"), "adj_close carries the split-adjusted price"
        sent = {call.request.url.params["adjustment"] for call in route.calls}
        assert sent == {"raw", "all"}

    @respx.mock
    async def test_unadjusted_makes_one_pass_and_leaves_adj_close_unset(self) -> None:
        route = respx.get(BARS_URL).mock(side_effect=self._by_adjustment)

        result = await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        assert route.call_count == 1
        assert route.calls[0].request.url.params["adjustment"] == "raw"
        assert result["SPY"][0].adj_close is None


class TestGaps:
    @respx.mock
    async def test_symbol_with_no_bars_raises_rather_than_returning_empty(self) -> None:
        """Returning [] would let a backtest run over a hole and report a return
        that never existed."""
        respx.get(BARS_URL).mock(return_value=httpx.Response(200, json=page({"SPY": [bar(2)]})))

        with pytest.raises(DataGapError, match="QQQ"):
            await provider().get_bars(["SPY", "QQQ"], Timeframe.D1, START, END, adjusted=False)

    @respx.mock
    async def test_null_bars_payload_raises(self) -> None:
        """Alpaca sends `"bars": null`, not `{}`, for an empty window."""
        respx.get(BARS_URL).mock(
            return_value=httpx.Response(200, json={"bars": None, "next_page_token": None})
        )

        with pytest.raises(DataGapError):
            await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)


class TestRetries:
    @respx.mock
    async def test_retries_429_then_succeeds(self) -> None:
        """The free tier is 200 requests a minute; a backfill will hit it."""
        route = respx.get(BARS_URL).mock(
            side_effect=[
                httpx.Response(429, text="rate limited"),
                httpx.Response(200, json=page({"SPY": [bar(2)]})),
            ]
        )

        result = await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        assert route.call_count == 2
        assert len(result["SPY"]) == 1

    @respx.mock
    async def test_retries_transient_server_errors(self) -> None:
        route = respx.get(BARS_URL).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json=page({"SPY": [bar(2)]})),
            ]
        )

        await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        assert route.call_count == 2

    @respx.mock
    async def test_client_error_is_not_retried(self) -> None:
        """A 422 is our bug — a bad symbol or timeframe. Retrying multiplies it
        against the rate limit and delays the real error."""
        route = respx.get(BARS_URL).mock(return_value=httpx.Response(422, text="invalid symbol"))

        with pytest.raises(DataError, match="422"):
            await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        assert route.call_count == 1

    @respx.mock
    async def test_gives_up_after_max_attempts(self) -> None:
        route = respx.get(BARS_URL).mock(return_value=httpx.Response(503))

        with pytest.raises(DataError, match="attempts"):
            await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        assert route.call_count == 5

    @respx.mock
    async def test_connection_errors_are_retried(self) -> None:
        route = respx.get(BARS_URL).mock(
            side_effect=[
                httpx.ConnectError("connection reset"),
                httpx.Response(200, json=page({"SPY": [bar(2)]})),
            ]
        )

        await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        assert route.call_count == 2


class TestRequestShape:
    @respx.mock
    async def test_credentials_go_in_headers_never_the_url(self) -> None:
        """A key in a query string ends up in access logs and traces
        (CLAUDE.md §1.6)."""
        route = respx.get(BARS_URL).mock(
            return_value=httpx.Response(200, json=page({"SPY": [bar(2)]}))
        )

        await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        request = route.calls[0].request
        assert request.headers["APCA-API-KEY-ID"] == "test-key-id"
        assert request.headers["APCA-API-SECRET-KEY"] == "test-secret"
        assert "test-key-id" not in str(request.url)
        assert "test-secret" not in str(request.url)

    @respx.mock
    async def test_error_message_does_not_leak_credentials(self) -> None:
        respx.get(BARS_URL).mock(return_value=httpx.Response(422, text="bad request"))

        with pytest.raises(DataError) as exc:
            await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        assert "test-secret" not in str(exc.value)
        assert "test-key-id" not in str(exc.value)

    @respx.mock
    @pytest.mark.parametrize(
        ("timeframe", "expected"),
        [
            (Timeframe.M1, "1Min"),
            (Timeframe.M5, "5Min"),
            (Timeframe.M15, "15Min"),
            (Timeframe.M30, "30Min"),
            (Timeframe.H1, "1Hour"),
            (Timeframe.H4, "4Hour"),
            (Timeframe.D1, "1Day"),
        ],
    )
    async def test_timeframe_is_translated_to_alpacas_spelling(
        self, timeframe: Timeframe, expected: str
    ) -> None:
        """An unrecognised spelling is a 400, not a silent fallback."""
        route = respx.get(BARS_URL).mock(
            return_value=httpx.Response(200, json=page({"SPY": [bar(2)]}))
        )

        await provider().get_bars(["SPY"], timeframe, START, END, adjusted=False)

        assert route.calls[0].request.url.params["timeframe"] == expected

    @respx.mock
    async def test_window_and_feed_are_sent(self) -> None:
        route = respx.get(BARS_URL).mock(
            return_value=httpx.Response(200, json=page({"SPY": [bar(2)]}))
        )

        await provider().get_bars(["SPY"], Timeframe.D1, START, END, adjusted=False)

        params = route.calls[0].request.url.params
        assert params["start"] == "2024-01-02T00:00:00Z"
        assert params["end"] == "2024-01-05T00:00:00Z"
        assert params["feed"] == "iex"
        assert params["symbols"] == "SPY"


class TestInputValidation:
    """Bad input fails here rather than as a confusing 400 from Alpaca."""

    async def test_empty_symbols_makes_no_request(self) -> None:
        # No respx mock: any HTTP call at all would raise.
        assert await provider().get_bars([], Timeframe.D1, START, END) == {}

    async def test_naive_start_is_rejected(self) -> None:
        # The naive datetime below is the input under test — DTZ001 is
        # suppressed per-line because making it tz-aware to satisfy the linter
        # would delete the assertion.
        with pytest.raises(ValueError, match="timezone-aware"):
            await provider().get_bars(
                ["SPY"],
                Timeframe.D1,
                datetime(2024, 1, 2),  # noqa: DTZ001
                END,
                adjusted=False,
            )

    async def test_naive_end_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            await provider().get_bars(
                ["SPY"],
                Timeframe.D1,
                START,
                datetime(2024, 1, 5),  # noqa: DTZ001
                adjusted=False,
            )

    async def test_inverted_window_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="start must be before end"):
            await provider().get_bars(["SPY"], Timeframe.D1, END, START, adjusted=False)

    async def test_lowercase_symbol_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="uppercase"):
            await provider().get_bars(["spy"], Timeframe.D1, START, END, adjusted=False)


class TestLatestBar:
    @respx.mock
    async def test_returns_the_most_recent_bar(self) -> None:
        route = respx.get(BARS_URL).mock(
            return_value=httpx.Response(200, json=page({"SPY": [bar(4, close=123.45)]}))
        )

        got = await provider().get_latest_bar("SPY", Timeframe.D1)

        assert got is not None
        assert got.close == Decimal("123.45")
        params = route.calls[0].request.url.params
        assert params["sort"] == "desc", "newest first, so one row is the latest"
        assert params["limit"] == "1"
        assert params["adjustment"] == "raw", "compared against live prices"

    @respx.mock
    async def test_returns_none_when_there_is_no_bar_yet(self) -> None:
        """Unlike get_bars this is not a gap — it is a normal answer before the
        first session."""
        respx.get(BARS_URL).mock(
            return_value=httpx.Response(200, json={"bars": None, "next_page_token": None})
        )

        assert await provider().get_latest_bar("SPY", Timeframe.D1) is None

    async def test_lowercase_symbol_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="uppercase"):
            await provider().get_latest_bar("spy", Timeframe.D1)
