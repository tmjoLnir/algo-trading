"""Nothing anywhere still reaches for the risk ceilings where they used to live.

This module exists because moving `RiskLimits` out of `atp_core.config` broke two
files that nothing was watching, and both were found by *running* the code rather
than by any check in this repository:

- `infra/alembic/env.py` still did `from atp_core.config import RiskLimits`, so
  `make migrate` died on an `ImportError` before it reached a database. The one
  test that would have caught it, `tests/integration/test_alembic_env.py`, shells
  out to alembic and is marked `integration`, so `make check` never runs it.
- `scripts/preflight.py` still read `settings.risk.max_quote_age_seconds`, which
  is an `AttributeError` at run time on the quote-freshness check.

The common cause is a coverage hole rather than carelessness: `make typecheck`
runs `mypy libs apps tests`, and **`scripts/` and `infra/` are outside it**. Both
directories import `atp_core` freely — eleven files do — so a symbol that moves
in `libs/core` can break them silently, and `make check` stays green.

Closing that hole properly means putting `scripts` and `infra` under mypy, which
is worth doing and is not this test: there are ten pre-existing errors in files
unrelated to any of this, and fixing them belongs in its own change. What this
does instead is pin the *specific* hazard that just cost two bugs, over the whole
tree rather than over the part mypy happens to look at.

**Parsed, not grepped.** A regex over the source would fire on the several
comments that legitimately say "this used to be `settings.risk`" — the history is
worth keeping, so the check has to be able to tell a sentence from an expression.
`ast` can: it sees attribute access and import statements and never sees a
comment or a docstring at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Where Python that imports `atp_core` actually lives. `infra/` and `scripts/`
#: are the two nobody typechecks, and therefore the two that matter most here.
SEARCHED = ("libs", "apps", "scripts", "infra", "tests")

#: This file names both patterns in its own prose and would otherwise be the one
#: thing it reports.
SELF = Path(__file__).name


def python_files() -> list[Path]:
    out: list[Path] = []
    for top in SEARCHED:
        for path in (ROOT / top).rglob("*.py"):
            if path.name == SELF or "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            out.append(path)
    return sorted(out)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class TestTheCeilingsAreNoLongerOnSettings:
    """`Settings.risk` is gone, so reading it is an `AttributeError` at run time.

    Not a type error anybody would see: pydantic models raise on unknown
    attributes at access time, so a line like this survives import, survives
    every unit test that does not execute it, and fails in front of an operator.
    """

    def test_no_module_reads_settings_dot_risk(self) -> None:
        offenders: list[str] = []
        for path in python_files():
            for node in ast.walk(_tree(path)):
                # `<anything>.risk` where the thing on the left is spelled
                # `settings` — the shape every one of the old call sites had,
                # whether that was a local, `ctx["settings"]` is excluded by
                # being a subscript, or `self.settings`.
                if not (isinstance(node, ast.Attribute) and node.attr == "risk"):
                    continue
                base = node.value
                name = (
                    base.id
                    if isinstance(base, ast.Name)
                    else base.attr
                    if isinstance(base, ast.Attribute)
                    else None
                )
                if name == "settings":
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        assert not offenders, (
            "these read `settings.risk`, which no longer exists — the ceilings are "
            f"columns on the worker_config row (ADR 0025): {offenders}"
        )

    def test_settings_really_has_no_risk_attribute(self) -> None:
        """The premise of the test above, asserted rather than assumed.

        If `Settings` ever grows a `risk` field again, the scan becomes a rule
        against something legal and should be deleted rather than worked around.
        """
        from atp_core.config import Settings

        assert "risk" not in Settings.model_fields


class TestTheClassIsNoLongerImportableFromConfig:
    """`from atp_core.config import RiskLimits` is an `ImportError` at import.

    Which is the *loud* half of the pair and still went unnoticed, because the
    file it broke — alembic's `env.py` — is executed by a Makefile target and by
    an integration test, and by nothing that `make check` runs.
    """

    def test_no_module_imports_risklimits_from_config(self) -> None:
        offenders: list[str] = []
        for path in python_files():
            for node in ast.walk(_tree(path)):
                if not isinstance(node, ast.ImportFrom) or node.module != "atp_core.config":
                    continue
                for alias in node.names:
                    if alias.name == "RiskLimits":
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        assert not offenders, (
            "these import `RiskLimits` from `atp_core.config`, where it no longer is — "
            f"it lives in `atp_core.risk.limits` (ADR 0025): {offenders}"
        )

    def test_the_old_import_really_fails(self) -> None:
        with pytest.raises(ImportError):
            from atp_core.config import RiskLimits  # type: ignore[attr-defined]  # noqa: F401

    def test_the_new_import_works(self) -> None:
        from atp_core.risk.limits import RiskLimits

        assert RiskLimits().max_position_pct


class TestTheScanCanActuallyFail:
    """A guard that cannot fail is worse than no guard: it reports the property
    as held. Both scans are run against source that does contain the pattern."""

    def test_the_attribute_scan_catches_a_planted_read(self, tmp_path: Path) -> None:
        planted = tmp_path / "planted.py"
        planted.write_text("budget = settings.risk.max_quote_age_seconds\n")
        hits = [
            node
            for node in ast.walk(_tree(planted))
            if isinstance(node, ast.Attribute)
            and node.attr == "risk"
            and isinstance(node.value, ast.Name)
            and node.value.id == "settings"
        ]
        assert len(hits) == 1

    def test_the_import_scan_catches_a_planted_import(self, tmp_path: Path) -> None:
        planted = tmp_path / "planted.py"
        planted.write_text("from atp_core.config import RiskLimits, Settings\n")
        hits = [
            alias
            for node in ast.walk(_tree(planted))
            if isinstance(node, ast.ImportFrom) and node.module == "atp_core.config"
            for alias in node.names
            if alias.name == "RiskLimits"
        ]
        assert len(hits) == 1

    def test_the_scan_ignores_prose(self, tmp_path: Path) -> None:
        """The reason this is `ast` and not a regex: several files still explain
        that a value "used to be `settings.risk`", and that history is worth
        keeping."""
        planted = tmp_path / "planted.py"
        planted.write_text(
            '"""A docstring saying settings.risk once existed."""\n# settings.risk\n'
        )
        hits = [n for n in ast.walk(_tree(planted)) if isinstance(n, ast.Attribute)]
        assert hits == []


def test_every_searched_directory_exists() -> None:
    """A typo in `SEARCHED` would silently scan nothing and pass."""
    for top in SEARCHED:
        assert (ROOT / top).is_dir(), top
    assert len(python_files()) > 100
