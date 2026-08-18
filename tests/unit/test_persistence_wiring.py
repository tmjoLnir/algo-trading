"""What persistence changes about a restart.

The repositories' SQL is exercised against a real database in
`tests/integration/`; nothing here touches one. What is tested here is the
*decision* persistence exists to make possible — that a worker with a stored
book uses it and lets the broker disagree, rather than adopting the broker's
book and calling the resulting agreement a clean reconciliation.

That distinction is the whole point. Before this, every boot adopted, so
reconciliation across a restart could not fail. `docs/FIRST_PAPER_RUN.md` says
a paper week cannot prove reconciliation survives a restart; these tests are
what narrow that caveat to a first boot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from atp_core.domain import Order, OrderStatus, OrderType, Portfolio, Position, RunMode, Side
from atp_worker import trading
from tests.fakes import FakeBroker, FakeOrderRepository, FakePortfolioRepository

NOW = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)


class Reconciler:
    """Just enough reconciler for the boot path: it adopts, and records that."""

    def __init__(self, broker: FakeBroker) -> None:
        self.broker = broker
        self.adopted = 0

    async def adopt_broker_state(self, portfolio: Portfolio) -> None:
        self.adopted += 1
        account = await self.broker.get_account()
        for position in await self.broker.get_positions():
            portfolio.positions[position.symbol] = position
        portfolio.cash = account.cash


def a_position(symbol: str = "SPY", qty: str = "100") -> Position:
    return Position(
        symbol=symbol,
        qty=Decimal(qty),
        avg_entry_price=Decimal("500"),
        last_price=Decimal("500"),
    )


def a_stored_book() -> Portfolio:
    portfolio = Portfolio(cash=Decimal("50000"), starting_equity=Decimal("100000"))
    portfolio.positions["SPY"] = a_position()
    return portfolio


def build() -> tuple[Reconciler, FakePortfolioRepository, FakeBroker]:
    broker = FakeBroker()
    return Reconciler(broker), FakePortfolioRepository(), broker


class TestRestoreOrAdopt:
    @pytest.mark.asyncio
    async def test_a_stored_book_is_used(self) -> None:
        """The case that makes a restart's reconciliation mean something: the
        two views were formed independently, so they can genuinely disagree."""
        reconciler, repo, _ = build()
        repo.stored = a_stored_book()

        portfolio = await trading.restore_or_adopt(reconciler, repo, RunMode.PAPER)  # type: ignore[arg-type]

        assert portfolio.cash == Decimal("50000")
        assert portfolio.positions["SPY"].qty == Decimal("100")
        assert reconciler.adopted == 0, "must not adopt over a book we already have"

    @pytest.mark.asyncio
    async def test_no_stored_book_adopts_the_brokers(self) -> None:
        """A first boot. Nothing exists to disagree with the broker, and a
        worker that started flat while holding positions would double them."""
        reconciler, repo, broker = build()
        broker.hold("SPY", Decimal("250"), Decimal("500"))

        portfolio = await trading.restore_or_adopt(reconciler, repo, RunMode.PAPER)  # type: ignore[arg-type]

        assert reconciler.adopted == 1
        assert portfolio.positions["SPY"].qty == Decimal("250")

    @pytest.mark.asyncio
    async def test_a_stored_book_that_disagrees_is_still_used(self) -> None:
        """Adopting the broker's here would erase the disagreement — which is
        the *finding*, and belongs to the reconciler to halt on."""
        reconciler, repo, broker = build()
        repo.stored = a_stored_book()  # we think 100
        broker.hold("SPY", Decimal("999"), Decimal("500"))  # the venue says 999

        portfolio = await trading.restore_or_adopt(reconciler, repo, RunMode.PAPER)  # type: ignore[arg-type]

        assert portfolio.positions["SPY"].qty == Decimal("100")
        assert reconciler.adopted == 0

    @pytest.mark.asyncio
    async def test_a_read_failure_raises_rather_than_adopting(self) -> None:
        """Adopting because the database blinked would silently discard our own
        book — the one outcome worse than refusing to start."""

        class Broken(FakePortfolioRepository):
            async def latest(self, run_mode: object) -> Portfolio | None:
                raise ConnectionError("database is down")

        reconciler, _, _ = build()

        with pytest.raises(ConnectionError):
            await trading.restore_or_adopt(reconciler, Broken(), RunMode.PAPER)  # type: ignore[arg-type]
        assert reconciler.adopted == 0


class TestTheRunnerPersists:
    """Driven through the runner's own fakes — see test_strategy_runner.py."""

    @staticmethod
    def runner(**kwargs: Any) -> Any:
        from tests.unit.test_strategy_runner import ScriptedStrategy, bar
        from tests.unit.test_strategy_runner import build as build_runner

        return build_runner(ScriptedStrategy(), bars=[bar(0)], **kwargs)

    @pytest.mark.asyncio
    async def test_every_pass_snapshots_the_book(self) -> None:
        repo = FakePortfolioRepository()
        runner, _, _, _, portfolio, _ = self.runner(portfolio_repo=repo)
        await runner.warmup(portfolio)

        await runner.evaluate(portfolio)

        assert len(repo.snapshots) == 1
        assert repo.snapshots[0][0] is not None

    @pytest.mark.asyncio
    async def test_the_snapshot_is_a_copy_not_a_reference(self) -> None:
        """A snapshot that kept changing after it was taken would make every
        restart read whatever the runner did next."""
        repo = FakePortfolioRepository()
        runner, _, _, _, portfolio, _ = self.runner(portfolio_repo=repo)
        await runner.warmup(portfolio)
        await runner.evaluate(portfolio)

        portfolio.cash = Decimal("1")

        assert repo.snapshots[0][1].cash != Decimal("1")

    @pytest.mark.asyncio
    async def test_working_orders_are_restored_before_reconciling(self) -> None:
        """Without this, every order resting at the venue reads as an orphan
        and the first reconciliation of a restart halts on our own orders."""
        stored = Order(
            symbol="SPY",
            side=Side.BUY,
            qty=Decimal("10"),
            order_type=OrderType.MARKET,
            client_order_id="atp-from-before",
        )
        stored.status = OrderStatus.SUBMITTED
        repo = FakeOrderRepository()
        repo.restorable = [stored]

        runner, _, _, reconciler, portfolio, _ = self.runner(order_repo=repo)
        await runner.warmup(portfolio)

        assert [o.client_order_id for o in runner.open_orders] == ["atp-from-before"]
        assert [o.client_order_id for o in reconciler.calls[-1]] == ["atp-from-before"]
