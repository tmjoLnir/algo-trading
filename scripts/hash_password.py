#!/usr/bin/env python3
"""Produce the `API_PASSWORD_HASH` line for `.env`.

    uv run python scripts/hash_password.py

That form needs the workspace installed — `make install`, which is
`uv sync --all-packages`. A plain `uv sync` installs only the workspace root,
which declares no runtime dependencies, and this script then cannot import what
it hashes with. It says so and how to fix it rather than raising. The form that
works either way, because it resolves the environment of the package that
declares those dependencies:

    uv run --package atp-api python scripts/hash_password.py

The password is read without echo and never printed, never logged, and never
taken as an argument — an argument would put it in the shell history and in the
process list of every other user on the machine, which is the whole thing
CLAUDE.md §1.6 is about. Only the hash is written to stdout.

A bcrypt hash is not a credential you can log in with, but it is one an attacker
can grind offline, so treat the resulting `.env` line as a secret too.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api" / "src"))


def _dependency_help(missing: str) -> str:
    """What to print when the hashing code's own imports do not resolve.

    Defined above the import it serves, which reads oddly and has to: the import
    is the thing that can fail.

    The `sys.path` line above is what makes this failure worth its own message.
    It puts `apps/api/src` on the path, so the *first-party* import resolves
    from a bare checkout — and then dies one layer down on a third-party package
    that has to be genuinely installed. `bcrypt` is declared by `atp-api`, a
    workspace member; the workspace ROOT declares no runtime dependencies at
    all. So a plain `uv sync` leaves this script importable and unusable, and
    the traceback it produced named `bcrypt` without naming the remedy — on the
    first command a new operator runs, before there is any password to log in
    with at all.
    """
    return (
        f"{missing} is not installed in this environment.\n"
        "\n"
        "This script hashes with the `atp-api` package's code, so it needs that\n"
        "package's dependencies. A plain `uv sync` installs only the workspace\n"
        "root, which declares none of them.\n"
        "\n"
        "Install the workspace:\n"
        "\n"
        "    make install                 # uv sync --all-packages\n"
        "\n"
        "or run this against the package that declares them, without re-syncing:\n"
        "\n"
        "    uv run --package atp-api python scripts/hash_password.py"
    )


try:
    from atp_api.auth import MAX_PASSWORD_BYTES, PasswordTooLongError, hash_password
except ImportError as exc:  # pragma: no cover - driven by a subprocess test
    raise SystemExit(_dependency_help(exc.name or "A dependency")) from exc


def main() -> int:
    password = getpass.getpass("Password for the operator account: ")
    if not password:
        print("No password given — nothing written.", file=sys.stderr)
        return 1

    if password != getpass.getpass("Again, to confirm: "):
        print("The two did not match.", file=sys.stderr)
        return 1

    try:
        hashed = hash_password(password)
    except PasswordTooLongError:
        print(
            f"bcrypt hashes at most {MAX_PASSWORD_BYTES} bytes. Choose a shorter\n"
            "password rather than a truncated one — truncation would make every\n"
            "password sharing the first 72 bytes equivalent.",
            file=sys.stderr,
        )
        return 1

    print()
    print("Put this in .env (and keep it out of anything you commit):")
    print()
    print(f"API_PASSWORD_HASH={hashed}")
    print()
    print("Set API_USER too if the account should not be called 'operator', and")
    print("set API_SECRET_KEY (openssl rand -hex 32) so sessions survive a restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
