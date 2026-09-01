#!/usr/bin/env python3
"""Keep the deployment's secrets encrypted at rest, and install them on the host.

    uv run python scripts/manage_secrets.py init
    uv run python scripts/manage_secrets.py import --env paper --from .env
    uv run python scripts/manage_secrets.py edit   --env paper
    uv run python scripts/manage_secrets.py check  --env paper
    uv run python scripts/manage_secrets.py install --env paper

This is the secrets half of ADR 0011, which chose **SOPS + age**: the encrypted
bundle lives in the repository and is decrypted at deploy time to a `0600`
`.env` on the host. One operator and one host do not need a secrets manager's
server, and a hosted control plane would be a network dependency in the startup
path of a process whose job is to be running when the market opens.

A thin wrapper, on purpose. `sops` does the cryptography and `age` holds the
key; nothing here reimplements either, and there is no code path that touches a
cipher. What this file adds is the part `sops` has no opinion about — where
bundles live, what may not go in one, and how the plaintext lands on disk.

**Why dotenv and not YAML.** SOPS encrypts dotenv *values* and leaves the
*keys* readable, so a committed bundle diffs as "ALPACA_API_SECRET changed"
rather than as one opaque blob. Which secret was rotated is exactly what a
reviewer needs to see, and exactly what must not be readable is the value.

**Three keys may never be in a bundle** (`FORBIDDEN_KEYS`). `ATP_RUN_MODE`
and `ATP_ALLOW_LIVE_TRADING` are the two live-money locks that still live in
the environment — host configuration, not secrets — and a bundle is a thing
that gets copied between hosts, restored from a backup and re-synced by
tooling. None of those events may be able to turn on live trading as a side
effect. ADR 0011 named `ATP_ALLOW_LIVE_TRADING`; `ATP_RUN_MODE` is here for the
same reason, and is flagged in docs/DEPLOYMENT.md as an extension a reviewer
should either accept or strike.

`WORKER_ALLOW_LIVE_ORDERS` is the third of the three and is no longer read from
the environment at all — the lock moved into the `worker_config` row, where
arming it costs the operator's password. It stays on this list anyway, and the
reason is worth stating: a key nothing reads, sitting in an encrypted bundle
named after the thing that authorises real orders, is a line a future reader
will believe. Refusing it keeps the bundle honest.

**Nothing here ever prints a secret.** Failures report `sops`'s stderr and the
*names* of offending keys; decrypted plaintext goes to the target file and
nowhere else — never to stdout, never into an exception message, never into a
log line (CLAUDE.md §1.6). `install` is the only command that writes plaintext,
and it writes `0600`, atomically, and only after the bundle has passed
`check` — so a bundle that violates policy replaces nothing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[1]

#: Encrypted bundles live here, one per run mode, because ADR 0011 puts paper
#: and live on separate hosts with separate key pairs. NOT `secrets/`: the
#: `.gitignore` rule for that name is unanchored, so a bundle under it would be
#: ignored at any depth — and git cannot re-include a file whose parent
#: directory is excluded, so no negation would rescue it. An encrypted bundle
#: that git silently refuses to track is one that never reaches the host.
BUNDLE_DIR = REPO / "infra" / "env"

#: `*.sops.env` rather than `.env.*`, which `.gitignore` excludes wholesale.
#: The suffix is also what `.sops.yaml`'s creation rule matches on.
BUNDLE_SUFFIX = ".sops.env"

#: Where the plaintext lands by default: the `.env` compose reads from the
#: project directory.
DEFAULT_TARGET = REPO / ".env"

SOPS_CONFIG = REPO / ".sops.yaml"

#: The run-mode locks, plus one that is no longer a setting. See the module
#: docstring: the first two are host configuration a bundle must not carry
#: between hosts, and the third is refused so a dead key cannot sit in a bundle
#: looking like it still does something.
FORBIDDEN_KEYS = frozenset({"ATP_RUN_MODE", "ATP_ALLOW_LIVE_TRADING", "WORKER_ALLOW_LIVE_ORDERS"})

#: Reported by `check` when absent, never fatal. Which of these a host needs
#: depends on its run mode — a backtest host has no broker credentials — so an
#: absent one is a question rather than an error. docs/DEPLOYMENT.md has the
#: table that says which matter when.
EXPECTED_KEYS = (
    "ATP_DB_PASSWORD",
    "DATABASE_URL",
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
    "API_SECRET_KEY",
    "API_USER",
    "API_PASSWORD_HASH",
)


class SecretsError(Exception):
    """Something went wrong in a way the operator has to decide about.

    Carries no secret material, by construction: every raise site passes a
    message built from key names, paths and `sops`'s stderr.
    """


# ── running sops ─────────────────────────────────────────────────────────────
def _require_sops() -> str:
    found = shutil.which("sops")
    if found is None:
        raise SecretsError(
            "sops is not installed.\n"
            "  https://github.com/getsops/sops/releases — one binary, no daemon.\n"
            "  age is the key backend and is a separate install (`age-keygen`)."
        )
    return found


def run_sops(
    args: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    stdin: str | None = None,
) -> str:
    """Run `sops` and return its stdout, or raise carrying only its stderr.

    The distinction matters more than it looks. On the decrypt path stdout *is*
    the plaintext, so an error handler that helpfully included "the output" in
    its message would put the entire secret bundle into a traceback, a CI log
    and a bug report. Only stderr is ever quoted.

    `sops` writes an upstream-version-check warning to stderr in networks that
    block it, so stderr is not evidence of failure — the exit code is.
    """
    run = runner or subprocess.run
    sops = _require_sops() if runner is None else "sops"
    completed = run(
        [sops, *args],
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        hint = ""
        if "data key" in stderr:
            hint = (
                "\n\nThat is what sops says when it cannot find a key that can open this\n"
                "bundle. Check SOPS_AGE_KEY_FILE, or ~/.config/sops/age/keys.txt, and that\n"
                "this host's public key is one of the bundle's recipients."
            )
        raise SecretsError(f"sops exited {completed.returncode}:\n{stderr}{hint}")
    return completed.stdout


# ── dotenv ───────────────────────────────────────────────────────────────────
def parse_dotenv(text: str) -> dict[str, str]:
    """Key/value pairs from dotenv text, ignoring comments and blank lines.

    Deliberately not a full dotenv implementation — it exists to answer "which
    keys are in here, and is any of them empty". It does not expand variables,
    resolve quotes or interpret escapes, because the file it reads is handed
    straight to Docker Compose and to `Settings`, which do their own parsing. A
    second interpretation here that disagreed with theirs would be worse than
    none.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if key:
            values[key] = value.strip()
    return values


# ── policy ───────────────────────────────────────────────────────────────────
def policy_failures(values: Mapping[str, str]) -> list[str]:
    """Every reason this bundle must not be installed. Names only, no values."""
    failures: list[str] = []

    for key in sorted(FORBIDDEN_KEYS & values.keys()):
        failures.append(
            f"{key} is in the bundle. It is a run-mode lock (docs/SAFETY.md layers 1-2)"
            " and belongs in the host's own configuration — a bundle travels between"
            " hosts and comes back from backups, and none of that may switch on live"
            " trading."
        )

    for key in sorted(k for k, v in values.items() if not v):
        failures.append(
            f"{key} is present but empty. SOPS leaves an empty value unencrypted, so"
            " this neither carries a secret nor protects one; the process reading it"
            " will fall back to its default as though the key were absent. Remove the"
            " line or give it a value."
        )

    return failures


def missing_expected(values: Mapping[str, str]) -> list[str]:
    return [key for key in EXPECTED_KEYS if key not in values]


# ── paths ────────────────────────────────────────────────────────────────────
def bundle_path(env: str) -> Path:
    if not env or "/" in env or env.startswith("."):
        raise SecretsError(f"{env!r} is not a usable environment name.")
    return BUNDLE_DIR / f"{env}{BUNDLE_SUFFIX}"


def require_bundle(env: str) -> Path:
    path = bundle_path(env)
    if not path.is_file():
        raise SecretsError(
            f"No bundle at {path.relative_to(REPO)}.\n"
            f"  Create one:  scripts/manage_secrets.py import --env {env} --from .env"
        )
    return path


def refuse_if_gitignored(path: Path) -> None:
    """Fail if git would ignore the bundle we are about to write.

    An encrypted bundle is *meant* to be committed — that is the whole design —
    so one that git silently drops is a deployment that quietly has no secrets.
    `.gitignore` in this repo already carries a scar from an unanchored rule
    hiding a whole package from CI; this is the same failure with a worse blast
    radius, checked rather than hoped for.
    """
    try:
        completed = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return  # No git here. Not this script's business to insist on one.
    if completed.returncode == 0:
        raise SecretsError(
            f"{path.relative_to(REPO)} is gitignored, so it would never be committed"
            " and never reach a host.\n"
            "  Check the rules in .gitignore — an unanchored pattern matches at every"
            " depth, and\n  git cannot re-include a file whose parent directory is"
            " excluded."
        )


def write_private(path: Path, text: str) -> None:
    """Write plaintext to `path` with mode 0600, atomically.

    Two properties, both of which matter on a host that is trading.

    *Never briefly world-readable.* The temp file is created 0600 by
    `mkstemp` before anything is written to it, rather than written first and
    chmod-ed after — that gap is small and is exactly when a backup agent or
    another user reads the file.

    *Never half-written.* The rename is atomic within a directory, so a process
    reading `.env` sees either the old file or the new one. A crash mid-write
    that left a truncated `.env` would start the stack with half its
    credentials, which fails in ways that look like anything but a truncated
    file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.chmod(0o600)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_init(args: argparse.Namespace) -> int:
    """Generate this host's age key and register it as a recipient."""
    key_file = Path(args.key_file).expanduser()
    if key_file.exists() and not args.force:
        print(f"{key_file} already exists — refusing to overwrite it.")
        print("A lost age key means every bundle encrypted to it is unreadable.")
        print("Use --force only if you are certain, and re-encrypt afterwards.")
        return 1

    if shutil.which("age-keygen") is None:
        raise SecretsError("age-keygen is not installed — https://github.com/FiloSottile/age")

    key_file.parent.mkdir(parents=True, exist_ok=True)
    # age-keygen writes the private key. Its own stderr carries the public key;
    # we re-derive it below rather than parse that, and the private half is
    # never read into this process.
    subprocess.run(["age-keygen", "-o", str(key_file)], capture_output=True, text=True, check=True)
    key_file.chmod(0o600)

    recipient = subprocess.run(
        ["age-keygen", "-y", str(key_file)], capture_output=True, text=True, check=True
    ).stdout.strip()

    _write_sops_config([recipient])

    print(f"Private key : {key_file}  (mode 0600 — back this up somewhere offline)")
    print(f"Recipient   : {recipient}")
    print(f"Wrote       : {SOPS_CONFIG.relative_to(REPO)}")
    print()
    print("The recipient is a public key: commit .sops.yaml. The private key is not")
    print("in the repository and must never be — it is the one thing that cannot be")
    print("regenerated, and losing it means re-creating every bundle from scratch.")
    return 0


def _write_sops_config(recipients: Iterable[str]) -> None:
    joined = ",".join(recipients)
    SOPS_CONFIG.write_text(
        "# Generated by scripts/manage_secrets.py init. Committed on purpose: an age\n"
        "# recipient is a public key, and every machine that edits a bundle needs\n"
        "# the same list. The private halves live outside the repository.\n"
        "#\n"
        "# The rule matches the bundle naming this repo uses — infra/env/*.sops.env\n"
        "# (see scripts/manage_secrets.py). Decryption ignores this file entirely; it is\n"
        "# consulted only when encrypting.\n"
        "creation_rules:\n"
        "  - path_regex: '\\.sops\\.env$'\n"
        f"    age: '{joined}'\n",
        encoding="utf-8",
    )


def cmd_import(args: argparse.Namespace) -> int:
    """Encrypt an existing plaintext .env into a bundle."""
    source = Path(args.source)
    if not source.is_file():
        raise SecretsError(f"No such file: {source}")
    if not SOPS_CONFIG.is_file():
        raise SecretsError(
            f"No {SOPS_CONFIG.name} — nothing knows who to encrypt to.\n"
            "  Run:  scripts/manage_secrets.py init"
        )

    values = parse_dotenv(source.read_text(encoding="utf-8"))
    failures = policy_failures(values)
    if failures:
        _report_failures(failures)
        print("\nNothing was encrypted. Fix the source file and run this again.")
        return 1

    destination = bundle_path(args.env)
    if destination.exists() and not args.force:
        raise SecretsError(
            f"{destination.relative_to(REPO)} already exists.\n"
            f"  To change it:   scripts/manage_secrets.py edit --env {args.env}\n"
            "  To replace it:  pass --force"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    refuse_if_gitignored(destination)

    encrypted = run_sops(
        [
            "--encrypt",
            "--input-type",
            "dotenv",
            "--output-type",
            "dotenv",
            # sops matches .sops.yaml's creation rules against the path it is
            # *reading*, which here is whatever plaintext file the operator
            # points at — `.env`, usually, which matches no rule. This tells it
            # to decide by the destination instead. The alternative is copying
            # the plaintext to the bundle path and encrypting in place, which
            # would leave a readable secret sitting at a committable path for
            # as long as that takes, and permanently if the encrypt failed.
            "--filename-override",
            str(destination),
            str(source),
        ]
    )
    destination.write_text(encrypted, encoding="utf-8")

    print(f"Encrypted {len(values)} key(s) into {destination.relative_to(REPO)}")
    print()
    print("Commit it — that is the point of encrypting it. Then remove the plaintext:")
    print(f"  shred -u {source}" if source != DEFAULT_TARGET else f"  (keeping {source})")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """Open the bundle in $EDITOR through sops, which re-encrypts on save."""
    path = require_bundle(args.env)
    refuse_if_gitignored(path)
    sops = _require_sops()
    # Handed straight to sops rather than captured: this is an interactive
    # editor session, and sops writes the plaintext to a temp file it owns and
    # removes. Capturing its streams would break the editor and gain nothing.
    completed = subprocess.run([sops, str(path)], check=False)
    if completed.returncode != 0:
        raise SecretsError(f"sops exited {completed.returncode} — the bundle is unchanged.")
    return cmd_check(args)


def cmd_check(args: argparse.Namespace) -> int:
    """Decrypt in memory and report on the contents, naming no values."""
    path = require_bundle(args.env)
    plaintext = run_sops(
        ["--decrypt", "--input-type", "dotenv", "--output-type", "dotenv", str(path)]
    )
    values = parse_dotenv(plaintext)

    failures = policy_failures(values)
    if failures:
        _report_failures(failures)
        return 1

    print(f"{path.relative_to(REPO)}: decrypts, and carries {len(values)} key(s)")
    for key in sorted(values):
        print(f"  {key}")

    absent = missing_expected(values)
    if absent:
        print()
        print("Not in this bundle, which may be right — a host in backtest mode has no")
        print("broker credentials. docs/DEPLOYMENT.md says which matter when:")
        for key in absent:
            print(f"  {key}")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Decrypt the bundle onto the host as a 0600 file. The deploy-time step."""
    path = require_bundle(args.env)
    target = Path(args.target)

    plaintext = run_sops(
        ["--decrypt", "--input-type", "dotenv", "--output-type", "dotenv", str(path)]
    )
    values = parse_dotenv(plaintext)

    # Checked before writing, so a bundle that breaks policy leaves whatever is
    # already on the host untouched rather than replacing it with something
    # worse.
    failures = policy_failures(values)
    if failures:
        _report_failures(failures)
        print(f"\n{target} was not written.")
        return 1

    write_private(target, plaintext)
    print(f"Wrote {len(values)} key(s) to {target} (mode 0600)")
    print()
    print("The run-mode locks are deliberately not in the bundle and are not written")
    print("here. Set ATP_RUN_MODE and ATP_ALLOW_LIVE_TRADING")
    print("if this host trades live, in the host's own configuration.")
    return 0


def _report_failures(failures: list[str]) -> None:
    print("Refusing — this must not be in a bundle:", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)


# ── entry point ──────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="secrets.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="generate an age key and write .sops.yaml")
    init.add_argument(
        "--key-file",
        default="~/.config/sops/age/keys.txt",
        help="where the private key goes (default: where sops looks for it)",
    )
    init.add_argument("--force", action="store_true", help="overwrite an existing key")
    init.set_defaults(func=cmd_init)

    imp = sub.add_parser("import", help="encrypt a plaintext .env into a bundle")
    _add_env(imp)
    imp.add_argument("--from", dest="source", default=str(DEFAULT_TARGET), help="plaintext .env")
    imp.add_argument("--force", action="store_true", help="replace an existing bundle")
    imp.set_defaults(func=cmd_import)

    edit = sub.add_parser("edit", help="edit the bundle in $EDITOR, re-encrypting on save")
    _add_env(edit)
    edit.set_defaults(func=cmd_edit)

    check = sub.add_parser("check", help="decrypt in memory and report; writes nothing")
    _add_env(check)
    check.set_defaults(func=cmd_check)

    install = sub.add_parser("install", help="decrypt onto this host as a 0600 .env")
    _add_env(install)
    install.add_argument(
        "--to", dest="target", default=str(DEFAULT_TARGET), help="where the plaintext goes"
    )
    install.set_defaults(func=cmd_install)

    return p.parse_args(argv)


def _add_env(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env",
        required=True,
        help="which bundle — one per run mode, since paper and live are separate hosts",
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result: int = args.func(args)
    except SecretsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
