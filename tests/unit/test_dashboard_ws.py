"""The dashboard WebSocket: who gets what, and what happens when a client stops
reading.

The socket is an enhancement and the 5-minute poll is the source of truth, so
nothing here is about delivery guarantees — it is about the two ways a fan-out
can be wrong in a trading UI:

- **a halt that does not arrive**, because the client did not think to subscribe
  to one. `ws.py` promises it reaches every client regardless;
- **one slow client holding up everyone else**, which is how a single browser on
  a bad connection stops the whole dashboard fleet updating.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from atp_api.ws import ConnectionManager, _dispatch, _string_list
from atp_core.channels import CHANNEL_HALTS, CHANNEL_ORDERS, CHANNEL_QUOTES


class FakeSocket:
    """Just enough `WebSocket`. Can be told to hang or to fail."""

    def __init__(self, *, hang: bool = False, error: Exception | None = None) -> None:
        self.accepted = False
        self.sent: list[dict[str, Any]] = []
        self.hang = hang
        self.error = error

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, message: dict[str, Any]) -> None:
        if self.error is not None:
            raise self.error
        if self.hang:
            await asyncio.Event().wait()  # never returns
        self.sent.append(message)

    def types(self) -> list[str]:
        return [m.get("type", "") for m in self.sent]


async def connected(
    manager: ConnectionManager, client_id: str, **subscription: list[str]
) -> FakeSocket:
    socket = FakeSocket()
    await manager.connect(client_id, socket)  # type: ignore[arg-type]
    if subscription:
        manager.subscribe(
            client_id, subscription.get("channels", []), subscription.get("symbols", [])
        )
    return socket


@pytest.fixture
def manager() -> ConnectionManager:
    return ConnectionManager()


class TestHaltsReachEveryone:
    async def test_a_client_that_subscribed_to_nothing_still_gets_a_halt(
        self, manager: ConnectionManager
    ) -> None:
        """A trading halt is not something to opt into. A dashboard that
        filtered one out would show a green screen while nothing was trading."""
        socket = await connected(manager, "a")

        await manager.broadcast("halts", {"type": "halt", "scope": "global"})

        assert socket.types() == ["halt"]

    async def test_a_symbol_scoped_halt_reaches_a_client_watching_other_symbols(
        self, manager: ConnectionManager
    ) -> None:
        """The halt carries a symbol, and symbol filtering must not apply to it:
        an operator watching AAPL still needs to know MSFT was halted, because
        the halt is a fact about the platform rather than about their watchlist.
        """
        socket = await connected(manager, "a", channels=["quotes"], symbols=["AAPL"])

        await manager.broadcast("halts", {"type": "halt", "scope": "symbol", "symbol": "MSFT"})

        assert socket.types() == ["halt"]


class TestSubscriptionFiltering:
    async def test_quotes_go_only_to_subscribers_of_that_symbol(
        self, manager: ConnectionManager
    ) -> None:
        """A dashboard watching five symbols should not receive the universe's
        tick stream."""
        watcher = await connected(manager, "a", channels=["quotes"], symbols=["AAPL"])
        other = await connected(manager, "b", channels=["quotes"], symbols=["MSFT"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL", "bid": "1"})

        assert watcher.types() == ["quote"]
        assert other.sent == []

    async def test_a_client_on_no_channels_gets_no_quotes(self, manager: ConnectionManager) -> None:
        socket = await connected(manager, "a")

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})

        assert socket.sent == []

    async def test_subscribing_to_a_channel_with_no_symbols_means_all_of_them(
        self, manager: ConnectionManager
    ) -> None:
        """Empty is "everything on this channel", not "nothing". Treating it as
        nothing would make the first subscribe deliver silence."""
        socket = await connected(manager, "a", channels=["quotes"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})

        assert socket.types() == ["quote"]

    async def test_a_fill_is_not_symbol_filtered(self, manager: ConnectionManager) -> None:
        """A fill on a symbol you did not subscribe to is still your money."""
        socket = await connected(manager, "a", channels=["fills"], symbols=["AAPL"])

        await manager.broadcast("fills", {"type": "fill", "symbol": "TSLA", "qty": "10"})

        assert socket.types() == ["fill"]

    async def test_symbols_are_upper_cased(self, manager: ConnectionManager) -> None:
        """`aapl` means the same instrument, not a different one."""
        socket = await connected(manager, "a", channels=["quotes"], symbols=["aapl"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})

        assert socket.types() == ["quote"]

    async def test_subscribing_again_adds_rather_than_replaces(
        self, manager: ConnectionManager
    ) -> None:
        """A dashboard subscribes as each panel mounts. A second call that
        dropped the first panel's symbols would leave a table that stops
        updating for no visible reason."""
        socket = await connected(manager, "a", channels=["quotes"], symbols=["AAPL"])
        manager.subscribe("a", ["quotes"], ["MSFT"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})
        await manager.broadcast("quotes", {"type": "quote", "symbol": "MSFT"})

        assert socket.types() == ["quote", "quote"]

    async def test_unsubscribing_stops_that_symbol_only(self, manager: ConnectionManager) -> None:
        socket = await connected(manager, "a", channels=["quotes"], symbols=["AAPL", "MSFT"])
        manager.unsubscribe("a", ["AAPL"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})
        await manager.broadcast("quotes", {"type": "quote", "symbol": "MSFT"})

        assert [m["symbol"] for m in socket.sent] == ["MSFT"]

    async def test_an_unknown_channel_name_is_not_subscribable(
        self, manager: ConnectionManager
    ) -> None:
        """A typo in a client build must not silently create a channel nobody
        publishes to — it would look exactly like a broken producer."""
        socket = await connected(manager, "a", channels=["qoutes"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})

        assert socket.sent == []


class TestOneDeadClientCostsNobodyElse:
    async def test_a_failing_client_is_dropped_and_the_rest_still_receive(
        self, manager: ConnectionManager
    ) -> None:
        broken = FakeSocket(error=RuntimeError("socket is gone"))
        await manager.connect("broken", broken)  # type: ignore[arg-type]
        manager.subscribe("broken", ["quotes"], [])
        healthy = await connected(manager, "healthy", channels=["quotes"])

        await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})

        assert healthy.types() == ["quote"]
        assert manager.client_count == 1

    async def test_a_client_that_stops_reading_is_dropped_on_the_deadline(
        self, manager: ConnectionManager
    ) -> None:
        """Unbounded buffering for one slow reader costs every other client. The
        poll recovers whatever it misses, so dropping is cheap."""
        import atp_api.ws as ws_module

        stalled = FakeSocket(hang=True)
        await manager.connect("stalled", stalled)  # type: ignore[arg-type]
        manager.subscribe("stalled", ["quotes"], [])
        healthy = await connected(manager, "healthy", channels=["quotes"])

        original = ws_module.SEND_TIMEOUT_SECONDS
        ws_module.SEND_TIMEOUT_SECONDS = 0.01
        try:
            await manager.broadcast("quotes", {"type": "quote", "symbol": "AAPL"})
        finally:
            ws_module.SEND_TIMEOUT_SECONDS = original

        assert healthy.types() == ["quote"]
        assert manager.client_count == 1

    async def test_disconnecting_an_unknown_client_is_not_an_error(
        self, manager: ConnectionManager
    ) -> None:
        """The endpoint's `finally` runs even when `connect` never did."""
        manager.disconnect("never-connected")

    async def test_subscribing_a_disconnected_client_is_ignored(
        self, manager: ConnectionManager
    ) -> None:
        """A frame that arrives after the socket closed must not resurrect it as
        an entry nothing will ever clean up."""
        manager.subscribe("ghost", ["quotes"], ["AAPL"])

        assert manager.client_count == 0


class TestTheRedisBridge:
    """`_dispatch` — turning one Redis message into one fan-out."""

    def message(self, channel: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"type": "message", "channel": channel, "data": json.dumps(payload)}

    async def dispatched(self, raw: dict[str, Any], manager: ConnectionManager) -> None:
        """Dispatch and wait for the fan-out it started, if it started one.

        Awaited rather than slept on: a `sleep(0)` yields once, which is not
        enough for a task that itself awaits a gather, and a longer sleep is a
        flaky test waiting for a loaded CI runner.
        """
        task = _dispatch(raw, manager)
        if task is not None:
            await task

    async def test_it_maps_a_redis_channel_to_a_client_channel(
        self, manager: ConnectionManager
    ) -> None:
        """Two vocabularies: the Redis names are internal, the client names are
        a published protocol. A rename on either side must not silently
        unsubscribe every browser."""
        socket = await connected(manager, "a", channels=["fills"])

        await self.dispatched(
            self.message(CHANNEL_ORDERS, {"type": "fill", "symbol": "AAPL"}), manager
        )

        assert socket.types() == ["fill"]

    async def test_a_halt_from_redis_reaches_an_unsubscribed_client(
        self, manager: ConnectionManager
    ) -> None:
        socket = await connected(manager, "a")

        await self.dispatched(
            self.message(CHANNEL_HALTS, {"type": "halt", "scope": "global"}), manager
        )

        assert socket.types() == ["halt"]

    async def test_undecodable_data_is_dropped_not_raised(self, manager: ConnectionManager) -> None:
        """One malformed message must not take the bridge down and with it every
        client's live updates."""
        socket = await connected(manager, "a", channels=["quotes"])

        await self.dispatched(
            {"type": "message", "channel": CHANNEL_QUOTES, "data": "not json"}, manager
        )

        assert socket.sent == []

    async def test_a_json_scalar_is_dropped(self, manager: ConnectionManager) -> None:
        socket = await connected(manager, "a", channels=["quotes"])

        await self.dispatched({"type": "message", "channel": CHANNEL_QUOTES, "data": "42"}, manager)

        assert socket.sent == []

    async def test_an_unmapped_channel_is_dropped(self, manager: ConnectionManager) -> None:
        socket = await connected(manager, "a", channels=["quotes"])

        await self.dispatched(self.message("atp:something:else", {"type": "quote"}), manager)

        assert socket.sent == []


class TestClientFrameParsing:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (["AAPL", "MSFT"], ["AAPL", "MSFT"]),
            (["AAPL", None, 7], ["AAPL"]),
            ("AAPL", []),
            (None, []),
        ],
    )
    def test_non_strings_are_dropped_rather_than_coerced(
        self, value: object, expected: list[str]
    ) -> None:
        """`str(None)` is `"None"`, which would enter a symbol set and quietly
        match nothing forever."""
        assert _string_list(value) == expected
