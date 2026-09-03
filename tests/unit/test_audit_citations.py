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
RECORD_NOTE = re.compile(r"^\*Record note \(§(\d+), [\d-]+\): (.+)\*$")

#: A line that *starts* a note, whether or not it is well formed. The gap
#: between this and RECORD_NOTE is what `test_every_note_is_readable` asserts
#: away: a note wrapped across two lines matches this and not RECORD_NOTE, so
#: without it a note can be present, look right in a browser, and be read by
#: nothing. That is exactly what happened to finding 82.
NOTE_OPENS = re.compile(r"^\*Record note \(§\d+")

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
    def __init__(
        self, number: int, path: str, line: int, notes: list[tuple[int, str]], note_lines: int
    ) -> None:
        self.number = number
        self.path = path
        self.line = line
        #: (section number, text) for each note that parsed, in document order.
        self.notes = notes
        #: How many lines *look* like a note, parsed or not. A gap between this
        #: and len(notes) is a note no assertion can read.
        self.note_lines = note_lines

    @property
    def note(self) -> str | None:
        """The newest note — the one that describes the citation as it stands.

        Notes are written newest-first, so this is the highest section number,
        not the first in the file. Taking the max rather than the first entry
        means a note accidentally filed out of order still reads correctly here,
        and `test_notes_run_newest_first` is what complains about the ordering.
        """
        return max(self.notes)[1] if self.notes else None

    @property
    def declares_an_absence(self) -> bool:
        return any(ABSENCE in text for _, text in self.notes)

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
        # Every note under this finding, in document order, up to its Evidence
        # block. A fixed lookahead window was what stood here, and it had two
        # holes that showed up the first time a second review left notes: a
        # multi-line note matched nothing (RECORD_NOTE is anchored on one line),
        # and an older note pushed past the window vanished silently. Both are
        # now failures rather than blind spots — see TestTheRecordNotes.
        raw: list[str] = []
        parsed: list[tuple[int, str]] = []
        for candidate in lines[index + 3 :]:
            if candidate.startswith("**Evidence**") or candidate.startswith("####"):
                break
            if NOTE_OPENS.match(candidate):
                raw.append(candidate)
            if match := RECORD_NOTE.match(candidate):
                parsed.append((int(match.group(1)), match.group(2)))
        out.append(
            Citation(
                int(heading.group(1)),
                location.group(1),
                int(location.group(2)),
                parsed,
                len(raw),
            )
        )
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

    def test_every_note_is_readable(self, citations: list[Citation]) -> None:
        """A note the parser cannot read is a note no assertion can hold.

        It renders correctly in a browser, so nothing about it looks wrong; only
        this comparison says so. Finding 82's §11 note was wrapped across five
        lines on the day this check did not exist, which silently took *both* of
        that finding's notes out of every assertion below.
        """
        for citation in citations:
            assert citation.note_lines == len(citation.notes), (
                f"{citation} has {citation.note_lines} record-note line(s) but "
                f"{len(citation.notes)} that parse — a note wrapped across lines, or "
                f"otherwise malformed, is read by nothing. Keep each note on one line."
            )

    def test_notes_run_newest_first(self, citations: list[Citation]) -> None:
        """The newest note describes the citation as it stands; older ones are
        history beneath it. Out of order, a reader meets a superseded line
        number first and has no way to know it is superseded."""
        for citation in citations:
            sections = [section for section, _ in citation.notes]
            assert sections == sorted(sections, reverse=True), (
                f"{citation}'s notes are in section order {sections}; they must run "
                f"newest-first (highest section number at the top)"
            )

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
