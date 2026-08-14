"""Strategy registry — name → class, for config-driven instantiation."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from atp_core.errors import StrategyError

if TYPE_CHECKING:
    from atp_core.strategy.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}
S = TypeVar("S", bound="type[Strategy]")


def register(cls: S) -> S:
    """Class decorator. Duplicate names are an error, not a silent overwrite —
    two strategies sharing a name would make backtest results ambiguous.

        @register
        class SmaCrossover(Strategy):
            name = "sma_crossover"
    """
    name = getattr(cls, "name", "")
    if not name:
        raise StrategyError(f"{cls.__name__} must set a non-empty `name`")
    if name in _REGISTRY and _REGISTRY[name] is not cls:
        raise StrategyError(f"strategy name {name!r} already registered by {_REGISTRY[name]}")
    _REGISTRY[name] = cls
    return cls


def get(name: str) -> type[Strategy]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise StrategyError(
            f"unknown strategy {name!r}; registered: {sorted(_REGISTRY)}"
        ) from None


def all_strategies() -> dict[str, type[Strategy]]:
    return dict(_REGISTRY)
