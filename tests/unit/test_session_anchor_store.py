"""`RedisSessionAnchorStore` — the key it writes and what an unreadable value does.

The runner-side behaviour is in `test_strategy_runner.py`
(`TestTheSessionIsAnchoredOnFreshMarks`). This is the adapter, against a mocked
client, because what matters here is the key and the refusal to read a
non-number as "not anchored".
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from atp_core.domain import RunMode
from atp_core.persistence.session_anchor import (
    DEFAULT_TTL_SECONDS,
    RedisSessionAnchorStore,
    SessionAnchorUnreadableError,
)

DAY = date(2026, 9, 14)


class TestRedisSessionAnchorStore:
    async def test_it_writes_one_key_per_run_mode_and_session(self) -> None:
        client = AsyncMock()
        await RedisSessionAnchorStore(client).put(RunMode.PAPER, DAY, Decimal("106000.50"))

        client.set.assert_awaited_once_with(
            "atp:risk:session_anchor:paper:2026-09-14", "106000.50", ex=DEFAULT_TTL_SECONDS
        )

    async def test_it_reads_the_anchor_back_as_a_decimal(self) -> None:
        client = AsyncMock()
        client.get.return_value = b"106000.50"

        got = await RedisSessionAnchorStore(client).get(RunMode.PAPER, DAY)

        assert got == Decimal("106000.50")
        client.get.assert_awaited_once_with("atp:risk:session_anchor:paper:2026-09-14")

    async def test_no_key_is_not_anchored(self) -> None:
        client = AsyncMock()
        client.get.return_value = None

        assert await RedisSessionAnchorStore(client).get(RunMode.PAPER, DAY) is None

    @pytest.mark.parametrize("raw", [b"not-a-number", b"NaN", b"Infinity"])
    async def test_an_unreadable_anchor_raises_rather_than_reading_as_none(
        self, raw: bytes
    ) -> None:
        """None would re-anchor fresh, which is the second allowance. The runner
        turns this raise into a closed rule and a page."""
        client = AsyncMock()
        client.get.return_value = raw

        with pytest.raises(SessionAnchorUnreadableError):
            await RedisSessionAnchorStore(client).get(RunMode.PAPER, DAY)

    def test_a_ttl_below_one_second_is_refused(self) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            RedisSessionAnchorStore(AsyncMock(), ttl_seconds=0)
