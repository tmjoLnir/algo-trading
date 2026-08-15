"""Backfill orchestration.

Pure unit tests: the orchestrator talks to the two ports, so a fake provider and
a fake repository exercise every decision it makes without a network or a
database. What is under test is the *ordering* — how the range is sliced, how
symbols are batched, what happens when a window comes back empty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest

from atp_core.data.backfill import (
    BackfillResult,
    backfill_bars,
    iter_windows,
    window_days_for,
)
from atp_core.domain import Bar, Timeframe
from atp_core.errors import DataGapError

if TYPE_CHECKING:
    from collections.abc import Sequence

START = datetime(2024, 1, 1, tzinfo=UTC)


def make_bar(symbol: str, ts: datetime, timeframe: Timeframe = Timeframe.D1) -> Bar:
    return Bar(
        symbol=symbol,
        ts=ts,
        timeframe=timeframe,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("1000"),
    )


class FakeProvider:
    """Records every call, and can be told which symbols have no data."""

    def __init__(self, *, empty: set[str] | None = None, bars_per_window: int = 1) -> None:
        self.empty = empty or set()
        self.bars_per_window = bars_per_window
        self.calls: list[tuple[tuple[str, ...], datetime, datetime, bool]] = []

    async def get_bars(
        self,
        symbols: list[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        adjusted: bool = True,
    ) -> dict[str, list[Bar]]:
        self.calls.append((tuple(symbols), start, end, adjusted))
        missing = [s for s in symbols if s in self.empty]
        if missing:
            # Mirrors the real provider: it raises for the whole call as soon as
            # one requested symbol has nothing.
            raise DataGapError(f"no bars for {missing[0]}")
        return {
            s: [make_bar(s, start + timedelta(hours=i)) for i in range(self.bars_per_window)]
            for s in symbols
        }

    async def get_latest_bar(self, symbol: str, timeframe: Timeframe) -> Bar | None:
        return None


class FakeRepository:
    def __init__(self) -> None:
        self.batches: list[list[Bar]] = []

    async def upsert_bars(self, bars: list[Bar]) -> int:
        self.batches.append(list(bars))
        return len(bars)

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        return []

    async def get_last_n_bars(self, symbol: str, timeframe: Timeframe, n: int) -> list[Bar]:
        return []

    async def find_gaps(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        raise NotImplementedError


async def run(
    provider: FakeProvider,
    repo: FakeRepository,
    *,
    symbols: Sequence[str] = ("SPY",),
    days: int = 10,
    **kwargs: object,
) -> BackfillResult:
    return await backfill_bars(
        provider,
        repo,
        symbols=list(symbols),
        timeframe=Timeframe.D1,
        start=START,
        end=START + timedelta(days=days),
        **kwargs,  # type: ignore[arg-type]
    )


class TestWindowing:
    def test_windows_are_half_open_and_abut(self) -> None:
        got = list(iter_windows(START, START + timedelta(days=10), 4))

        assert got == [
            (START, START + timedelta(days=4)),
            (START + timedelta(days=4), START + timedelta(days=8)),
            (START + timedelta(days=8), START + timedelta(days=10)),
        ]

    def test_final_window_is_clipped_to_end(self) -> None:
        """Never asks for data past the requested range — an overrun would pay
        for bars the caller did not ask for."""
        windows = list(iter_windows(START, START + timedelta(days=10), 7))

        assert windows[-1][1] == START + timedelta(days=10)

    def test_windows_cover_the_range_exactly_once(self) -> None:
        windows = list(iter_windows(START, START + timedelta(days=30), 7))

        assert windows[0][0] == START
        assert windows[-1][1] == START + timedelta(days=30)
        for (_, prev_end), (next_start, _) in pairwise(windows):
            assert prev_end == next_start, "a gap or overlap between windows"

    def test_range_shorter_than_a_window_is_one_window(self) -> None:
        assert list(iter_windows(START, START + timedelta(days=2), 30)) == [
            (START, START + timedelta(days=2))
        ]

    def test_zero_window_is_rejected(self) -> None:
        """A zero-day window would loop forever."""
        with pytest.raises(ValueError, match="at least 1"):
            list(iter_windows(START, START + timedelta(days=1), 0))

    def test_intraday_windows_are_smaller_than_daily(self) -> None:
        """Minute bars are ~400x denser than daily, so the window that bounds
        memory has to be correspondingly shorter."""
        assert window_days_for(Timeframe.M1) < window_days_for(Timeframe.D1)

    def test_every_timeframe_has_a_window(self) -> None:
        for timeframe in Timeframe:
            assert window_days_for(timeframe) >= 1


class TestFetchAndWrite:
    async def test_writes_every_window(self) -> None:
        provider, repo = FakeProvider(), FakeRepository()

        result = await run(provider, repo, days=10, window_days=4)

        assert result.windows == 3
        assert len(provider.calls) == 3
        assert result.bars_written == 3

    async def test_each_window_is_written_before_the_next_is_fetched(self) -> None:
        """Bounds memory, and leaves an interrupted run's completed windows
        durably stored rather than losing the lot."""
        provider, repo = FakeProvider(), FakeRepository()

        await run(provider, repo, days=9, window_days=3)

        assert len(repo.batches) == 3, "one write per window, not one at the end"

    async def test_symbols_are_batched_into_one_request(self) -> None:
        """Alpaca bills per request, not per symbol."""
        provider, repo = FakeProvider(), FakeRepository()

        await run(provider, repo, symbols=("SPY", "QQQ", "IWM"), days=1, window_days=1)

        assert len(provider.calls) == 1
        assert provider.calls[0][0] == ("SPY", "QQQ", "IWM")

    async def test_batches_are_split_at_batch_size(self) -> None:
        provider, repo = FakeProvider(), FakeRepository()

        await run(
            provider, repo, symbols=("A", "B", "C", "D", "E"), days=1, window_days=1, batch_size=2
        )

        assert [call[0] for call in provider.calls] == [("A", "B"), ("C", "D"), ("E",)]

    async def test_duplicate_symbols_are_collapsed(self) -> None:
        provider, repo = FakeProvider(), FakeRepository()

        result = await run(provider, repo, symbols=("SPY", "SPY", "QQQ"), days=1, window_days=1)

        assert result.symbols == ("SPY", "QQQ")

    async def test_adjusted_is_passed_through(self) -> None:
        provider, repo = FakeProvider(), FakeRepository()

        await run(provider, repo, days=1, window_days=1, adjusted=False)

        assert provider.calls[0][3] is False

    async def test_empty_write_is_skipped(self) -> None:
        """No rows means no statement — a backfill over a quiet range should not
        issue empty INSERTs."""
        provider, repo = FakeProvider(bars_per_window=0), FakeRepository()

        await run(provider, repo, days=4, window_days=1)

        assert repo.batches == []


class TestEmptyWindows:
    """A multi-year range contains stretches where a symbol has no data — before
    it listed, while it was halted. Aborting on the first one means never
    backfilling across an IPO."""

    async def test_a_symbol_with_no_data_does_not_abort_the_run(self) -> None:
        provider, repo = FakeProvider(empty={"NEWCO"}), FakeRepository()

        result = await run(provider, repo, symbols=("SPY", "NEWCO"), days=2, window_days=1)

        assert result.bars_written == 2, "SPY still backfilled"
        assert [e.symbol for e in result.empty_windows] == ["NEWCO", "NEWCO"]

    async def test_the_rest_of_a_batch_survives_one_empty_symbol(self) -> None:
        """The provider raises for the whole call, so the batch is re-fetched
        symbol-by-symbol to find out which one it was."""
        provider, repo = FakeProvider(empty={"NEWCO"}), FakeRepository()

        result = await run(provider, repo, symbols=("SPY", "NEWCO", "QQQ"), days=1, window_days=1)

        written = {bar.symbol for batch in repo.batches for bar in batch}
        assert written == {"SPY", "QQQ"}
        assert result.empty_windows[0].symbol == "NEWCO"

    async def test_isolation_costs_extra_requests_only_when_it_happens(self) -> None:
        clean, repo = FakeProvider(), FakeRepository()
        await run(clean, repo, symbols=("SPY", "QQQ"), days=1, window_days=1)
        assert len(clean.calls) == 1

        dirty, repo2 = FakeProvider(empty={"QQQ"}), FakeRepository()
        await run(dirty, repo2, symbols=("SPY", "QQQ"), days=1, window_days=1)
        assert len(dirty.calls) == 3, "the batch, then one per symbol"

    async def test_empty_windows_record_which_window(self) -> None:
        provider, repo = FakeProvider(empty={"NEWCO"}), FakeRepository()

        result = await run(provider, repo, symbols=("NEWCO",), days=2, window_days=1)

        assert [(e.start, e.end) for e in result.empty_windows] == [
            (START, START + timedelta(days=1)),
            (START + timedelta(days=1), START + timedelta(days=2)),
        ]

    async def test_result_is_not_ok_when_a_window_was_empty(self) -> None:
        """The signal a caller chains on — a partial dataset must not read as a
        clean one."""
        provider, repo = FakeProvider(empty={"NEWCO"}), FakeRepository()

        result = await run(provider, repo, symbols=("NEWCO",), days=1, window_days=1)

        assert not result.ok

    async def test_result_is_ok_when_everything_returned_data(self) -> None:
        provider, repo = FakeProvider(), FakeRepository()

        assert (await run(provider, repo, days=3, window_days=1)).ok


class TestInputValidation:
    async def test_no_symbols_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no symbols"):
            await run(FakeProvider(), FakeRepository(), symbols=())

    async def test_inverted_range_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="start must be before end"):
            await backfill_bars(
                FakeProvider(),
                FakeRepository(),
                symbols=["SPY"],
                timeframe=Timeframe.D1,
                start=START + timedelta(days=1),
                end=START,
            )

    async def test_lowercase_symbol_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="uppercase"):
            await run(FakeProvider(), FakeRepository(), symbols=("spy",))

    async def test_zero_batch_size_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            await run(FakeProvider(), FakeRepository(), days=1, window_days=1, batch_size=0)
