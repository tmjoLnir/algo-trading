"""`docs/ROADMAP.md`'s summary table must agree with the boxes below it.

CLAUDE.md §6 says the roadmap "is the only record of what this platform has and
has not built, which makes it worthless the moment it lags the code". A summary
section is a second copy of that record, so it inherits the same failure and
makes it easier: the boxes are edited by the PR doing the work, and a table
sitting two hundred lines above them is exactly the thing that gets forgotten.

So the summary is derived here rather than trusted. Every number in it is
recomputed from the item lines and compared. A PR that ticks a box without
updating the table fails this, which is the point — the alternative is a
headline figure that was true once.

Deliberately parsed from the rendered Markdown rather than from a data file the
document is generated from. The roadmap is read by people in a browser and
edited by hand; a generator would put the thing under test one step away from
the thing anyone actually reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROADMAP = Path(__file__).resolve().parents[2] / "docs" / "ROADMAP.md"

#: `- [x] item` / `- [ ] item`, at the start of a line. Never indented: the
#: roadmap has no nested checkboxes, and the conventions table's examples are
#: inside table cells, which start with `|`.
ITEM = re.compile(r"^- \[([ x])\] (.+)$")

#: `## Phase 4 — Execution & paper trading (requirements #1, #5)` → `4`.
PHASE_HEADING = re.compile(r"^## Phase (\d+) ")

#: A summary row: `| 4 — Execution & paper trading | 0 / 10 | 10 | … |`.
SUMMARY_ROW = re.compile(r"^\| (\d+) — [^|]+\|\s*(\d+) / (\d+)\s*\|\s*(\d+)\s*\|")

#: `| **Total** | **19 / 47** | **28** | |`.
TOTAL_ROW = re.compile(r"^\| \*\*Total\*\* \| \*\*(\d+) / (\d+)\*\* \| \*\*(\d+)\*\* \|")

#: `**19 of 47 items ticked` — the headline, which is the sentence a reader
#: takes away and therefore the one most worth pinning.
HEADLINE = re.compile(r"\*\*(\d+) of (\d+) items ticked")

#: `| Claimed, in progress (`wip`) | 21 | … |` — the state table's counts.
STATE_ROW = re.compile(
    r"^\| (Claimed, in progress|Built, awaiting the phase line|Unclaimed)[^|]*\|\s*(\d+)\s*\|"
)

#: The numbers the opening paragraph spells out in words. A literal map, kept
#: small on purpose: it exists so that sentence cannot quietly stop being true
#: while every figure in the table beside it stays right. Add an entry when the
#: count moves somewhere this does not cover — the test says so by name.
WORDS = {
    "ten": 10,
    "twelve": 12,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "twenty-one": 21,
    "twenty-two": 22,
    "twenty-three": 23,
    "twenty-four": 24,
    "twenty-five": 25,
    "twenty-six": 26,
    "twenty-seven": 27,
    "twenty-eight": 28,
    "twenty-nine": 29,
    "thirty": 30,
}

#: `Twenty of the twenty-eight sit in Phases 4 and 5` — the section's argument
#: rather than one of its figures, which is why both halves are pinned.
SPELLED_CLAIM = re.compile(
    r"([A-Za-z]+(?:-[a-z]+)?) of the ([a-z]+(?:-[a-z]+)?) sit in Phases 4 and 5"
)


@pytest.fixture(scope="module")
def lines() -> list[str]:
    return ROADMAP.read_text(encoding="utf-8").splitlines()


@pytest.fixture(scope="module")
def actual(lines: list[str]) -> dict[int, tuple[int, int]]:
    """phase number → (ticked, total), read off the item lines themselves."""
    counts: dict[int, list[int]] = {}
    phase: int | None = None
    for line in lines:
        heading = PHASE_HEADING.match(line)
        if heading:
            phase = int(heading.group(1))
            counts.setdefault(phase, [0, 0])
            continue
        item = ITEM.match(line)
        if item and phase is not None:
            counts[phase][1] += 1
            if item.group(1) == "x":
                counts[phase][0] += 1
    return {p: (c[0], c[1]) for p, c in counts.items()}


@pytest.fixture(scope="module")
def summary(lines: list[str]) -> dict[int, tuple[int, int, int]]:
    """phase number → (ticked, total, open), read off the summary table."""
    rows: dict[int, tuple[int, int, int]] = {}
    for line in lines:
        row = SUMMARY_ROW.match(line)
        if row:
            rows[int(row.group(1))] = (int(row.group(2)), int(row.group(3)), int(row.group(4)))
    return rows


class TestThePhaseTable:
    def test_every_phase_has_a_row(
        self, actual: dict[int, tuple[int, int]], summary: dict[int, tuple[int, int, int]]
    ) -> None:
        """A phase added below without a row above it is the drift this file
        exists to catch, and it is the direction drift actually happens in."""
        assert sorted(summary) == sorted(actual)

    def test_the_counts_match_the_boxes(
        self, actual: dict[int, tuple[int, int]], summary: dict[int, tuple[int, int, int]]
    ) -> None:
        for phase, (ticked, total) in sorted(actual.items()):
            assert summary[phase][:2] == (ticked, total), (
                f"Phase {phase}: the boxes say {ticked}/{total}, "
                f"the summary says {summary[phase][0]}/{summary[phase][1]}"
            )

    def test_the_open_column_is_the_remainder(
        self, summary: dict[int, tuple[int, int, int]]
    ) -> None:
        """Stated rather than derived on the page, so it can disagree with
        itself in a way a reader scanning one column would not notice."""
        for phase, (ticked, total, open_) in sorted(summary.items()):
            assert open_ == total - ticked, f"Phase {phase}: {total} - {ticked} != {open_}"


class TestTheTotals:
    def test_the_total_row_sums_the_phases(
        self, lines: list[str], actual: dict[int, tuple[int, int]]
    ) -> None:
        row = next((TOTAL_ROW.match(line) for line in lines if TOTAL_ROW.match(line)), None)
        assert row is not None, "the summary table has no **Total** row"

        ticked = sum(t for t, _ in actual.values())
        total = sum(a for _, a in actual.values())
        assert (int(row.group(1)), int(row.group(2)), int(row.group(3))) == (
            ticked,
            total,
            total - ticked,
        )

    def test_the_headline_matches(
        self, lines: list[str], actual: dict[int, tuple[int, int]]
    ) -> None:
        """The sentence in bold at the top of the section — the one figure most
        readers will take away, and the one nobody thinks to update."""
        headline = next((HEADLINE.search(line) for line in lines if HEADLINE.search(line)), None)
        assert headline is not None, "the summary has no '**N of M items ticked' headline"

        ticked = sum(t for t, _ in actual.values())
        total = sum(a for _, a in actual.values())
        assert (int(headline.group(1)), int(headline.group(2))) == (ticked, total)

    def test_the_spelled_out_claim_matches(
        self, lines: list[str], summary: dict[int, tuple[int, int, int]]
    ) -> None:
        """ "Twenty of the twenty-eight sit in Phases 4 and 5" is the section's
        actual argument, not decoration: it is what turns a discouraging count
        into a statement about one missing demonstration. Pinned so it cannot
        become false while every number in the table beside it stays right."""
        claim = next(
            (SPELLED_CLAIM.search(line) for line in lines if SPELLED_CLAIM.search(line)), None
        )
        assert claim is not None, (
            "the opening paragraph's '<N> of the <M> sit in Phases 4 and 5' claim is missing"
        )

        in_phases, of_open = (w.lower() for w in claim.groups())
        for word in (in_phases, of_open):
            assert word in WORDS, f"unrecognised number word {word!r} — add it to WORDS"

        assert WORDS[in_phases] == summary[4][2] + summary[5][2], (
            f"the paragraph says {in_phases} items sit in Phases 4 and 5; "
            f"the table says {summary[4][2] + summary[5][2]}"
        )
        assert WORDS[of_open] == sum(open_ for _, _, open_ in summary.values()), (
            f"the paragraph says there are {of_open} open items; "
            f"the table says {sum(open_ for _, _, open_ in summary.values())}"
        )


class TestTheStateTable:
    def test_the_three_states_account_for_every_open_item(
        self, lines: list[str], actual: dict[int, tuple[int, int]]
    ) -> None:
        states = {
            row.group(1): int(row.group(2)) for line in lines if (row := STATE_ROW.match(line))
        }
        assert len(states) == 3, f"expected three state rows, found {sorted(states)}"

        open_items = sum(a - t for t, a in actual.values())
        assert sum(states.values()) == open_items, (
            f"the state table accounts for {sum(states.values())} items, "
            f"but {open_items} boxes are unticked"
        )

    def test_each_state_matches_how_the_items_are_marked(self, lines: list[str]) -> None:
        """The conventions table gives three shapes an unticked line can take,
        and the summary counts them. A `wip` marker dropped when work finished —
        which is what happens when an item moves to "built, awaiting" — has to
        move a number here too."""
        wip = built = unclaimed = 0
        for line in lines:
            item = ITEM.match(line)
            if not item or item.group(1) == "x":
                continue
            text = item.group(2)
            if "(wip" in text:
                wip += 1
            elif "@" in text:
                built += 1
            else:
                unclaimed += 1

        states = {
            row.group(1): int(row.group(2)) for line in lines if (row := STATE_ROW.match(line))
        }
        assert states["Claimed, in progress"] == wip
        assert states["Built, awaiting the phase line"] == built
        assert states["Unclaimed"] == unclaimed
