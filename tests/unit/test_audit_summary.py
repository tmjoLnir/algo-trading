"""`AUDIT.md`'s summary tables must agree with the 82 findings below them.

`test_roadmap_summary.py` next door does this for `docs/ROADMAP.md`, and the
reasoning transfers without change: a summary section is a second copy of a
record, the copy sits hundreds of lines above the thing it describes, and the
copy is what gets forgotten. `AUDIT.md` §10 was written because that file had
drifted from the tree it describes; this is the check §10.6 says was still
missing when it was written.

The drift is not hypothetical here either. #128 corrected 24 stale `file:line`
citations in the finding bodies and left the *same locations* untouched in §3's
"high-severity findings at a glance" table, which carries its own copy of one
for each of the fourteen. Three of them disagreed the moment that PR merged
(findings 6, 7 and 8), and nothing could say so. `TestTheGlanceTable` is that
assertion.

The findings themselves are the record; every number in §3, the header and §8
is derived from them here rather than trusted. What this file deliberately does
*not* check is whether a finding is **true** — that is a statement about the
source tree, not about this document, and `test_audit_citations.py` takes the
part of it that can be mechanised.

Parsed from the rendered Markdown rather than from a data file, for the reason
the roadmap's test gives: this document is read in a browser and edited by hand,
and a generator would put the thing under test one step away from the thing
anyone reads.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

AUDIT = Path(__file__).resolve().parents[2] / "AUDIT.md"

#: `#### 8. DASHBOARD.md states login rate limiting is not built…`
HEADING = re.compile(r"^#### (\d+)\. (.+)$")

#: The line under each heading, which is the finding's whole machine-readable
#: state:
#:
#:     `docs/DASHBOARD.md:766` · Inconsistency · 🔴 High · ✅ Verified · 🟢 **Closed** — @claude (#113)
#:
#: Five fields: where, what kind, how bad, how well established, and — since
#: §10 — whether it is still true. The trailing `— @who (#12)` is required on a
#: closed or half-closed finding and forbidden on an open one; that is the
#: roadmap's annotation convention (`CLAUDE.md` §6) and `TestTheStateMarks`
#: holds it.
META = re.compile(
    r"^`(?P<loc>[^`]+)` · (?P<kind>\w+) · \S+ (?P<severity>\w+) · \S+ (?P<evidence>\w+)"
    r" · \S+ \*\*(?P<state>[\w-]+)\*\*(?P<annotation>.*)$"
)

#: `— @claude (#113)` — who closed it and in which PR.
ANNOTATION = re.compile(r"^ — @(\w+) \(#(\d+)\)$")

#: `## 4. High severity` … `## 6. Low severity` — the three sections the
#: findings live in. §7 onwards is prose and holds no `####` headings.
FINDING_SECTION = re.compile(r"^## [456]\. ")

#: `### apps/web (dashboard)` — the area grouping inside a severity section,
#: which is what the "By area" table counts.
AREA_HEADING = re.compile(r"^### (.+)$")

#: `**Findings:** 82 (14 high, 40 medium, 28 low)`
HEADER_COUNTS = re.compile(r"^\*\*Findings:\*\* (\d+) \((\d+) high, (\d+) medium, (\d+) low\)")

#: `open; 27 citations no longer resolve.` and the clause before it —
#: `7 closed, 1 half-closed, 74` — split across a line break in the source.
HEADER_STATE = re.compile(r"(\d+) closed, (\d+) half-closed, (\d+)\s+open;")

#: A row of the severity-by-kind table: `| 🔴 High | 12 | 2 | 0 | **14** |`.
KIND_ROW = re.compile(r"^\| \S+ (High|Medium|Low) \| (\d+) \| (\d+) \| (\d+) \| \*\*(\d+)\*\* \|")

#: Its total row: `| **Total** | **32** | **36** | **14** | **82** |`.
KIND_TOTAL = re.compile(
    r"^\| \*\*Total\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \|"
)

#: A row of the state table: `| 🔴 High | 1 | 0 | 13 | **14** |`. Shares its
#: shape with `KIND_ROW`, which is why both are matched inside their own
#: section rather than over the whole file.
STATE_ROW = KIND_ROW
STATE_TOTAL = KIND_TOTAL

#: A row of the area table: `| libs/core — backtest | 1 | 2 | 3 | 6 |`.
AREA_ROW = re.compile(r"^\| ([^|*]+?) \| (\d+) \| (\d+) \| (\d+) \| (\d+) \|$")

#: A row of the glance table:
#: `| 6 | The live runner is pinned… | `path:203` | 🔴 |`.
GLANCE_ROW = re.compile(r"^\| +(\d+) \| (.+?) \| `([^`]+)` \| (\S+) \|$")

#: The state emoji, which is the only part of a state mark the glance table has
#: room for. Keyed off the same words the meta lines use so the two cannot be
#: given different meanings.
STATE_EMOJI = {"Closed": "🟢", "Half-closed": "🟡", "Open": "🔴"}

#: `Of the 74 still open, **51 have never been re-checked by anyone**`
NEVER_RECHECKED = re.compile(
    r"Of the (\d+) still open, \*\*(\d+) have never been re-checked by anyone\*\*"
)

#: §8.1: `57 of the 82 findings are marked ⚠️ … I verified 25 myself, including
#: 8 of the 14 high-severity findings.` The sentence that tells a reader how
#: much of this document to believe, so it is the one most worth pinning — it
#: said 60 until §10, which with the 25 in the same breath was three findings
#: more than the file contains.
LIMITATION = re.compile(
    r"(\d+) of the (\d+) findings are\s+marked ⚠️.*?I verified (\d+) myself,\s+"
    r"including (\d+) of the (\d+) high-severity findings",
    re.DOTALL,
)

KINDS = ("Broken", "Inconsistency", "Redundancy")
SEVERITIES = ("High", "Medium", "Low")
EVIDENCE = ("Verified", "Reported")
STATES = ("Closed", "Half-closed", "Open")


class Finding:
    """One `####` entry, reduced to the fields the summaries are derived from."""

    def __init__(self, number: int, title: str, area: str, meta: re.Match[str]) -> None:
        self.number = number
        self.title = title
        self.area = area
        self.location = meta["loc"]
        self.kind = meta["kind"]
        self.severity = meta["severity"]
        self.evidence = meta["evidence"]
        self.state = meta["state"]
        self.annotation = meta["annotation"]

    def __repr__(self) -> str:  # pragma: no cover - test failure output only
        return f"finding {self.number} ({self.severity}, {self.state})"


@pytest.fixture(scope="module")
def lines() -> list[str]:
    return AUDIT.read_text(encoding="utf-8").splitlines()


@pytest.fixture(scope="module")
def text(lines: list[str]) -> str:
    return "\n".join(lines)


@pytest.fixture(scope="module")
def findings(lines: list[str]) -> list[Finding]:
    """Every finding, in document order, with the area heading it sits under."""
    out: list[Finding] = []
    in_findings = False
    area = ""
    for index, line in enumerate(lines):
        if line.startswith("## "):
            in_findings = bool(FINDING_SECTION.match(line))
            continue
        if not in_findings:
            continue
        heading = AREA_HEADING.match(line)
        if heading:
            area = heading.group(1)
            continue
        title = HEADING.match(line)
        if not title:
            continue
        meta = META.match(lines[index + 2])
        assert meta is not None, (
            f"finding {title.group(1)}'s meta line is not in the documented shape "
            f"(`path:line` · Kind · Severity · Evidence · State): {lines[index + 2]!r}"
        )
        out.append(Finding(int(title.group(1)), title.group(2), area, meta))
    return out


def _section(lines: list[str], heading: str) -> list[str]:
    """The lines under a `###` heading, up to the next heading of any level."""
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    end = next(
        (i for i, line in enumerate(lines[start + 1 :], start + 1) if line.startswith("#")),
        len(lines),
    )
    return lines[start:end]


class TestTheFindingsParse:
    """Nothing below means anything if the entries cannot be read, so this runs
    first and states the shape everything else assumes."""

    def test_the_numbering_is_contiguous(self, findings: list[Finding]) -> None:
        """A duplicated or skipped number silently changes every count."""
        assert [f.number for f in findings] == list(range(1, len(findings) + 1))

    def test_every_field_is_one_of_its_documented_values(self, findings: list[Finding]) -> None:
        """§2 gives the vocabulary. A typo'd mark would otherwise vanish from
        every table here while still rendering as a finding in a browser."""
        for f in findings:
            assert f.kind in KINDS, f"{f}: unknown kind {f.kind!r}"
            assert f.severity in SEVERITIES, f"{f}: unknown severity {f.severity!r}"
            assert f.evidence in EVIDENCE, f"{f}: unknown evidence mark {f.evidence!r}"
            assert f.state in STATES, f"{f}: unknown state {f.state!r}"


class TestTheStateMarks:
    """§2 says a state is annotated with the PR that earned it, "in the same
    diff, exactly as a roadmap tick is". That is the convention `CLAUDE.md` §6
    imposes on `- [x]`, and it is worth no less here: a finding marked closed
    with nothing to look up is the `(wip)` marker with no number all over
    again — a claim with no way to check it."""

    def test_a_closed_finding_names_who_and_which_pr(self, findings: list[Finding]) -> None:
        for f in findings:
            if f.state == "Open":
                continue
            assert ANNOTATION.match(f.annotation), (
                f"{f} is marked {f.state} but carries no `— @who (#12)` annotation: "
                f"{f.annotation!r}"
            )

    def test_an_open_finding_claims_nobody(self, findings: list[Finding]) -> None:
        """The other direction, and the one that rots quietly: an annotation
        left behind when a state was walked back reads as work that was done."""
        for f in findings:
            if f.state == "Open":
                assert f.annotation == "", f"{f} is open but carries {f.annotation!r}"


class TestTheHeader:
    def test_the_finding_count_and_severity_split_match(
        self, lines: list[str], findings: list[Finding]
    ) -> None:
        """The first numbers a reader sees, three lines in."""
        row = next((HEADER_COUNTS.match(line) for line in lines if HEADER_COUNTS.match(line)), None)
        assert row is not None, "the header has no '**Findings:** N (…)' line"

        by_severity = Counter(f.severity for f in findings)
        assert (int(row.group(1)), int(row.group(2)), int(row.group(3)), int(row.group(4))) == (
            len(findings),
            by_severity["High"],
            by_severity["Medium"],
            by_severity["Low"],
        )

    def test_the_state_summary_matches(self, text: str, findings: list[Finding]) -> None:
        """`7 closed, 1 half-closed, 74 open` — added by §10, and the sentence
        that decides whether a reader treats this file as current."""
        row = HEADER_STATE.search(text)
        assert row is not None, "the header has no 'N closed, N half-closed, N open' clause"

        by_state = Counter(f.state for f in findings)
        assert (int(row.group(1)), int(row.group(2)), int(row.group(3))) == (
            by_state["Closed"],
            by_state["Half-closed"],
            by_state["Open"],
        )


class TestTheSeverityAndKindTable:
    def test_every_cell_matches(self, lines: list[str], findings: list[Finding]) -> None:
        rows = [KIND_ROW.match(line) for line in _section(lines, "### By severity and kind")]
        cells = {r.group(1): r for r in rows if r}
        assert sorted(cells) == sorted(SEVERITIES)

        actual = Counter((f.severity, f.kind) for f in findings)
        for severity, row in cells.items():
            for column, kind in enumerate(KINDS, start=2):
                assert int(row.group(column)) == actual[(severity, kind)], (
                    f"{severity} x {kind}: the table says {row.group(column)}, "
                    f"the findings say {actual[(severity, kind)]}"
                )
            assert int(row.group(5)) == sum(actual[(severity, k)] for k in KINDS)

    def test_the_total_row_matches(self, lines: list[str], findings: list[Finding]) -> None:
        section = _section(lines, "### By severity and kind")
        total = next((KIND_TOTAL.match(line) for line in section if KIND_TOTAL.match(line)), None)
        assert total is not None, "the severity-by-kind table has no **Total** row"

        by_kind = Counter(f.kind for f in findings)
        assert [int(total.group(i)) for i in (1, 2, 3, 4)] == [by_kind[k] for k in KINDS] + [
            len(findings)
        ]


class TestTheStateTable:
    """§10 added this table and, in the same breath, said nothing enforced it.
    This is what closes that."""

    def test_every_cell_matches(self, lines: list[str], findings: list[Finding]) -> None:
        rows = [STATE_ROW.match(line) for line in _section(lines, "### By state")]
        cells = {r.group(1): r for r in rows if r}
        assert sorted(cells) == sorted(SEVERITIES)

        actual = Counter((f.severity, f.state) for f in findings)
        for severity, row in cells.items():
            for column, state in enumerate(STATES, start=2):
                assert int(row.group(column)) == actual[(severity, state)], (
                    f"{severity} x {state}: the table says {row.group(column)}, "
                    f"the findings say {actual[(severity, state)]}"
                )
            assert int(row.group(5)) == sum(actual[(severity, s)] for s in STATES)

    def test_the_total_row_matches(self, lines: list[str], findings: list[Finding]) -> None:
        section = _section(lines, "### By state")
        total = next((STATE_TOTAL.match(line) for line in section if STATE_TOTAL.match(line)), None)
        assert total is not None, "the state table has no **Total** row"

        by_state = Counter(f.state for f in findings)
        assert [int(total.group(i)) for i in (1, 2, 3, 4)] == [by_state[s] for s in STATES] + [
            len(findings)
        ]

    def test_the_never_rechecked_count_matches(self, text: str, findings: list[Finding]) -> None:
        """ "51 have never been re-checked by anyone" is the sentence that says
        how much of the open set is a lead rather than a defect. It moves when
        a finding is verified *or* when one is closed, so it has two ways to go
        stale and no obvious moment for either."""
        claim = NEVER_RECHECKED.search(text)
        assert claim is not None, "§3 has no 'Of the N still open, **M have never…**' sentence"

        still_open = [f for f in findings if f.state == "Open"]
        unverified = [f for f in still_open if f.evidence == "Reported"]
        assert (int(claim.group(1)), int(claim.group(2))) == (len(still_open), len(unverified))


class TestTheAreaTable:
    def test_every_area_row_matches_its_section(
        self, lines: list[str], findings: list[Finding]
    ) -> None:
        """The areas are `###` headings inside §4–§6, so a finding filed under
        the wrong one is invisible in the body and visible only here."""
        rows = [AREA_ROW.match(line) for line in _section(lines, "### By area")]
        table = {r.group(1): [int(r.group(i)) for i in (2, 3, 4, 5)] for r in rows if r}

        actual = Counter((f.area, f.severity) for f in findings)
        areas = {f.area for f in findings}
        assert set(table) == areas, (
            f"the table lists {sorted(set(table) - areas)} that no finding is filed under, "
            f"and omits {sorted(areas - set(table))}"
        )

        for area, (high, medium, low, total) in table.items():
            counts = [actual[(area, s)] for s in SEVERITIES]
            assert [high, medium, low] == counts, (
                f"{area}: the table says {[high, medium, low]}, the findings say {counts}"
            )
            assert total == sum(counts), f"{area}: {sum(counts)} findings, row totals {total}"


class TestTheGlanceTable:
    """§3's "high-severity findings at a glance" carries a *second copy* of each
    high finding's title and location. #128 re-anchored 24 citations in the
    bodies and left this table alone; findings 6, 7 and 8 disagreed with
    themselves from the moment it merged. A duplicated citation is a citation
    that will drift."""

    def test_it_lists_exactly_the_high_findings_in_order(
        self, lines: list[str], findings: list[Finding]
    ) -> None:
        rows = [GLANCE_ROW.match(line) for line in _section(lines, "### The high-severity")]
        numbers = [int(r.group(1)) for r in rows if r]
        assert numbers == [f.number for f in findings if f.severity == "High"]

    def test_every_location_agrees_with_the_finding(
        self, lines: list[str], findings: list[Finding]
    ) -> None:
        by_number = {f.number: f for f in findings}
        for row in (GLANCE_ROW.match(line) for line in _section(lines, "### The high-severity")):
            if not row:
                continue
            finding = by_number[int(row.group(1))]
            assert row.group(3) == finding.location, (
                f"finding {finding.number}: the glance table cites `{row.group(3)}`, "
                f"the finding itself cites `{finding.location}`"
            )

    def test_every_title_agrees_with_the_finding(
        self, lines: list[str], findings: list[Finding]
    ) -> None:
        """Titles are edited when a finding is understood better. The copy up
        here is not, unless something says so."""
        by_number = {f.number: f for f in findings}
        for row in (GLANCE_ROW.match(line) for line in _section(lines, "### The high-severity")):
            if not row:
                continue
            finding = by_number[int(row.group(1))]
            assert row.group(2) == finding.title, (
                f"finding {finding.number}: the glance table and the finding have different titles"
            )

    def test_every_state_agrees_with_the_finding(
        self, lines: list[str], findings: list[Finding]
    ) -> None:
        """A closed finding still belongs in a list of what was high severity —
        it is history — but a reader scanning that list should not have to open
        the finding to learn it is closed."""
        by_number = {f.number: f for f in findings}
        for row in (GLANCE_ROW.match(line) for line in _section(lines, "### The high-severity")):
            if not row:
                continue
            finding = by_number[int(row.group(1))]
            assert row.group(4) == STATE_EMOJI[finding.state], (
                f"finding {finding.number}: the glance table shows {row.group(4)}, "
                f"the finding is {finding.state}"
            )


class TestTheLimitations:
    def test_section_eight_still_describes_this_document(
        self, text: str, findings: list[Finding]
    ) -> None:
        """§8.1 is the caveat the whole document rests on — how many of these
        findings are leads rather than defects. It was wrong on the day it was
        written, by three."""
        claim = LIMITATION.search(text)
        assert claim is not None, "§8.1's '<N> of the <M> findings are marked ⚠️' sentence is gone"

        reported = [f for f in findings if f.evidence == "Reported"]
        verified = [f for f in findings if f.evidence == "Verified"]
        high = [f for f in findings if f.severity == "High"]
        assert int(claim.group(1)) == len(reported), (
            f"§8.1 says {claim.group(1)} findings are unverified; {len(reported)} are"
        )
        assert int(claim.group(2)) == len(findings)
        assert int(claim.group(3)) == len(verified)
        assert int(claim.group(4)) == len([f for f in high if f.evidence == "Verified"])
        assert int(claim.group(5)) == len(high)
