"""The shipped RSI mean-reversion rule set.

Three things are being checked, and only the first is about this file's spec.

- **It is the spec the docs print.** `docs/STRATEGY_AUTHORING.md` shows this
  YAML as the worked example; a reader who pastes it must get a rule set that
  compiles, so the page and the file are pinned to each other here.
- **The trend filter does something.** `close > SMA(200)` is the half of the
  entry that makes this a strategy rather than "buy whatever fell hardest", and
  a filter that never gates anything is a filter nobody would notice was
  broken. `TestTheTrendFilter` drives the same oversold reading in a downtrend
  and asserts silence.
- **The compiler handles a realistic spec.** Everything the compiler was tested
  on in `test_rule_compilation.py` is a two-line fixture. This is the first spec
  with nested groups, two indicator families, a price operand, a 200-bar warmup
  and a risk block, run end to end through the engine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from atp_core.backtest.costs import ZeroCostModel
from atp_core.backtest.engine import (
    BacktestConfig,
    BacktestContext,
    BacktestEngine,
    RiskBasedSizer,
)
from atp_core.clock import SimulatedClock
from atp_core.domain import (
    Bar,
    OrderStatus,
    Portfolio,
    Side,
    SignalAction,
    StopType,
    Timeframe,
)
from atp_core.risk.engine import RiskDecision
from atp_core.risk.stops import StopConfig
from atp_core.strategy import compile_ruleset
from atp_core.strategy.examples.rsi_mean_reversion import (
    RSI_MEAN_REVERSION_YAML,
    rsi_mean_reversion,
)

if TYPE_CHECKING:
    from atp_core.domain import Order, Signal

REPO_ROOT = Path(__file__).resolve().parents[2]
START = datetime(2024, 1, 2, tzinfo=UTC)
CASH = Decimal(100_000)
SYMBOL = "SPY"

#: The spec's own ATR stop, as a run would have to be configured to honour it.
#: A compiled rule set emits no level of its own — the run's `stop_config` owns
#: that (see `compile_ruleset`) — and `risk_pct` cannot size an entry without
#: one, so a run of this spec that omitted this refuses every entry at sizing.
SPEC_STOP = StopConfig(
    stop_type=StopType.ATR, multiplier=Decimal("2.0"), period=14, broker_side=False
)


def bar(index: int, close: float, *, symbol: str = SYMBOL) -> Bar:
    price = Decimal(str(round(close, 2)))
    return Bar(
        symbol=symbol,
        ts=START + timedelta(days=index),
        timeframe=Timeframe.D1,
        open=price - Decimal("0.25"),
        high=price + Decimal("1.5"),
        low=price - Decimal("1.5"),
        close=price,
        volume=Decimal(5_000_000),
    )


def dip_in_an_uptrend(*, symbol: str = SYMBOL) -> list[Bar]:
    """250 bars up, 10 bars sharply down, 25 bars back up.

    Shaped to produce the one state this rule set is looking for and which is
    otherwise hard to reach: RSI(14) deeply oversold *while* the close is still
    well clear of its 200-day average. A plain decline gets the RSI there and
    loses the trend filter on the way; only a sharp dip inside a standing
    uptrend satisfies both.
    """
    prices, price = [], 100.0
    for _ in range(250):
        prices.append(price)
        price += 0.5
    for _ in range(10):
        price -= 3.0
        prices.append(price)
    for _ in range(25):
        price += 3.0
        prices.append(price)
    return [bar(index, close, symbol=symbol) for index, close in enumerate(prices)]


def a_downtrend(*, symbol: str = SYMBOL) -> list[Bar]:
    """A steady decline: RSI pinned at 0, close far below its 200-day average.

    The thing the trend filter exists to refuse. "Oversold" here is not a dip to
    buy, it is the trend.
    """
    return [bar(index, 300.0 - index * 0.5, symbol=symbol) for index in range(285)]


class _AllowAllRisk:
    """The surface `BacktestEngine` uses of a `RiskEngine`, and only that."""

    def anchor_session(self, equity: Decimal) -> int:
        return 1

    def validate(self, order: Order, portfolio: Portfolio) -> RiskDecision:
        return RiskDecision.allow()


def drive(bars: list[Bar], *, symbol: str = SYMBOL) -> list[Signal]:
    """Feed every bar through the compiled spec, with no fills.

    Nothing fills, so the book stays flat and the entry condition is re-offered
    on every bar it holds. That makes this the right harness for *when* the
    condition first becomes true and the wrong one for counting entries — the
    engine below does that.
    """
    strategy = compile_ruleset(rsi_mean_reversion())
    book = Portfolio(cash=CASH, starting_equity=CASH)
    clock = SimulatedClock(START)
    ctx = BacktestContext({symbol: bars}, book, clock, (symbol,))
    strategy.on_start()

    emitted: list[Signal] = []
    for index, current in enumerate(bars):
        clock.set(current.ts)
        ctx.advance(symbol, index)
        emitted.extend(strategy.on_bar(ctx, current))
    return emitted


def run_backtest(bars: list[Bar], *, stop: StopConfig | None = SPEC_STOP) -> Any:
    return BacktestEngine(
        strategy=compile_ruleset(rsi_mean_reversion()),
        config=BacktestConfig(
            symbols=[SYMBOL],
            start=bars[0].ts,
            end=bars[-1].ts,
            timeframe=Timeframe.D1,
            starting_cash=CASH,
        ),
        cost_model=ZeroCostModel(),
        risk_engine=_AllowAllRisk(),
        # The spec's own sizing method, which is the one that needs the stop.
        position_sizer=RiskBasedSizer("risk_pct", Decimal("0.01")),
        stop_config=stop,
    ).run({SYMBOL: bars})


class TestTheShippedSpec:
    def test_it_is_the_spec_the_authoring_guide_prints(self) -> None:
        """Pins the page to the file.

        The guide shows this rule set as its worked example, so a reader can
        paste it — and a paste that no longer validates means the page is
        teaching a spec the platform would reject. Checked on the parts a
        reader would copy rather than on the whole block, which differs in
        whitespace and in the `description` line this file adds.
        """
        doc = (REPO_ROOT / "docs" / "STRATEGY_AUTHORING.md").read_text()

        assert "name: rsi_mean_reversion" in doc
        for fragment in (
            '{left: {indicator: rsi, period: 14}, op: "<", right: {value: 30}}',
            '{left: {price: close}, op: ">", right: {indicator: sma, period: 200}}',
            "position_size: {type: risk_pct, value: 0.01}",
        ):
            assert fragment in doc, f"the guide no longer shows `{fragment}`"

        block = yaml.safe_load(RSI_MEAN_REVERSION_YAML)
        assert block["entry_long"]["all"][0]["right"]["value"] == 30
        assert block["exit"]["any"][0]["right"]["value"] == 55

    def test_it_validates_as_a_rule_set(self) -> None:
        spec = rsi_mean_reversion()
        assert spec.name == "rsi_mean_reversion"
        assert spec.universe == ["SPY", "QQQ", "IWM"]
        assert spec.timeframe is Timeframe.D1
        assert spec.max_concurrent_positions == 3
        assert spec.cooldown_bars == 5

    def test_its_risk_block_is_the_documented_default_pairing(self) -> None:
        """An ATR stop with risk-based sizing — what docs/RISK.md calls the
        default pair, and the reason `required_warmup` counts stop periods."""
        risk = rsi_mean_reversion().risk
        assert risk.stop_loss is not None
        assert risk.stop_loss.type is StopType.ATR
        assert risk.position_size.type == "risk_pct"
        assert risk.position_size.value == Decimal("0.01")

    def test_it_needs_two_hundred_bars_of_warmup(self) -> None:
        """Driven by the SMA(200) trend filter, not by RSI(14) — the longest
        lookback in the tree wins, and here it is the filter rather than the
        signal everyone thinks of as the strategy."""
        assert rsi_mean_reversion().required_warmup == 200

    def test_it_compiles(self) -> None:
        strategy = compile_ruleset(rsi_mean_reversion())
        assert strategy.name == "rsi_mean_reversion"
        assert strategy.warmup_bars == 200

    def test_each_call_returns_its_own_copy(self) -> None:
        """`RuleSet` is not frozen and a compiled strategy keeps a reference to
        the one it was built from, so a shared instance would let one caller's
        edit reach every run in the process."""
        first, second = rsi_mean_reversion(), rsi_mean_reversion()
        assert first is not second
        first.universe.append("TSLA")
        assert second.universe == ["SPY", "QQQ", "IWM"]


class TestTheEntry:
    def test_it_buys_the_dip_inside_an_uptrend(self) -> None:
        emitted = drive(dip_in_an_uptrend())
        entries = [s for s in emitted if s.action is SignalAction.ENTER_LONG]
        assert entries

    def test_the_entry_reason_names_both_halves(self) -> None:
        """Authoring rule 4. A trade taken on two conditions has to say both, or
        a reader cannot tell an oversold buy from a trend-following one."""
        first = next(s for s in drive(dip_in_an_uptrend()) if s.is_entry)
        assert "rsi(14)=" in first.reason
        assert "sma(200)=" in first.reason
        assert "close=" in first.reason

    def test_it_signals_only_once_oversold(self) -> None:
        """Not during the 250-bar climb, which is the whole run up to the dip."""
        first = next(s for s in drive(dip_in_an_uptrend()) if s.is_entry)
        assert first.indicators["rsi(14)"] < 30

    def test_nothing_fires_before_the_two_hundredth_bar(self) -> None:
        """The SMA(200) cannot be computed, so the tree is unknowable rather
        than false — and unknowable must not enter."""
        assert drive(dip_in_an_uptrend()[:150]) == []


class TestTheTrendFilter:
    """`close > SMA(200)` — the half that makes this a strategy.

    Without it this buys whatever has fallen hardest, which in a downtrend is
    buying things on their way to zero. `docs/STRATEGY_AUTHORING.md` lists it
    under *Common mistakes* and it is the one a survivorship-biased universe is
    least likely to expose, since the companies it would have bought into
    oblivion are the ones missing from today's index.
    """

    def test_the_downtrend_fixture_really_is_oversold(self) -> None:
        """Otherwise the silence below proves nothing: a fixture that never
        triggered the RSI leg would pass with the filter deleted."""
        strategy = compile_ruleset(rsi_mean_reversion())
        bars = a_downtrend()
        book = Portfolio(cash=CASH, starting_equity=CASH)
        clock = SimulatedClock(START)
        ctx = BacktestContext({SYMBOL: bars}, book, clock, (SYMBOL,))
        strategy.on_start()
        for index, current in enumerate(bars):
            clock.set(current.ts)
            ctx.advance(SYMBOL, index)
            strategy.on_bar(ctx, current)

        from atp_core.indicators import dispatch

        oversold = dispatch.compute("rsi", bars, 14)
        trend = dispatch.compute("sma", bars, 200)
        assert oversold is not None and oversold < 30
        assert trend is not None and float(bars[-1].close) < trend

    def test_it_refuses_to_buy_a_downtrend(self) -> None:
        assert drive(a_downtrend()) == []


class TestThroughTheEngine:
    def test_a_round_trip_from_the_dip_to_the_bounce(self) -> None:
        result = run_backtest(dip_in_an_uptrend())
        filled = [o for o in result.orders if o.status is OrderStatus.FILLED]
        assert [o.side for o in filled][:2] == [Side.BUY, Side.SELL]

    def test_it_enters_once_rather_than_on_every_oversold_bar(self) -> None:
        """The position check doing its job: the entry condition holds for
        several consecutive bars, and only the first can act on it."""
        result = run_backtest(dip_in_an_uptrend())
        buys = [o for o in result.orders if o.status is OrderStatus.FILLED and o.side is Side.BUY]
        assert len(buys) == 1

    def test_risk_pct_sizing_works_once_the_run_carries_the_specs_stop(self) -> None:
        """The manual step this spec still needs, made visible.

        `compile_ruleset` emits no protective level — the run's `stop_config`
        owns it — so a run of this spec has to be configured with the ATR stop
        the spec asks for. Given it, `risk_pct` sizes; the case below shows what
        happens without it.
        """
        result = run_backtest(dip_in_an_uptrend())
        filled = [o for o in result.orders if o.status is OrderStatus.FILLED]
        assert filled
        assert all(o.filled_qty > 0 for o in filled)

    def test_without_that_stop_every_entry_is_refused_at_sizing(self) -> None:
        """Not silently dropped — booked, with a reason. A spec whose entries
        all vanished would look identical to one that never signalled."""
        result = run_backtest(dip_in_an_uptrend(), stop=None)

        refused = [o for o in result.orders if o.status is OrderStatus.REJECTED_RISK]
        assert refused
        assert "needs a stop" in refused[0].reject_reason
        assert not [o for o in result.orders if o.status is OrderStatus.FILLED]

    def test_the_first_fill_lands_after_the_bar_that_decided_it(self) -> None:
        bars = dip_in_an_uptrend()
        decided_at = next(s for s in drive(bars) if s.is_entry).ts
        index = next(i for i, b in enumerate(bars) if b.ts == decided_at)

        first = next(o for o in run_backtest(bars).orders if o.status is OrderStatus.FILLED)
        assert first.avg_fill_price == bars[index + 1].open
        assert first.avg_fill_price != bars[index].close

    def test_a_downtrend_run_takes_no_trades_at_all(self) -> None:
        result = run_backtest(a_downtrend())
        assert not [o for o in result.orders if o.status is OrderStatus.FILLED]
