#!/usr/bin/env python3
"""Fail if a `wip` marker in `docs/ROADMAP.md` names a PR that is no longer open.

`docs/ROADMAP.md` gives three shapes an unticked line can take, and the middle
one carries a claim about the outside world:

    - [ ] item                      not started, unclaimed
    - [ ] item — @who (wip #12)     claimed and IN PROGRESS
    - [ ] item — @who               built, waiting on the phase's Verifiable: line

`tests/unit/test_roadmap_summary.py` already keeps the summary table honest
against the boxes below it. What nothing checked is whether `#12` is still open.
It could not: every assertion in that file is a statement about the document,
and "is somebody still working on this?" is a statement about GitHub.

**That gap was not hypothetical.** On 2026-09-02 all twenty-one `wip` markers in
the file named PRs that had already merged — the oldest seventeen days and about
ninety PRs earlier, the newest the day before, added by the very PR that
finished the item it marked. One of them (`wip #40`) named a pull request that
was *closed without ever merging*; the work had landed in #41 half an hour
later, so the marker read as work abandoned rather than work done. Two carried
no number at all, against the convention's own format, leaving nothing to look
up. The file was internally consistent and externally false at the same time,
and stayed that way because the only thing that could have noticed was a person
remembering to look (#125, #126).

## What it refuses to do

**Absence of evidence never fails the build.** `fetch_pr_state` answers `None`
for anything that is not a definite reading — no network, no token, a rate
limit, a 404, a malformed body — and an unknown state is reported as a note
rather than a problem. A check that goes red when GitHub is unreachable would
teach everyone that red means "try again", which is exactly how the `stack`
job's sampled restart check came to be ignored (ADR 0024). This one is red only
when it can point at a merged or closed pull request by number.

So the two halves run in different places for different reasons. The format
half is pure and lives in the unit suite, where it runs offline on every commit
forever. The state half needs the network and runs as its own CI gate, where a
token exists and a failure to reach GitHub is a skip.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO = "tmjoLnir/algo-trading"
ROADMAP = Path(__file__).resolve().parents[1] / "docs" / "ROADMAP.md"

#: `- [ ] item` / `- [x] item`, never indented — the same shape
#: `test_roadmap_summary.py` parses, and for the same reason: the conventions
#: table's examples live inside table cells, which start with `|`.
ITEM = re.compile(r"^- \[([ x])\] (.+)$")

#: `#12`, `#50, #51, #100`. Deliberately finds every reference on the line: an
#: item may be claimed against more than one PR, and all of them are claims.
PR_REF = re.compile(r"#(\d+)")

#: States that contradict `wip`. `open` is the only one that does not.
CLOSED_STATES = frozenset({"merged", "closed"})


@dataclass(frozen=True)
class WipMarker:
    """One `- [ ] … (wip …)` line, and the PR numbers it claims against."""

    line_number: int
    text: str
    pr_numbers: tuple[int, ...]


def parse_wip_markers(lines: list[str]) -> list[WipMarker]:
    """Every unticked item carrying a `wip` marker, in file order.

    Ticked items are skipped on purpose. `- [x] item — @who (#12)` names the PR
    that *closed* the item, which is a merged PR by definition and would be a
    finding in every single case.
    """
    markers = []
    for index, line in enumerate(lines, start=1):
        item = ITEM.match(line)
        if item is None or item.group(1) == "x":
            continue
        text = item.group(2)
        if "(wip" not in text:
            continue
        numbers = tuple(int(n) for n in PR_REF.findall(text))
        markers.append(WipMarker(line_number=index, text=text, pr_numbers=numbers))
    return markers


def check_marker_format(markers: list[WipMarker]) -> list[str]:
    """Report every `wip` marker that names no PR at all.

    The convention is `(wip #12)`; a bare `(wip)` cannot be checked by anything,
    by a person or by `check_pr_states` below, because there is nothing to look
    up. Two of these sat in Phase 1 for seventeen days.
    """
    return [
        f"docs/ROADMAP.md:{marker.line_number}: `wip` with no PR number — "
        f"the convention is `(wip #12)`, so there is nothing to look up: {marker.text[:70]}"
        for marker in markers
        if not marker.pr_numbers
    ]


def check_pr_states(
    markers: list[WipMarker],
    fetch: Callable[[int], str | None],
) -> tuple[list[str], list[str]]:
    """Report every `wip` marker whose PR has merged or been closed.

    Returns `(problems, notes)`. A PR whose state cannot be read lands in
    `notes` and does not fail anything — see the module docstring.
    """
    problems: list[str] = []
    notes: list[str] = []
    for marker in markers:
        for number in marker.pr_numbers:
            state = fetch(number)
            if state is None:
                notes.append(f"docs/ROADMAP.md:{marker.line_number}: #{number} — state unreadable")
            elif state in CLOSED_STATES:
                problems.append(
                    f"docs/ROADMAP.md:{marker.line_number}: `wip #{number}` names a PR that is "
                    f"**{state}**, so it is not in progress: {marker.text[:70]}"
                )
    return problems, notes


def fetch_pr_state(number: int, *, repo: str = REPO, token: str | None = None) -> str | None:
    """`open`, `merged`, `closed`, or None when this cannot be answered.

    None is not an error path so much as the honest answer to "is #12 open?"
    when nothing could be reached. Every failure — offline, rate-limited,
    unauthorised, a 404, a body that is not the JSON we expect — collapses to
    it, because none of them is evidence that a marker is wrong.
    """
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls/{number}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "atp-roadmap-check",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("merged_at"):
        return "merged"
    state = payload.get("state")
    return state if isinstance(state, str) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="check only the marker format; do not ask GitHub whether the PRs are open",
    )
    parser.add_argument("--repo", default=REPO)
    args = parser.parse_args()

    markers = parse_wip_markers(ROADMAP.read_text(encoding="utf-8").splitlines())
    print(f"{len(markers)} `wip` marker(s) in docs/ROADMAP.md")

    problems = check_marker_format(markers)
    notes: list[str] = []

    if args.offline:
        print("offline: not asking GitHub whether the PRs are still open")
    else:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            print("no GITHUB_TOKEN — unauthenticated, which is rate limited to 60 requests/hour")

        def fetch(number: int) -> str | None:
            return fetch_pr_state(number, repo=args.repo, token=token)

        state_problems, notes = check_pr_states(markers, fetch)
        problems += state_problems

    for note in notes:
        print(f"  note: {note}")
    if notes and not problems:
        print()
        print("Some states could not be read, and that is not a finding: this check goes")
        print("red only when it can name a merged or closed PR. A check that failed")
        print("because GitHub was unreachable would teach everyone to re-run it.")

    if not problems:
        print("every `wip` marker names a PR that is still open")
        return 0

    print()
    print("A `wip` marker means somebody is on it, so nobody duplicates the work.")
    print("These name pull requests that are finished, which is a different sentence:")
    print()
    for problem in problems:
        print(f"  {problem}")
    print()
    print("If the work is done and the tick is waiting on the phase's *Verifiable:*")
    print("line, the marker is `— @who (#12)` with no `wip` — and the state table in")
    print("'Where this stands' moves with it, or test_roadmap_summary.py fails.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
