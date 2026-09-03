"""Every `file:line` in `AUDIT.md` must still point at something.

`scripts/check_roadmap_wip.py` exists because `wip #12` is a claim about GitHub
that no assertion about the document could check. A finding's `file:line` is the
same kind of claim, aimed at the tree instead: true when written, and silently
false the moment somebody inserts a line above it. On 2026-09-02, 27 of the 82
citations in this file no longer resolved (§10.4) — one of them had never
resolved at all — and nothing had been able to notice for six days.

It is the *cheaper* half of the same question, which is why this is a unit test
and not a CI gate with a token. The roadmap's `wip` state lives on GitHub, so
reading it needs a network, and a check that goes red when the network is down
teaches people to re-run it (ADR 0024). A citation's target is in the checkout.
There is no unreachable state, so there is no reason to be lenient: this runs
offline on every commit and is red when a citation is broken, full stop.

**What it does not check.** That the cited line still holds what the finding
says it holds — that needs a human, and pretending otherwise would be worse
than not checking. What it catches is the failure that actually happened:
a line number that has quietly stopped addressing anything.

The other half of the pair is `test_audit_summary.py`, which holds §3's tables
against the findings — including the glance table's *second* copy of each
high-severity location. A citation duplicated in two places drifts in two
places, so the two files split it: agreement there, resolution here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "AUDIT.md"

#: `#### 12. The nightly sweep never re-fetches…`
HEADING = re.compile(r"^#### (\d+)\. ")

#: The meta line's location field. Deliberately narrower than the one in
#: `test_audit_summary.py`: that file cares about all five fields, this one only
#: about where the finding points.
LOCATION = re.compile(r"^`([^`]+):(\d+)` · ")

#: A finding may carry italic notes beneath its meta line, one per review that
#: touched it. The section number is a capture rather than a literal `10`: a
#: second review has to be able to leave a note the same way the first did, and
#: pinning it to §10 would have meant either rewriting §10's notes — deleting
#: the history they exist to keep — or leaving the new ones unparsed, which is
#: the same as not writing them. The assertions below are unchanged.
#:
#: The *first* note under a finding wins, so notes are written newest-first and
#: the current position is the one this checks.
RECORD_NOTE = re.compile(r"^\*Record note \(§\d+, [\d-]+\): (.+)\*$")

#: The declaration that a citation addresses a *file* rather than a line — the
#: finding is that something is missing from it, so the number is decoration.
#: Spelled as a token rather than inferred from the prose: an exemption that a
#: reader can grant by accident, by phrasing a note a particular way, is not an
#: exemption anybody decided to grant.
ABSENCE = "**Cites an absence.**"

#: `Re-pointed from `:23` to `:24`` / `Cited `:713` on 2026-08-27; the code is
#: at `:766` today.` — a note claiming the citation moved. Both halves are
#: checked: the new line has to be the one on the meta line above, and it has
#: to differ from the old one, or the note is describing a move that did not
#: happen.
RELOCATION = re.compile(r"(?:Cited|Re-pointed from) `:(\d+)`(?:.*?)`:(\d+)`")

#: The findings whose citation addresses a file rather than a line. Held as a
#: literal so that granting a third one is a decision somebody makes on purpose,
#: in a diff, with this comment in front of them — the same reason
#: `test_roadmap_summary.py` keeps its number words in a map. Both of these are
#: findings *about an absence*: 36 is that no CI job runs `make check-tracked`,
#: 81 is that `tests/e2e/` holds no tests. Neither has a line to point at,
#: and 81 never did — `tests/e2e/__init__.py` has been zero bytes since it was
#: committed.
CITES_AN_ABSENCE = frozenset({36, 81})


class Citation:
    def __init__(self, number: int, path: str, line: int, note: str | None) -> None:
        self.number = number
        self.path = path
        self.line = line
        self.note = note

    @property
    def declares_an_absence(self) -> bool:
        return self.note is not None and ABSENCE in self.note

    @property
    def target(self) -> Path:
        return ROOT / self.path

    def __repr__(self) -> str:  # pragma: no cover - test failure output only
        return f"finding {self.number} (`{self.path}:{self.line}`)"


@pytest.fixture(scope="module")
def citations() -> list[Citation]:
    lines = AUDIT.read_text(encoding="utf-8").splitlines()
    out: list[Citation] = []
    for index, line in enumerate(lines):
        heading = HEADING.match(line)
        if not heading:
            continue
        location = LOCATION.match(lines[index + 2])
        assert location is not None, (
            f"finding {heading.group(1)} does not open with a `path:line` citation: "
            f"{lines[index + 2]!r}"
        )
        note = next(
            (
                match.group(1)
                for candidate in lines[index + 3 : index + 6]
                if (match := RECORD_NOTE.match(candidate))
            ),
            None,
        )
        out.append(Citation(int(heading.group(1)), location.group(1), int(location.group(2)), note))
    assert out, "no findings parsed out of AUDIT.md"
    return out


class TestEveryCitationResolves:
    def test_the_file_exists(self, citations: list[Citation]) -> None:
        """No exemption for this one, including for a finding that cites an
        absence: those still name a real file that something is missing *from*.
        A path that has been renamed or deleted is a dead citation however the
        finding is worded."""
        for citation in citations:
            assert citation.target.is_file(), f"{citation}: no such file"

    def test_the_line_is_in_the_file(self, citations: list[Citation]) -> None:
        """The failure that actually happened, twenty-six times over. A line
        number past the end of a file addresses nothing; one that has drifted
        inside it addresses the wrong thing, and no test can tell — which is
        why this checks the half that is checkable and says so."""
        for citation in citations:
            if citation.declares_an_absence:
                continue
            length = len(citation.target.read_text(encoding="utf-8").splitlines())
            assert 1 <= citation.line <= length, (
                f"{citation}: the file has {length} lines. Either re-anchor the citation "
                f"or, if the finding is that something is missing from this file rather "
                f"than wrong in it, say so with '{ABSENCE}' in its record note."
            )


class TestTheAbsenceExemption:
    """An exemption nobody counts is an exemption that spreads."""

    def test_only_the_declared_findings_claim_it(self, citations: list[Citation]) -> None:
        claimed = {c.number for c in citations if c.declares_an_absence}
        assert claimed == set(CITES_AN_ABSENCE), (
            f"findings {sorted(claimed ^ set(CITES_AN_ABSENCE))} changed whether they cite an "
            f"absence. That is a real decision — update CITES_AN_ABSENCE and its comment."
        )

    def test_each_one_actually_says_so(self, citations: list[Citation]) -> None:
        """The literal above and the document have to agree in both directions:
        a finding listed here that quietly lost its declaration would be exempt
        from `test_the_line_is_in_the_file` for no stated reason."""
        for citation in citations:
            if citation.number in CITES_AN_ABSENCE:
                assert citation.declares_an_absence, (
                    f"{citation} is listed in CITES_AN_ABSENCE but its record note does not "
                    f"contain '{ABSENCE}'"
                )


class TestTheRecordNotes:
    """§10 re-anchored 24 citations and left a note under each one saying where
    it used to point. The notes are a third copy of the line number, so they
    drift like the other two."""

    def test_a_relocation_note_matches_the_citation_above_it(
        self, citations: list[Citation]
    ) -> None:
        for citation in citations:
            if citation.note is None:
                continue
            move = RELOCATION.search(citation.note)
            if move is None:
                continue
            was, now = int(move.group(1)), int(move.group(2))
            assert now == citation.line, (
                f"{citation}: the note says the code is at `:{now}`, "
                f"but the citation reads `:{citation.line}`"
            )
            assert was != now, (
                f"{citation}: the note describes a move from `:{was}` to `:{now}`, "
                f"which is not a move"
            )
