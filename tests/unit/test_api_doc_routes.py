"""`docs/API.md` §7's route table must agree with the routes the app serves.

That table's whole value is the column the OpenAPI document cannot have. Twelve
of the forty-nine handlers `raise NotImplementedError`, and they appear in the
generated schema exactly like the thirty-seven that work — FastAPI documents a
handler by its signature, and a stub has the same signature as an
implementation. So a client author who generates from the schema and trusts it
finds out in production, and the table is the only place that says which is
which.

A hand-maintained list of that kind has one failure mode and it is not subtle:
somebody implements a stub, and its 🔲 stays. The table then reads as a
statement about what is built while being a statement about what was built on
the day it was written — and it is *more* dangerous stale than absent, because
a reader who has been told the page is authoritative on this will believe it.
`test_roadmap_summary.py` and `test_audit_summary.py` next door exist for the
same reason about the same class of document; this is that check for the route
table, and it was written in the same PR that promised it.

**What is derived and what is trusted.** The paths come from the app's own
OpenAPI document, so this never reimplements FastAPI's prefix handling. Whether
a handler is a stub comes from an AST walk of the router sources, because that
is a fact about the body and nothing in the schema can see it. The two are
joined per route and the join is asserted total — a route this module cannot
place is a failure here rather than a silent omission that would make the
comparison pass by shrinking it.

**What this does not check.** Whether a note in the third column is true. That
is prose about behaviour, and the tests that hold behaviour are the ones that
test the behaviour.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

from atp_api.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DOC = REPO_ROOT / "docs" / "API.md"
ROUTERS = REPO_ROOT / "apps" / "api" / "src" / "atp_api" / "routers"

#: The mark in the table's first column, and what it claims.
IMPLEMENTED, STUB = "✅", "🔲"

#: `| ✅ | `GET /api/v1/positions` | notes… |` — a row of a §7 table. The notes
#: are captured and deliberately never asserted on.
ROW = re.compile(
    r"^\|\s*(?P<mark>" + IMPLEMENTED + "|" + STUB + r")\s*\|\s*"
    r"`(?P<method>[A-Z]+) (?P<path>/\S*)`\s*\|(?P<notes>.*)\|\s*$"
)

#: `**49 routes, and 12 of them are stubs.**` — §7's opening claim, which is the
#: number a reader takes away without reading the table at all.
HEADLINE = re.compile(r"\*\*(\d+) routes, and (\d+) of them are stubs\.\*\*")

#: `Five of seven.` — the Strategies section's own count, in words because it is
#: a sentence. Spelled numbers are how this document writes counts in prose, so
#: the check has to speak them too.
STRATEGY_COUNT = re.compile(r"^(?P<built>\w+) of (?P<total>\w+)\.", re.MULTILINE)

WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

#: Where §7 starts and stops. Parsing between them rather than over the whole
#: file keeps a `| ✅ |` written anywhere else from quietly joining the table.
SECTION_START = "## 7. The route table"
SECTION_END = "## 8."


class Route(NamedTuple):
    method: str
    path: str
    stub: bool

    def __str__(self) -> str:  # pragma: no cover - only in failure messages
        return f"{self.method} {self.path}"


def _router_prefix(tree: ast.Module) -> str:
    """The `prefix=` given to this module's `router = APIRouter(...)`, or ``""``."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "router" for t in node.targets):
            continue
        for keyword in getattr(node.value, "keywords", []):
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
    return ""


def _raises_not_implemented(node: ast.AST) -> bool:
    """Whether this handler's body is `raise NotImplementedError`.

    Anywhere in the body, not only as the last statement: a stub that validates
    an argument before giving up is still a stub, and a handler that reaches
    this on one branch cannot be called a working route either.
    """
    return any(
        isinstance(child, ast.Raise)
        and isinstance(child.exc, ast.Name)
        and child.exc.id == "NotImplementedError"
        for child in ast.walk(node)
    )


def _declared() -> Iterator[tuple[str, str, bool]]:
    """`(method, router-relative path, is_stub)` for every decorated handler.

    Relative because `include_router`'s `/api/v1` is applied in `main.create_app`
    and is not visible here. `_resolve` puts it back from the schema rather than
    this module repeating the rule and getting to be wrong about it separately.

    `@router.websocket` is skipped: the socket is documented in §8 and has no
    row in the table, no method, and no place in an HTTP route list.
    """
    for source in sorted(ROUTERS.glob("*.py")):
        if source.name == "__init__.py":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        prefix = _router_prefix(tree)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            stub = _raises_not_implemented(node)
            for decorator in node.decorator_list:
                call = decorator.func if isinstance(decorator, ast.Call) else decorator
                if not (
                    isinstance(call, ast.Attribute)
                    and isinstance(call.value, ast.Name)
                    and call.value.id == "router"
                ):
                    continue
                if call.attr == "websocket":
                    continue
                path = ""
                if isinstance(decorator, ast.Call) and decorator.args:
                    first = decorator.args[0]
                    if isinstance(first, ast.Constant):
                        path = str(first.value)
                yield call.attr.upper(), prefix + path, stub


def _resolve(relative: str, served: set[str]) -> str | None:
    """The full path this router-relative one is served at, or None.

    Exactly one of the two candidates is a real path: business routers are
    included under `/api/v1` and the probes are not (§2). Returning None for
    anything else is what makes `test_every_declared_route_resolves` able to
    fail loudly instead of this module quietly testing a smaller set than it
    claims to.
    """
    candidates = [c for c in (relative, "/api/v1" + relative) if c in served]
    return candidates[0] if len(candidates) == 1 else None


@pytest.fixture(scope="module")
def served_paths() -> set[str]:
    """Every path the app actually serves.

    `create_app().openapi()` rather than a checked-in copy, for the reason
    `scripts/dump_openapi.py` gives: the schema is generated, and a fixture file
    would be one more thing to forget to regenerate — in a test whose entire
    subject is something being forgotten.
    """
    spec: dict[str, Any] = create_app().openapi()
    return set(spec["paths"])


@pytest.fixture(scope="module")
def source_routes(served_paths: set[str]) -> list[Route]:
    routes = [
        Route(method, resolved, stub)
        for method, relative, stub in _declared()
        if (resolved := _resolve(relative, served_paths)) is not None
    ]
    return sorted(routes, key=lambda r: (r.path, r.method))


@pytest.fixture(scope="module")
def doc_routes() -> list[Route]:
    """§7's rows, as routes."""
    text = API_DOC.read_text(encoding="utf-8")
    start = text.index(SECTION_START)
    end = text.index(SECTION_END, start)
    rows = [
        Route(m["method"], m["path"], m["mark"] == STUB)
        for line in text[start:end].splitlines()
        if (m := ROW.match(line))
    ]
    return sorted(rows, key=lambda r: (r.path, r.method))


@pytest.fixture(scope="module")
def section() -> str:
    text = API_DOC.read_text(encoding="utf-8")
    start = text.index(SECTION_START)
    return text[start : text.index(SECTION_END, start)]


def test_every_declared_route_resolves(served_paths: set[str]) -> None:
    """Every handler found in the sources is placed at a path the app serves.

    Guards the guard. If FastAPI's prefixing changes, or a router grows a
    mounting rule `_resolve` does not know, the honest outcome is this failing —
    not the comparisons below passing against whatever survived.
    """
    unresolved = [
        f"{method} {relative}"
        for method, relative, _ in _declared()
        if _resolve(relative, served_paths) is None
    ]
    assert not unresolved, (
        "these handlers could not be matched to a served path, so the table "
        f"comparison would have silently skipped them: {unresolved}"
    )


def test_the_table_lists_every_route(source_routes: list[Route], doc_routes: list[Route]) -> None:
    """A route with no row is a route the page does not admit exists."""
    missing = sorted(
        {(r.method, r.path) for r in source_routes} - {(r.method, r.path) for r in doc_routes}
    )
    assert not missing, (
        "docs/API.md §7 has no row for these routes — add one, marked "
        f"{IMPLEMENTED} or {STUB}: {[f'{m} {p}' for m, p in missing]}"
    )


def test_the_table_lists_nothing_else(source_routes: list[Route], doc_routes: list[Route]) -> None:
    """A row with no route sends a reader at a 404.

    The likely cause is a rename that moved the handler and not the row, which
    is the same drift as a stale mark wearing different clothes.
    """
    phantom = sorted(
        {(r.method, r.path) for r in doc_routes} - {(r.method, r.path) for r in source_routes}
    )
    assert not phantom, (
        "docs/API.md §7 has rows for routes that do not exist — the path may "
        f"have been renamed: {[f'{m} {p}' for m, p in phantom]}"
    )


def test_every_mark_matches_the_source(source_routes: list[Route], doc_routes: list[Route]) -> None:
    """The column that carries the whole point of the table.

    A route that stopped being a stub and kept its 🔲 understates the platform;
    one that gained a `raise NotImplementedError` and kept its ✅ tells a client
    author a 500 is a bug in their request.
    """
    by_route = {(r.method, r.path): r.stub for r in source_routes}
    wrong = [
        f"{row} is marked {STUB if row.stub else IMPLEMENTED} and is "
        f"{'a stub' if by_route[(row.method, row.path)] else 'implemented'}"
        for row in doc_routes
        if (row.method, row.path) in by_route and by_route[(row.method, row.path)] != row.stub
    ]
    assert not wrong, "docs/API.md §7 marks disagree with the source: " + "; ".join(wrong)


def test_the_headline_counts_match(section: str, source_routes: list[Route]) -> None:
    """`**49 routes, and 12 of them are stubs.**`

    Checked separately from the rows because it is read separately — it is the
    sentence somebody quotes without scrolling into the table underneath it.
    """
    claim = HEADLINE.search(section)
    assert claim is not None, (
        "§7 no longer opens with an 'N routes, and M of them are stubs' claim; "
        "keep it in a form this test can read, or move this assertion with it"
    )
    stubs = sum(1 for r in source_routes if r.stub)
    assert (int(claim.group(1)), int(claim.group(2))) == (len(source_routes), stubs)


def test_the_strategies_count_matches(section: str, source_routes: list[Route]) -> None:
    """`Five of seven.` — the one per-area count §7 states in prose.

    It sits under the table it summarises rather than above it, which is the
    one placement that does not help: a reader has already seen the rows, and
    an editor changing them has already scrolled past the sentence.
    """
    claim = STRATEGY_COUNT.search(section)
    assert claim is not None, (
        "the Strategies section no longer carries its 'N of M' count; keep it "
        "readable by this test or move this assertion with it"
    )
    built, total = claim.group("built").lower(), claim.group("total").lower()
    assert built in WORDS and total in WORDS, (
        f"'{claim.group(0)}' does not spell its numbers as words — extend WORDS "
        "or write the sentence the way the rest of the document does"
    )
    strategy_routes = [r for r in source_routes if r.path.startswith("/api/v1/strategies")]
    assert (WORDS[built], WORDS[total]) == (
        sum(1 for r in strategy_routes if r.stub),
        len(strategy_routes),
    )
