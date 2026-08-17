"""Deterministic order identity (CLAUDE.md §1.4).

Every test here is about one of two failures. Either the same intent derives two
keys — the duplicate-position case, where a retry the venue should have
deduplicated becomes a second order — or two different intents derive one key,
where the venue returns the first order for the second submit and a leg the
strategy is relying on silently never trades.

The second is the one that reads as fine in a log.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from atp_core.domain import Side
from atp_core.execution.idempotency import (
    ENTRY,
    EXIT,
    PREFIX,
    STOP_LOSS,
    client_order_id,
    protective_client_order_id,
)

TS = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)


def key(**overrides: object) -> str:
    base: dict[str, object] = {
        "symbol": "SPY",
        "side": Side.BUY,
        "decided_at": TS,
        "strategy_id": "sma_crossover",
        "purpose": ENTRY,
    }
    base.update(overrides)
    return client_order_id(**base)  # type: ignore[arg-type]


class TestOneIntentOneKey:
    def test_the_same_decision_derives_the_same_key(self) -> None:
        """The whole point. Rebuild the order — from the signal, from a row, in
        a fresh process — and the venue sees one order."""
        assert key() == key()

    def test_the_key_survives_a_fresh_interpreter(self) -> None:
        """Guards against an implementation reaching for `hash()`, which is
        salted per process: the retry after a restart is exactly the case this
        module exists for, and `hash()` would break precisely there while
        passing every in-process test."""
        script = (
            "from datetime import UTC, datetime;"
            "from atp_core.domain import Side;"
            "from atp_core.execution.idempotency import client_order_id;"
            "print(client_order_id(symbol='SPY', side=Side.BUY,"
            " decided_at=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),"
            " strategy_id='sma_crossover'))"
        )
        runs = {
            subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                check=True,
                env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            ).stdout.strip()
            for seed in ("0", "1")
        }
        assert len(runs) == 1
        assert runs == {key()}

    def test_the_same_instant_in_another_zone_is_the_same_intent(self) -> None:
        singapore = TS.astimezone(timezone(timedelta(hours=8)))
        assert singapore.hour != TS.hour  # a different wall clock, one instant
        assert key(decided_at=singapore) == key()

    def test_symbol_case_does_not_fork_the_key(self) -> None:
        """`symbol` is an uppercase ticker by convention. A key that differed on
        a lowercase one would hand the venue two orders for one intent."""
        assert key(symbol="spy") == key()


class TestDifferentIntentsDifferentKeys:
    @pytest.mark.parametrize(
        "override",
        [
            {"symbol": "QQQ"},
            {"side": Side.SELL},
            {"strategy_id": "mean_reversion"},
            {"strategy_id": None},
            {"decided_at": TS + timedelta(seconds=1)},
            {"purpose": EXIT},
        ],
    )
    def test_changing_any_component_changes_the_key(self, override: dict[str, object]) -> None:
        assert key(**override) != key()

    def test_an_exit_and_a_short_entry_on_one_bar_are_two_orders(self) -> None:
        """The collision that has no other discriminator. A strategy reversing —
        exit the long, open the short, one bar — emits two SELLs from one
        strategy for one symbol at one instant. Without `purpose` those are one
        key, the venue returns the first order for the second submit, and the
        strategy ends up flat believing it is short."""
        exiting = key(side=Side.SELL, purpose=EXIT)
        entering_short = key(side=Side.SELL, purpose=ENTRY)
        assert exiting != entering_short

    def test_field_boundaries_are_unambiguous(self) -> None:
        """Joined on a separator no field can contain, so ("a", "BC") and
        ("aB", "C") cannot hash to the same key."""
        assert key(strategy_id="a", symbol="BC") != key(strategy_id="aB", symbol="C")


class TestRefusals:
    def test_a_naive_decided_at_is_refused(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            key(decided_at=datetime(2024, 6, 3, 14, 30))  # noqa: DTZ001

    def test_an_empty_symbol_is_refused(self) -> None:
        with pytest.raises(ValueError, match="symbol"):
            key(symbol="")

    def test_an_empty_purpose_is_refused(self) -> None:
        with pytest.raises(ValueError, match="purpose"):
            key(purpose="")


class TestFormat:
    def test_the_key_is_prefixed_and_short_enough_for_any_venue(self) -> None:
        derived = key()
        assert derived.startswith(PREFIX)
        # Alpaca caps client_order_id at 128 characters; length is not the
        # binding constraint, but a key that quietly exceeded one would be
        # rejected at submit time on every order at once.
        assert len(derived) <= 128
        assert derived[len(PREFIX) :].isalnum()


class TestProtectiveChildren:
    def parent(self) -> str:
        return key()

    def test_two_equal_partials_are_two_orders(self) -> None:
        """A 200-share entry filling 100 then 100 is the ordinary case. Keyed on
        the increment, both children are "100" — one key, so the venue returns
        the first stop for the second submit and the second tranche is naked
        while the router books it as protected."""
        first = protective_client_order_id(self.parent(), STOP_LOSS, Decimal(0), Decimal(100))
        second = protective_client_order_id(self.parent(), STOP_LOSS, Decimal(100), Decimal(200))
        assert first != second

    def test_a_top_up_after_a_shrink_is_a_different_order(self) -> None:
        """Keyed on the cumulative total instead, a child shrunk to 60 and the
        40-share top-up that follows both cover "through 100" — the same
        collision from the other direction."""
        placed = protective_client_order_id(self.parent(), STOP_LOSS, Decimal(0), Decimal(100))
        top_up = protective_client_order_id(self.parent(), STOP_LOSS, Decimal(60), Decimal(100))
        assert placed != top_up

    def test_retrying_a_refused_child_is_the_same_order(self) -> None:
        """Four of the nine rules can refuse a protective stop, so the retry is
        an ordinary path — and it must not place a second stop."""
        first = protective_client_order_id(self.parent(), STOP_LOSS, Decimal(0), Decimal(100))
        retry = protective_client_order_id(self.parent(), STOP_LOSS, Decimal(0), Decimal(100))
        assert first == retry

    def test_the_same_quantity_written_two_ways_is_one_order(self) -> None:
        assert protective_client_order_id(
            self.parent(), STOP_LOSS, Decimal("0.00"), Decimal("100.00")
        ) == protective_client_order_id(self.parent(), STOP_LOSS, Decimal(0), Decimal(100))

    def test_children_of_different_entries_never_collide(self) -> None:
        other = key(decided_at=TS + timedelta(minutes=1))
        assert protective_client_order_id(
            self.parent(), STOP_LOSS, Decimal(0), Decimal(100)
        ) != protective_client_order_id(other, STOP_LOSS, Decimal(0), Decimal(100))

    def test_a_child_key_is_not_its_parent(self) -> None:
        assert (
            protective_client_order_id(self.parent(), STOP_LOSS, Decimal(0), Decimal(100))
            != self.parent()
        )

    @pytest.mark.parametrize(
        ("covered_from", "covered_to"),
        [(Decimal(100), Decimal(100)), (Decimal(100), Decimal(50)), (Decimal(-1), Decimal(100))],
    )
    def test_an_empty_or_negative_range_is_refused(
        self, covered_from: Decimal, covered_to: Decimal
    ) -> None:
        with pytest.raises(ValueError, match=r"range|zero"):
            protective_client_order_id(self.parent(), STOP_LOSS, covered_from, covered_to)

    def test_a_child_without_a_parent_is_refused(self) -> None:
        with pytest.raises(ValueError, match="parent"):
            protective_client_order_id("", STOP_LOSS, Decimal(0), Decimal(100))
