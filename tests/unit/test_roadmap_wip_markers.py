"""`scripts/check_roadmap_wip.py` — the half of the roadmap that is a claim
about GitHub rather than about the document.

`test_roadmap_summary.py` next door proves the summary table agrees with the
boxes below it, and it passed on a file in which **every single `wip` marker was
false**. It had to: every assertion in it is a statement about the document, and
`wip #12` asserts something about a pull request. The two files divide the
roadmap between them along exactly that line.

The fixtures here are not invented. Each one is a marker that was actually in
`docs/ROADMAP.md` on 2026-09-02, with the state its PR was actually in, so the
suite fails on the real incident rather than on a plausible imitation of it:

- `wip #30` — merged sixteen days earlier, the ordinary case, twenty of them.
- `wip #40` — **closed without ever merging**. The work had landed in #41 half
  an hour later, so the marker pointed at the abandoned attempt and read as work
  dropped rather than work done. The single most misleading line in the file.
- `(wip)` with no number — against the convention's own `(wip #12)` format, so
  there was nothing to look up at all. Two of these, for seventeen days.

`test_a_state_that_cannot_be_read_is_not_a_finding` is the one to keep. This
check runs in CI against a network, and the failure it must never have is going
red because GitHub was unreachable: a check that cries wolf teaches people that
red means "re-run me", which is how the `stack` job's sampled restart check came
to certify a worker that could not boot (ADR 0024). Absence of evidence is not
evidence, and here it is not even a warning that fails anything.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str) -> Any:
    """Import a script by path — `scripts/` is a set of entry points, not a
    package. Same approach as `test_compose_shape.py`, for the same reason."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = _load("check_roadmap_wip")


def _fetcher(states: dict[int, str | None]) -> Any:
    """A stand-in for GitHub. Anything not named answers None — the same thing
    the real one says when it cannot reach the API."""

    def fetch(number: int) -> str | None:
        return states.get(number)

    return fetch


class TestParsing:
    def test_a_wip_marker_yields_its_pr_numbers(self) -> None:
        markers = check.parse_wip_markers(
            ["- [ ] Position sizing, all methods — @claude (wip #30)."]
        )
        assert [m.pr_numbers for m in markers] == [(30,)]

    def test_an_item_claimed_against_several_prs_yields_all_of_them(self) -> None:
        """`(wip #50, #51, #100)` was a real line. Checking only the first would
        have passed it while two of the three had merged."""
        markers = check.parse_wip_markers(
            ["- [ ] Deployment target chosen; secrets manager — @claude (wip #50, #51, #100)."]
        )
        assert markers[0].pr_numbers == (50, 51, 100)

    def test_a_ticked_item_is_not_a_wip_marker(self) -> None:
        """`- [x] … (#12)` names the PR that *closed* the item, which is merged
        by definition. Reading those would report every completed item as a
        finding."""
        assert check.parse_wip_markers(["- [x] Repo structure, tooling, docs, CI (#1, #2)"]) == []

    def test_the_conventions_table_is_not_an_item(self) -> None:
        """The row documenting the format contains a literal `(wip #12)` and
        starts with `|`, not `- [ ]`. Matching it would make the file
        permanently red against a PR that has nothing to do with it."""
        row = "| `- [ ] item — @who (wip #12)` | Claimed and in progress |"
        assert check.parse_wip_markers([row]) == []

    def test_an_indented_line_is_not_an_item(self) -> None:
        assert check.parse_wip_markers(["  - [ ] a nested note — @claude (wip #7)"]) == []


class TestMarkerFormat:
    def test_a_wip_with_no_pr_number_is_reported(self) -> None:
        markers = check.parse_wip_markers(
            ["- [ ] Redis quote cache, pub/sub publisher, staleness monitor — @claude (wip)."]
        )
        problems = check.check_marker_format(markers)
        assert len(problems) == 1
        assert "no PR number" in problems[0]

    def test_a_well_formed_marker_is_not_reported(self) -> None:
        markers = check.parse_wip_markers(["- [ ] Reconciliation — @claude (wip #38)."])
        assert check.check_marker_format(markers) == []

    def test_the_committed_roadmap_has_no_numberless_wip_markers(self) -> None:
        """The assertion that actually guards the file, run offline on every
        commit. Two markers read `— @claude (wip).` for seventeen days; nothing
        could check them, because a marker with no number has nothing to look
        up."""
        markers = check.parse_wip_markers(
            (ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8").splitlines()
        )
        assert check.check_marker_format(markers) == []


class TestPullRequestState:
    def test_a_merged_pr_contradicts_wip(self) -> None:
        """The ordinary case, and there were twenty of them."""
        markers = check.parse_wip_markers(["- [ ] Position sizing — @claude (wip #30)."])
        problems, notes = check.check_pr_states(markers, _fetcher({30: "merged"}))
        assert notes == []
        assert len(problems) == 1
        assert "#30" in problems[0]
        assert "merged" in problems[0]

    def test_a_pr_closed_without_merging_also_contradicts_wip(self) -> None:
        """#40 was closed and never merged; #41 carried the work in half an hour
        later. Treating only `merged` as a finding would have left the single
        most misleading marker in the file untouched — the one that reads as
        work abandoned."""
        markers = check.parse_wip_markers(["- [ ] Worker wired to trade — @claude (wip #40)."])
        problems, _ = check.check_pr_states(markers, _fetcher({40: "closed"}))
        assert len(problems) == 1
        assert "closed" in problems[0]

    def test_an_open_pr_is_what_wip_is_for(self) -> None:
        markers = check.parse_wip_markers(["- [ ] Strategy creation endpoint — @claude (wip #97)."])
        problems, notes = check.check_pr_states(markers, _fetcher({97: "open"}))
        assert (problems, notes) == ([], [])

    def test_a_state_that_cannot_be_read_is_not_a_finding(self) -> None:
        """No network, no token, a rate limit, a 404 — all of them arrive here as
        None, and none of them is evidence that a marker is wrong. This check
        goes red only when it can name a finished PR."""
        markers = check.parse_wip_markers(["- [ ] Reconciliation — @claude (wip #38)."])
        problems, notes = check.check_pr_states(markers, _fetcher({}))
        assert problems == []
        assert len(notes) == 1
        assert "unreadable" in notes[0]

    def test_every_pr_on_a_multi_pr_marker_is_checked(self) -> None:
        markers = check.parse_wip_markers(["- [ ] Deployment — @claude (wip #50, #51, #100)."])
        problems, _ = check.check_pr_states(
            markers, _fetcher({50: "open", 51: "merged", 100: "merged"})
        )
        assert len(problems) == 2

    def test_the_state_of_the_file_on_the_day_this_was_written(self) -> None:
        """End to end over the shapes that were actually in the file, against the
        states those PRs were actually in: twenty ordinary merges, one closed
        without merging, one genuinely open, two with nothing to look up."""
        lines = [
            "- [ ] Real-time WS ingestor — @claude (wip).",
            "- [ ] Redis quote cache — @claude (wip).",
            "- [ ] Position sizing, all methods — @claude (wip #30).",
            "- [ ] Worker wired to trade — @claude (wip #40).",
            "- [ ] Strategy creation endpoint — @claude (wip #97).",
            "- [x] Repo structure, tooling, docs, CI (#1, #2)",
        ]
        markers = check.parse_wip_markers(lines)
        assert len(markers) == 5

        format_problems = check.check_marker_format(markers)
        state_problems, _ = check.check_pr_states(
            markers, _fetcher({30: "merged", 40: "closed", 97: "open"})
        )
        assert len(format_problems) == 2
        assert len(state_problems) == 2
        assert not any("#97" in problem for problem in state_problems)
