#!/usr/bin/env python3
"""Produce the `API_PASSWORD_HASH` line for `.env`.

    uv run python scripts/hash_password.py

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

from atp_api.auth import MAX_PASSWORD_BYTES, PasswordTooLongError, hash_password


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
