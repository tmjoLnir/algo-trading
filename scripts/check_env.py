#!/usr/bin/env python3
"""Name the value in `.env` that stops this platform starting.

`Settings` refuses to validate a configuration it cannot trust, and the API
builds its app at import — so one bad value in `.env` is a process that will not
run. Since #84 that is an exit code and a climbing restart count rather than a
container idling behind a live reloader, which is the difference between a fault
you can see and one you cannot.

Seeing it still left the operator a translation to do. The traceback in
`docker compose logs api` names a *field*:

    ValidationError: 1 validation error for RiskLimits
    max_position_pct
      Input should be a valid decimal [input_value='not-a-number']

and `max_position_pct` is not in `.env`. `RISK_MAX_POSITION_PCT` is. This prints
the second name, the line it is on, and what is wrong with it — for every broken
value at once rather than one per edit-and-retry.

**It runs without the platform.** No container, no database, no network: it reads
the same `.env` through the same `Settings` and reports what happens. That
matters because the situation it is for is one where nothing else starts,
including `scripts/preflight.py` and `scripts/status.py` — both of which call
`get_settings()` and, until this existed, died with the same traceback they were
being run to explain.

Secrets are never printed. A value that fails to load is still a credential, and
most of one is still worth grinding offline (CLAUDE.md §1.6) — so a problem on a
`SecretStr` field reports the variable and the reason and withholds the value,
and anything it cannot classify is withheld too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from atp_core.config import ConfigProblem, config_problems

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"


def env_file_lines(path: Path = ENV_FILE) -> dict[str, int]:
    """`{KEY: line number}` for every assignment in `.env`, or `{}`.

    Parsed here rather than through a dotenv reader because the question is
    "where do I type the fix", not "what is the value" — a commented-out or
    duplicated key still has a line worth pointing at, and the last assignment
    wins, which is the one to correct.
    """
    if not path.is_file():
        return {}
    found: dict[str, int] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            found[key.upper()] = number
    return found


def source_of(env_var: str, lines: dict[str, int]) -> str:
    """Where the offending value actually came from.

    **The environment wins over the file**, in pydantic-settings and in compose
    alike, and that ordering is the whole reason this function exists rather
    than a `lines.get()` at the call site. A key that is exported *and* written
    in `.env` is being read from the export; pointing at the `.env` line would
    send an operator to edit a line that has no effect on the value they are
    trying to change, which is a worse outcome than saying nothing.
    """
    exported = env_var in os.environ or env_var.lower() in os.environ
    where = lines.get(env_var)
    if exported and where is not None:
        return f"from the environment — .env line {where} is set but OVERRIDDEN"
    if exported:
        return "from the environment (compose `environment:`, or an export in your shell)"
    if where is not None:
        return f".env line {where}"
    return "not assigned anywhere — this is the built-in default"


def describe(problem: ConfigProblem, lines: dict[str, int]) -> list[str]:
    """One problem, as the lines to print for it."""
    if problem.is_whole_configuration:
        return ["  the configuration as a whole", f"    {problem.reason}"]

    out = [f"  {problem.env_var}    {source_of(problem.env_var, lines)}", f"    {problem.reason}"]
    out.append(
        "    value withheld — this is a credential (CLAUDE.md §1.6)"
        if problem.value is None
        else f"    you wrote: {problem.value}"
    )
    return out


def main(argv: list[str] | None = None) -> int:
    # `Settings` resolves `env_file=".env"` against the *working directory*, so
    # run from `scripts/` or from a subpackage it would read a different file
    # (usually none) from the one whose line numbers are printed below — and
    # answer "every value loads" about a file it never opened. Anchoring both to
    # the repository root makes the two agree wherever this is invoked from,
    # and "the repo's .env" is what an operator means in any case.
    os.chdir(REPO_ROOT)
    problems = config_problems()
    if not problems:
        print("environment: every value loads")
        if not ENV_FILE.is_file():
            print("  no .env here — defaults only (`make up` writes one from .env.example)")
        return 0

    lines = env_file_lines()
    count = len(problems)
    # "problem" rather than "value": one of these is a rule *between* values
    # (§1.8's two locks) and has no single variable behind it.
    print(f"environment: {count} problem{'' if count == 1 else 's'}\n")
    for problem in problems:
        for line in describe(problem, lines):
            print(line)
        print()

    print("Until these are fixed the API cannot start — it builds its app at import,")
    print("so the container exits and is restarted, and the dashboard's sign-in screen")
    print('shows "Cannot reach the API." (docs/RUNBOOK.md, "Before you sign in").')
    return 1


if __name__ == "__main__":
    sys.exit(main())
