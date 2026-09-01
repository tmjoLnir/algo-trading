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

**Every check here runs without the platform.** No container, no database, no
network: it reads the same `.env` through the same `Settings` and reports what
happens. That matters because the situation it is for is one where nothing else
starts, including `scripts/preflight.py` and `scripts/status.py` — both of which
call `get_settings()` and, until this existed, died with the same traceback they
were being run to explain.

**One question cannot be answered that way, and it is the one that comes up
most.** `POSTGRES_PASSWORD` is read at initdb and never again, so a password
rotated against an existing volume leaves `.env` internally consistent and the
database still wanting the old one — nothing in the file is wrong, and every
static check above passes while the platform cannot authenticate to its own
database. So after the static checks come back clean, this asks the database
itself, once, with a short timeout. A server that answers and refuses is the
finding; a server that does not answer is not, and the command behaves exactly
as it always did on a machine where nothing is up. `--offline` skips the
question entirely.

Secrets are never printed. A value that fails to load is still a credential, and
most of one is still worth grinding offline (CLAUDE.md §1.6) — so a problem on a
`SecretStr` field reports the variable and the reason and withholds the value,
and anything it cannot classify is withheld too.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import os
import sys
from enum import StrEnum
from pathlib import Path

import asyncpg
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

from atp_core.config import ConfigProblem, config_problems, known_env_vars
from atp_core.persistence.db import is_auth_failure

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"

#: Keys that belong in `.env` and are deliberately NOT `Settings` fields, with
#: what reads each. Without this the unknown-key check below would report all
#: four as typos on a stock `.env`, which is the fastest way to teach someone to
#: ignore it.
#:
#: Every entry is a key read by something that is not Python: keep it that way.
#: A `Settings` field that ends up here is a field the platform silently stopped
#: reading, which is the exact failure this check exists to catch.
READ_ELSEWHERE = {
    # Vite inlines `import.meta.env.VITE_*` into the bundle at BUILD time
    # (infra/docker/web.Dockerfile), so these never reach a running process.
    "VITE_API_BASE_URL": "read by Vite at build time",
    "VITE_WS_URL": "read by Vite at build time",
    # Read by vite.config.ts in the web container, not by any Python process.
    "ATP_DEV_PROXY_TARGET": "read by the dev server (apps/web/vite.config.ts)",
    # Interpolated by compose into the published port, before anything starts.
    "ATP_WEB_BIND_ADDR": "read by docker compose (docker-compose.yml)",
    # Interpolated by compose into POSTGRES_PASSWORD and into the DATABASE_URL
    # it hands the api/worker/queue containers. Never read by `Settings`: the
    # containers receive the assembled url, not this. In .env.example since the
    # deploy overlay made it mandatory and the template did not mention it.
    "ATP_DB_PASSWORD": "read by docker compose (docker-compose.prod.yml)",
}


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


def unread_keys(lines: dict[str, int]) -> list[tuple[str, int, str]]:
    """Keys assigned in `.env` that nothing will read, worst-first by line.

    The silent half of a broken `.env`. `Settings` ignores what it does not
    recognise — correctly, since the file is shared with compose and Vite — so a
    misspelled key is dropped without a word and the field keeps its default.
    An operator who wrote `RISK_MAX_POSITION_PC=0.02` believes the cap is 2%;
    it is 10%.

    A close match is offered where one exists, because "read by nothing" and
    "you are one character out" are the same finding and only the second is
    actionable in one step.
    """
    known = known_env_vars()
    found: list[tuple[str, int, str]] = []
    for key, line in sorted(lines.items(), key=lambda kv: kv[1]):
        if key in known or key in READ_ELSEWHERE:
            continue
        near = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.8)
        note = f"did you mean {near[0]}?" if near else "nothing in this platform reads it"
        found.append((key, line, note))
    return found


#: The DSN `docker-compose.prod.yml` builds, with the password left to fill in.
#: Kept here as the one literal so the round trip below is checked against the
#: string the deployed stack actually assembles rather than an approximation of
#: it — the whole value of the check is that it agrees with production.
DEPLOYED_DSN = "postgresql+asyncpg://atp:{password}@db:5432/atp"


def db_password_problem(password: str) -> str | None:
    """Why `ATP_DB_PASSWORD` will not reach Postgres intact, or `None`.

    The one value in `.env` that is copied down **two paths that must agree and
    neither of which is a plain copy**. Compose interpolates it into
    `POSTGRES_PASSWORD`, which initdb stores verbatim, and into a `DATABASE_URL`
    that SQLAlchemy then parses as a URL. A character that means something to
    either path arrives at the database as two different strings, and what the
    operator sees is:

        asyncpg.exceptions.InvalidPasswordError: password authentication
        failed for user "atp"

    on every request the API serves — with a `.env` that reads correctly, a
    password that *is* correct, and nothing anywhere naming the character that
    did it. `Settings` never sees this value, so until now nothing in this
    repository looked at it at all.

    Three characters break it, and they break it differently:

    ``$``   compose reads it as a variable reference and substitutes the empty
            string. It does so in *both* places, so the two still match and the
            containers authenticate fine — but `.env`'s own `DATABASE_URL`, read
            by pydantic, is not interpolated, so `make migrate` and `seed` are
            then the things that fail, against a database the stack is happily
            using.

    ``@``   the DSN's authority splits at the first one, so the password is
            truncated there and the rest becomes part of the hostname. That
            surfaces as an unresolvable host rather than as a refused password,
            which sends the diagnosis somewhere else entirely.

    ``%``   followed by two hex digits it is a percent-escape, and SQLAlchemy
            decodes it. Postgres stored `x%3Ay`; the API sends `x:y`. This is
            the silent one and the one that produces the error above verbatim.

    The check for the second and third is the round trip itself rather than a
    character list, so it cannot drift from the parser it is checking — the same
    reasoning `config_problems()` gives for reading `.env` through `Settings`.

    Never returns the password or any part of it: the reason names the
    *character class* that broke, which is what makes it actionable, and the
    caller withholds the value (CLAUDE.md §1.6).
    """
    if not password:
        # Documented as fine until you deploy: `make up` and `make up-prod` run
        # the base file, which hardcodes `atp`/`atp`. The deploy overlay's `:?`
        # is what refuses an empty one, at the moment it matters.
        return None

    if "$" in password:
        return (
            "contains `$`, which docker compose reads as a variable reference and "
            "substitutes away — Postgres and the host-side DATABASE_URL then "
            "disagree about the password. Use `$$` for a literal `$`, or generate "
            "one with `openssl rand -hex 24`"
        )

    try:
        url = make_url(DEPLOYED_DSN.format(password=password))
    except Exception:
        return (
            "cannot be parsed as part of a database URL — docker-compose.prod.yml "
            "interpolates it into one. Generate one with `openssl rand -hex 24`"
        )

    if url.host != "db" or url.database != "atp":
        return (
            "contains `@`, which ends the credentials in a database URL — the rest "
            "of the password is read as the hostname, so the API cannot resolve the "
            "database at all. Generate one with `openssl rand -hex 24`"
        )

    if url.password != password:
        return (
            "contains a `%` followed by two hex digits, which is a percent-escape "
            "in a database URL and is DECODED before it reaches Postgres. initdb "
            "stored what you wrote; the API sends the decoded form, and every "
            'request fails with `password authentication failed for user "atp"`. '
            "Generate one with `openssl rand -hex 24`"
        )

    return None


def db_credential_problems(values: dict[str, str], lines: dict[str, int]) -> list[tuple[str, str]]:
    """`(env var, reason)` for a database password that will not work, worst first.

    Two findings, and they are the two halves of one failure — the deployed
    stack and the host-side tools reach the same database by different routes,
    and a password can be wrong on either.

    The mismatch check fires only once `ATP_DB_PASSWORD` is set, because an
    empty one means this is a laptop running `make up` against the base file's
    hardcoded `atp`/`atp`, where `.env`'s stock `DATABASE_URL` is correct as
    written and reporting it would be noise on every developer's machine.
    """
    found: list[tuple[str, str]] = []

    password = values.get("ATP_DB_PASSWORD", "")
    reason = db_password_problem(password)
    if reason is not None:
        found.append(("ATP_DB_PASSWORD", reason))

    dsn = values.get("DATABASE_URL", "")
    if password and dsn:
        try:
            host_side = make_url(dsn).password
        except Exception:
            # A DATABASE_URL that will not parse is `Settings`' problem to
            # report, not this one's — saying it twice in one run reads as two
            # faults.
            return found
        if host_side != password:
            found.append(
                (
                    "DATABASE_URL",
                    "carries a different password from ATP_DB_PASSWORD. The containers "
                    "get the one compose builds and will be fine; the host-side tools "
                    "read THIS url, so `make migrate`, `seed`, `backfill` and "
                    "`scripts/halt.py` will fail against the database the stack is "
                    "using (.env.example, 'datastores')",
                )
            )

    return found


#: How long to wait for the database to answer. Short on purpose: this runs when
#: the platform will not come up, and an operator staring at a hung command
#: learns nothing. Three seconds is far more than a loopback Postgres needs and
#: far less than a wrong host takes to time out.
PROBE_TIMEOUT_SECONDS = 3.0


class Probe(StrEnum):
    """What the database said when asked to accept the password in `.env`."""

    #: It answered and let us in. The stored password and `.env` agree — the one
    #: outcome no static check can establish.
    ACCEPTED = "accepted"
    #: It answered and refused. SQLSTATE class 28: the server is up, it read the
    #: credentials, and it said no.
    REFUSED = "refused"
    #: Nothing answered — refused socket, wrong host, timeout, or no database
    #: running. Not a finding: this is the normal state of a laptop with the
    #: stack down, which is most of when this command is run.
    UNREACHABLE = "unreachable"
    #: Not asked: `--offline`, or there is no url to ask down.
    NOT_ASKED = "not asked"


async def _ask_the_database(dsn: str, timeout: float) -> Probe:
    """Open one connection and immediately close it."""
    url = make_url(dsn)
    try:
        connection = await asyncpg.connect(
            host=url.host,
            port=url.port or 5432,
            user=url.username,
            password=url.password,
            database=url.database,
            timeout=timeout,
        )
    except Exception as exc:
        # The same verdict the API and preflight use, from the module that owns
        # it — so "the database refused these credentials" cannot come to mean
        # one thing here and another there.
        return Probe.REFUSED if is_auth_failure(exc) else Probe.UNREACHABLE
    await connection.close()
    return Probe.ACCEPTED


def probe_stored_password(dsn: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> Probe:
    """Does the database actually accept the password `.env` carries?

    **The fault this exists for is invisible to every other check in this file.**
    `POSTGRES_PASSWORD` is read by initdb and never again, so on every start
    after the first the volume keeps whatever password it was created with. Set
    or rotate `ATP_DB_PASSWORD` against an existing volume and the containers
    begin sending a new password to a database that still wants the old one —
    with a `.env` that is correct, internally consistent, and passes everything
    above. `docker compose logs db` says `password authentication failed for
    user "atp"` and nothing else in the repository has an opinion.

    It is also the *common* case. The three characters `db_password_problem`
    catches are a password that was never right; this is the far more ordinary
    story of a password that was right and then changed on one side only.

    Asked of the **host-side** `DATABASE_URL`, because that is the one reachable
    from wherever this command runs — the containers' url names `db`, a host on
    the compose network that does not resolve here. The two are checked against
    each other by `db_credential_problems`, so when they agree this answers for
    both, and when they disagree that is already reported.

    Never sends anything anywhere else: one connection to the host in that url,
    closed immediately. Nothing is read, written or migrated.
    """
    try:
        make_url(dsn)
    except Exception:
        # `Settings` reports an unparseable url; saying it twice reads as two
        # faults.
        return Probe.NOT_ASKED
    try:
        return asyncio.run(_ask_the_database(dsn, timeout))
    except Exception:
        # A driver that will not even start is not evidence about a password.
        return Probe.UNREACHABLE


def should_ask_the_database(
    *, offline: bool, problems: list[ConfigProblem], credentials: list[tuple[str, str]], dsn: str
) -> bool:
    """Would a refusal from the database still be unexplained by `.env`?

    The gating rule, and the whole reason the probe adds information rather than
    noise. **Every finding above is already a reason the database would refuse
    us.** A `%` escape, a password that disagrees with `ATP_DB_PASSWORD`, a
    value that will not load — ask through any of those and the answer is "the
    database said no", which is true, useless, and printed next to the check
    that just named the character and the line. One fault must not be reported
    twice, and certainly not with the vaguer half last.

    So the question is asked only when the file has nothing left to say. A
    refusal that survives this gate cannot be accounted for by anything in
    `.env` — which is the stale volume, and is the one shape no static check
    can reach.

    Unread keys are deliberately not a reason to stay silent: a misspelled risk
    limit has no bearing on whether Postgres accepts a password, and suppressing
    the probe over one would hide the database fault behind a typo.
    """
    return bool(dsn) and not offline and not problems and not credentials


def describe_refusal(dsn: str, lines: dict[str, int]) -> list[str]:
    """The finding, as the lines to print for it.

    Shaped like `describe` above: this returns the report and `main` prints it,
    so what it says can be checked without a terminal — including the one thing
    that must never be in it (CLAUDE.md §1.6).
    """
    return [
        ".env: nothing wrong with the file, and the database refuses it anyway",
        "",
        f"  DATABASE_URL    {source_of('DATABASE_URL', lines)}",
        f"    {where_it_asked(dsn)} answered, read this password, and refused it.",
        "    POSTGRES_PASSWORD is read at initdb and NEVER AGAIN, so a volume that",
        "    already existed kept whatever password it was created with. If",
        "    ATP_DB_PASSWORD was set or rotated after the first start, every container",
        "    has been sending the new one to a database that still wants the old.",
        "    value withheld — this is a credential (CLAUDE.md §1.6)",
        "",
        "Two fixes, and the first is almost always the right one.",
        "",
        "  Change the password in the database to match .env — KEEPS THE DATA:",
        "    docker compose exec db psql -U atp -d atp \\",
        "      -c \"ALTER USER atp PASSWORD '<the password in .env>';\"",
        "    docker compose restart api worker queue",
        "",
        "  Or re-initialise the volume — DESTROYS every bar, order and snapshot:",
        "    docker compose down -v && make deploy && make migrate",
        "",
        "Restart the three services after an ALTER USER: SQLAlchemy pools connections,",
        "so a process that authenticated before the change keeps working and one that",
        "reconnects after it does not — which is how this fault arrives mid-session",
        'rather than at startup. docs/RUNBOOK.md, "password authentication failed",',
        "has both procedures in full.",
    ]


def where_it_asked(dsn: str) -> str:
    """The database a probe went to, named without its password.

    Printed with the finding because the answer is only as good as the target:
    a developer with an unrelated Postgres on 5432 gets a true statement about
    the wrong server, and this is the line that shows it.
    """
    try:
        url = make_url(dsn)
    except Exception:
        return "the url in DATABASE_URL"
    return f"{url.username}@{url.host}:{url.port or 5432}/{url.database}"


def env_file_values(path: Path = ENV_FILE) -> dict[str, str]:
    """`{KEY: value}` as the things that read `.env` will resolve it.

    Through a dotenv reader rather than the split in `env_file_lines`, because
    here the question *is* "what is the value" — quoting and escapes have to be
    resolved the way compose and pydantic resolve them, or the check would judge
    a password neither of them will ever see.

    The real environment wins over the file, matching `source_of` and matching
    both readers.
    """
    values = (
        {k: v for k, v in dotenv_values(path).items() if v is not None} if path.is_file() else {}
    )
    for key in [*values, "ATP_DB_PASSWORD", "DATABASE_URL"]:
        exported = os.environ.get(key)
        if exported is not None:
            values[key] = exported
    return values


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip the one check that opens a connection; read .env and nothing else",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # `Settings` resolves `env_file=".env"` against the *working directory*, so
    # run from `scripts/` or from a subpackage it would read a different file
    # (usually none) from the one whose line numbers are printed below — and
    # answer "every value loads" about a file it never opened. Anchoring both to
    # the repository root makes the two agree wherever this is invoked from,
    # and "the repo's .env" is what an operator means in any case.
    os.chdir(REPO_ROOT)
    problems = config_problems()
    lines = env_file_lines()
    unread = unread_keys(lines)
    values = env_file_values()
    credentials = db_credential_problems(values, lines)

    # Asked only once the file itself is clean, and that is the whole design:
    # every finding above is a reason the database would refuse us that `.env`
    # already explains. Probing through one of those would report the same
    # fault twice and bury the actionable half — the static reason names the
    # character or the line, and "the database said no" does not. So a refusal
    # reaching this point means something the file cannot account for, which
    # is exactly the stale volume.
    dsn = values.get("DATABASE_URL", "")
    probe = Probe.NOT_ASKED
    if should_ask_the_database(
        offline=args.offline, problems=problems, credentials=credentials, dsn=dsn
    ):
        probe = probe_stored_password(dsn)

    if not problems and not unread and not credentials and probe is not Probe.REFUSED:
        print("environment: every value loads, and every key in .env is read")
        if not ENV_FILE.is_file():
            print("  no .env here — defaults only (`make up` writes one from .env.example)")
        # Worth a line of its own: it is the only statement here that was
        # confirmed against the running database rather than reasoned about.
        if probe is Probe.ACCEPTED:
            print(f"  and {where_it_asked(dsn)} accepts the password in DATABASE_URL")
        elif probe is Probe.UNREACHABLE:
            print(f"  not checked: nothing answered at {where_it_asked(dsn)} — the stored")
            print("  password is whatever initdb was given, which only a running database")
            print("  can confirm (`make up`, then run this again)")
        return 0

    # ── values that will not load ───────────────────────────────────────────
    if problems:
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

    # ── keys nothing reads ──────────────────────────────────────────────────
    # Reported separately because it is a different failure with a different
    # shape: nothing is broken, nothing exits, and the value simply had no
    # effect. That is worse than a crash for a risk limit — a stack that will
    # not boot tells you so, and a cap you believe you tightened does not.
    if unread:
        if problems:
            print()
        count = len(unread)
        print(f".env: {count} key{'' if count == 1 else 's'} that nothing reads\n")
        for key, line, note in unread:
            print(f"  {key}    .env line {line}")
            print(f"    {note}")
            print()
        print("These load cleanly and have NO effect: `Settings` ignores what it does not")
        print("recognise, because this file is shared with compose and Vite. A misspelled")
        print("limit is the dangerous one — the field silently keeps its default, so a cap")
        print("you believe you tightened is still whatever it was.")

    # ── a password that will not survive the trip to Postgres ───────────────
    # The third shape, and the only one where the value is both correct and
    # unusable: it loads, nothing reads it wrong, and it still arrives at the
    # database as a different string from the one initdb stored.
    if credentials:
        if problems or unread:
            print()
        count = len(credentials)
        print(f".env: {count} database credential{'' if count == 1 else 's'} that will not work\n")
        for key, reason in credentials:
            print(f"  {key}    {source_of(key, lines)}")
            print(f"    {reason}")
            print("    value withheld — this is a credential (CLAUDE.md §1.6)")
            print()
        print("The API starts fine with these and then fails every request that reads the")
        print('database: `password authentication failed for user "atp"`, with /readyz')
        print('reporting `database: unreachable` (docs/RUNBOOK.md, "password authentication')
        print('failed").')

    # ── a file that is right about a database that disagrees ────────────────
    # The fourth shape, and the only one nothing in `.env` can account for:
    # every value loads, every key is read, the password survives both paths —
    # and the database refuses it anyway, because it is not the password initdb
    # was given.
    if probe is Probe.REFUSED:
        if problems or unread or credentials:
            print()
        for line in describe_refusal(dsn, lines):
            print(line)

    return 1


if __name__ == "__main__":
    sys.exit(main())
