"""Venue fees, and the halt they caused.

The platform's cash was a fills-only total. Alpaca's was not — it charges CAT,
REG and TAF and books them on the account activity feed, never on the fill, so
`AlpacaBroker` built every `Fill` with `fee=0`. The two totals could therefore
only ever move apart.

On 2026-09-08 that cost $4.14 in three charges. On 2026-09-09 the worker read
its own stored book (cash 100094.20), asked the venue (cash 100090.06), found
$4.14 against a $1.00 tolerance, halted global trading and crash-looped. This
file is the arithmetic of that morning, and the proof it now reconciles.

The tests that matter are the ones about *exactly once*: a fee applied twice
walks our cash below the venue's, which reads identically to the bug it
replaced and is harder to spot.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from atp_core.brokers.ports import FeeActivity
from atp_core.clock import SimulatedClock
from atp_core.domain import Portfolio, RunMode
from atp_core.execution.fees import DEFAULT_LOOKBACK_DAYS, settle_broker_fees
from atp_core.execution.reconciliation import DiscrepancyKind, Reconciler
from tests.fakes import FakeBroker, FakeFeeLedger, FakeKillSwitch

#: The morning the worker would not start.
NOW = datetime(2026, 9, 9, 9, 15, tzinfo=UTC)
TODAY = NOW.date()

#: Verbatim from the account activity feed for 2026-09-08, sign and all. Three
#: rows, one `activity_type` — `REG` and `TAF` are *sub*-types under `FEE`, and
#: a reader who takes them for activity types asks the venue a question it
#: answers with silence.
DAY_TWO_FEES = [
    FeeActivity(
        activity_id="20260908000000000::da7b17a5-2eb4-4990-858c-ff53b830f8de",
        booked_on=date(2026, 9, 8),
        amount=Decimal("0.01"),
        sub_type="CAT",
        description="CAT fee for proceed of 174 trades on 2026-09-08",
    ),
    FeeActivity(
        activity_id="20260908000000000::97e8ab4e-6a81-42f5-8338-1bd6b5609864",
        booked_on=date(2026, 9, 8),
        amount=Decimal("3.82"),
        sub_type="REG",
        description="REG fee for proceed of $185271.37 on 2026-09-08",
    ),
    FeeActivity(
        activity_id="20260908000000000::6fa7483d-449e-474d-8909-b1bc9a8e029b",
        booked_on=date(2026, 9, 8),
        amount=Decimal("0.31"),
        sub_type="TAF",
        description="TAF fee for proceed of 1572 shares (89 trades) on 2026-09-08",
    ),
]

#: What our snapshot held, and what the venue held, at 09:15:06 on 2026-09-09.
OUR_CASH = Decimal("100094.20")
BROKER_CASH = Decimal("100090.06")


def a_book(cash: Decimal = OUR_CASH) -> Portfolio:
    return Portfolio(cash=cash, starting_equity=cash)


async def settle(
    portfolio: Portfolio, broker: FakeBroker, ledger: FakeFeeLedger, **kwargs: object
) -> object:
    return await settle_broker_fees(
        portfolio,
        broker=broker,
        ledger=ledger,
        run_mode=RunMode.PAPER,
        today=TODAY,
        **kwargs,  # type: ignore[arg-type]
    )


class TestSettling:
    @pytest.mark.asyncio
    async def test_the_venue_s_fees_come_out_of_our_cash(self) -> None:
        """The whole point: $4.14 of charges, $4.14 off the book."""
        broker, ledger, book = FakeBroker(), FakeFeeLedger(), a_book()
        broker.fee_activities = list(DAY_TWO_FEES)

        result = await settle(book, broker, ledger)

        assert result.total == Decimal("4.14")  # type: ignore[attr-defined]
        assert book.cash == BROKER_CASH, "our book now says what the venue says"
        assert book.fees_settled == Decimal("4.14"), "and records that it does"

    @pytest.mark.asyncio
    async def test_a_second_pass_charges_nothing(self) -> None:
        """The feed is re-read on every reconcile — every five minutes, and
        again on each re-read of a disagreement. A charge applied twice walks
        our cash *below* the venue's, which is the same halt wearing a
        different sign."""
        broker, ledger, book = FakeBroker(), FakeFeeLedger(), a_book()
        broker.fee_activities = list(DAY_TWO_FEES)

        await settle(book, broker, ledger)
        again = await settle(book, broker, ledger)

        assert again.is_empty  # type: ignore[attr-defined]
        assert book.cash == BROKER_CASH, "unchanged by the second pass"

    @pytest.mark.asyncio
    async def test_a_fee_feed_that_will_not_answer_is_not_a_halt(self) -> None:
        """A venue outage must not become a refusal to reconcile. The unsettled
        charge stays visible as drift, which is the behaviour that existed
        before this module — degraded to, not worse than."""
        broker, ledger, book = FakeBroker(), FakeFeeLedger(), a_book()
        broker.fee_activities = list(DAY_TWO_FEES)
        broker.reads_fail = True

        result = await settle(book, broker, ledger)

        assert result.is_empty  # type: ignore[attr-defined]
        assert book.cash == OUR_CASH, "nothing applied, and nothing raised"

    @pytest.mark.asyncio
    async def test_a_rebate_credits_rather_than_charging(self) -> None:
        """`amount` is signed, so a correction the venue pays back moves cash
        the other way through the same arithmetic."""
        broker, ledger, book = FakeBroker(), FakeFeeLedger(), a_book()
        broker.fee_activities = [
            FeeActivity(
                activity_id="rebate-1",
                booked_on=date(2026, 9, 8),
                amount=Decimal("-2.00"),
                sub_type="REG",
            )
        ]

        await settle(book, broker, ledger)

        assert book.cash == OUR_CASH + Decimal("2.00")

    @pytest.mark.asyncio
    async def test_a_quiet_venue_still_asks_the_ledger(self) -> None:
        """Changed deliberately from "a quiet venue is not a round trip".

        An empty sweep used to return before consulting the ledger, and that is
        exactly how a book gets stuck: the charges are already recorded, so the
        feed has nothing new to offer, and a settlement that skips the ledger on
        an empty feed would wait forever for a fee that arrived days ago.
        """
        broker, ledger, book = FakeBroker(), FakeFeeLedger(), a_book()

        result = await settle(book, broker, ledger)

        assert result.is_empty  # type: ignore[attr-defined]
        assert ledger.calls == [0], "asked, and told there is nothing owed"

    @pytest.mark.asyncio
    async def test_the_window_reaches_back_past_a_long_weekend(self) -> None:
        """Alpaca stamped 2026-09-08's fees with `created_at` after midnight on
        the 9th, and the API filters on `created_at`. A one-day window would
        already have been too narrow, and a holiday weekend makes it worse — so
        the default is wide, and it costs only a larger response because the
        total is over the ledger rather than over the sweep."""
        broker, ledger, book = FakeBroker(), FakeFeeLedger(), a_book()

        await settle(book, broker, ledger)

        assert broker.fee_queries == [date(2026, 8, 10)]
        assert DEFAULT_LOOKBACK_DAYS >= 7

    @pytest.mark.asyncio
    async def test_a_nonsense_lookback_does_not_ask_about_the_future(self) -> None:
        broker, ledger, book = FakeBroker(), FakeFeeLedger(), a_book()

        await settle(book, broker, ledger, lookback_days=-5)

        assert broker.fee_queries == [TODAY]


class TestRecoveringAnInterruptedSettlement:
    """The failure ADR 0030 could not survive and ADR 0031 exists for.

    On 2026-09-09 a worker recorded all three charges, subtracted $4.14 from an
    in-memory balance, and ended before any snapshot made that cash durable.
    The ledger then said applied; the book had never seen it; and no later run
    could tell the difference, because "applied" was a claim the ledger made
    alone. Every restart read the stale balance, found nothing new, and halted
    on the drift the charges explained.

    Under the derived scheme that state is not special. It is simply a book
    whose `fees_settled` is behind the ledger's total, which is the one
    condition this module exists to close.
    """

    @staticmethod
    async def _ledger_holding_day_two() -> FakeFeeLedger:
        ledger = FakeFeeLedger()
        await ledger.record_seen(DAY_TWO_FEES, run_mode=RunMode.PAPER)
        ledger.calls.clear()
        return ledger

    @pytest.mark.asyncio
    async def test_a_book_that_never_settled_recorded_fees_is_brought_level(self) -> None:
        """The exact state the database was left in: three charges recorded,
        `fees_settled` zero, cash still 100094.20."""
        broker, book = FakeBroker(), a_book()
        ledger = await self._ledger_holding_day_two()
        broker.fee_activities = list(DAY_TWO_FEES)

        result = await settle(book, broker, ledger)

        assert result.total == Decimal("4.14")  # type: ignore[attr-defined]
        assert book.cash == BROKER_CASH
        assert book.fees_settled == Decimal("4.14")

    @pytest.mark.asyncio
    async def test_it_recovers_even_when_the_venue_reports_nothing_now(self) -> None:
        """The charges are days old, so a narrow feed may no longer offer them.
        Recovery must not depend on the venue repeating itself."""
        broker, book = FakeBroker(), a_book()
        ledger = await self._ledger_holding_day_two()

        result = await settle(book, broker, ledger)

        assert result.total == Decimal("4.14")  # type: ignore[attr-defined]
        assert book.cash == BROKER_CASH

    @pytest.mark.asyncio
    async def test_a_settlement_dropped_before_it_persisted_is_simply_redone(self) -> None:
        """The crash itself. A pass settles, the process dies before a snapshot,
        and the next boot reloads the *old* book — same cash, same
        `fees_settled` of zero. Under ADR 0030 that book was unrecoverable;
        here the next pass derives the same correction again."""
        broker, ledger = FakeBroker(), FakeFeeLedger()
        broker.fee_activities = list(DAY_TWO_FEES)
        lost = a_book()
        await settle(lost, broker, ledger)
        assert lost.cash == BROKER_CASH, "settled, then never persisted"

        reloaded = a_book()  # what the next boot reads back
        result = await settle(reloaded, broker, ledger)

        assert result.total == Decimal("4.14")  # type: ignore[attr-defined]
        assert reloaded.cash == BROKER_CASH, "self-healed, with nothing deleted by hand"

    @pytest.mark.asyncio
    async def test_a_book_ahead_of_the_ledger_is_credited_back(self) -> None:
        """The other direction, and it is legitimate: a charge reversed at the
        venue, or an operator removing a row, leaves the book having settled
        more than the venue says it charged. Returning the money is the honest
        response rather than a refusal."""
        broker, ledger, book = FakeBroker(), FakeFeeLedger(), a_book()
        book.fees_settled = Decimal("4.14")
        book.cash = BROKER_CASH

        result = await settle(book, broker, ledger)

        assert result.total == Decimal("-4.14")  # type: ignore[attr-defined]
        assert book.cash == OUR_CASH
        assert book.fees_settled == Decimal(0)


class TestTheHaltOfTheNinth:
    """The regression, both halves. Without the ledger the morning of
    2026-09-09 halts; with it, the same books reconcile."""

    @staticmethod
    def _venue(broker: FakeBroker) -> None:
        broker.equity = BROKER_CASH
        broker.fee_activities = list(DAY_TWO_FEES)

    @pytest.mark.asyncio
    async def test_without_the_ledger_the_books_disagree_by_the_fee_take(self) -> None:
        broker, switch = FakeBroker(), FakeKillSwitch()
        self._venue(broker)
        reconciler = Reconciler(broker, switch, SimulatedClock(NOW), settle_seconds=0)

        report = await reconciler.reconcile(a_book(), known_orders=[])

        assert not report.is_clean
        cash = [d for d in report.discrepancies if d.kind is DiscrepancyKind.CASH]
        assert cash and cash[0].ours == OUR_CASH and cash[0].theirs == BROKER_CASH
        assert switch.engaged, "which is the crash-loop the worker was in"

    @pytest.mark.asyncio
    async def test_with_the_ledger_the_same_morning_reconciles(self) -> None:
        broker, switch = FakeBroker(), FakeKillSwitch()
        self._venue(broker)
        book = a_book()
        reconciler = Reconciler(
            broker,
            switch,
            SimulatedClock(NOW),
            settle_seconds=0,
            fee_ledger=FakeFeeLedger(),
            run_mode=RunMode.PAPER,
        )

        report = await reconciler.reconcile(book, known_orders=[])

        assert report.is_clean, report.explain()
        assert book.cash == BROKER_CASH
        assert not switch.engaged

    @pytest.mark.asyncio
    async def test_a_ledger_without_a_run_mode_is_not_wired_at_all(self) -> None:
        """Both or neither. A ledger scopes its rows by run mode, so one
        without a mode could not tell paper money from real money — and the
        safe reading of a half-configured safety path is the old behaviour,
        not a guess at which account these fees belong to."""
        broker, switch = FakeBroker(), FakeKillSwitch()
        self._venue(broker)
        reconciler = Reconciler(
            broker, switch, SimulatedClock(NOW), settle_seconds=0, fee_ledger=FakeFeeLedger()
        )

        report = await reconciler.reconcile(a_book(), known_orders=[])

        assert not report.is_clean, "unwired, so it reports the drift as before"
