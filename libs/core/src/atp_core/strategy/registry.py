"""Strategy registry — name → class, for config-driven instantiation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from atp_core.errors import StrategyError

if TYPE_CHECKING:
    from atp_core.strategy.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}
S = TypeVar("S", bound="type[Strategy]")


def register[S: "type[Strategy]"](cls: S) -> S:
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
        raise StrategyError(f"unknown strategy {name!r}; registered: {sorted(_REGISTRY)}") from None


def all_strategies() -> dict[str, type[Strategy]]:
    return dict(_REGISTRY)


def default_params(cls: type[Strategy]) -> dict[str, Any]:
    """The parameters a class runs on when nobody supplies any.

    `Strategy.__init__` stores `params or {}` and every accessor reads
    `self.params.get(name, default)`, so the defaults live in `params_schema`
    and nowhere else. That is fine for a running strategy and wrong for a stored
    row: writing `{}` into `strategies.params` records that the strategy was
    configured with nothing, when what actually happened is that it ran on 20
    and 50. A reader — the backtest form, most immediately — cannot tell those
    apart, and the second one is the truth.

    Read from the class rather than from an instance, deliberately: a `Strategy`
    validates its params at construction, so building one to ask what its
    defaults are would fail for exactly the classes whose required params make
    the question interesting. The same reasoning as `_available` in the
    strategies router.

    Properties with no `default` are omitted rather than given a `None` — the
    schema is saying the value must be supplied, and a null would be this
    function inventing one.
    """
    properties = cls.params_schema.get("properties", {})
    if not isinstance(properties, dict):
        return {}
    return {
        name: spec["default"]
        for name, spec in properties.items()
        if isinstance(spec, dict) and "default" in spec
    }
