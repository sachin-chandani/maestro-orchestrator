"""#130: multi-request phrases and their expected splits, as a corpus.

Unlike phrases.txt (a statistical accuracy corpus with a 95% floor), every row here must pass: a
wrong split either loses half of what someone said or invents requests they never made. Run
`pytest tests/test_split_corpus.py -s` to print any miss. Readings are produced exactly as a
capture is: `RulesInterpreter.read`, then `maestro.confidence.gap` on each, as `gaps.split_apply`
does.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from maestro.confidence import gap
from maestro.interpret import Context, RulesInterpreter
from maestro.models import Kind

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 27, 8, 0, tzinfo=TZ)  # a Tuesday morning, as in phrases.txt
CORPUS = Path(__file__).parent / "corpus" / "splits.txt"


@dataclass
class Reading:
    text: str
    due: datetime | None
    rule: str | None
    kind: Kind
    gap: str | None
    channels: list[str] | None


@dataclass
class Case:
    source: str
    line: int
    readings: list[Reading] = field(default_factory=list)


def load() -> list[Case]:
    cases: list[Case] = []
    source_lines: list[str] = []
    for number, raw in enumerate(CORPUS.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(">"):
            # Consecutive input lines are one multi-line message (a newline is a join).
            if cases and cases[-1].readings == [] and source_lines:
                source_lines.append(line)
                cases[-1].source = "\n".join(source_lines)
            else:
                source_lines = [line]
                cases.append(Case(source=line, line=number))
            continue
        source_lines = []
        text, due, rule, *extra = (part.strip() for part in line.lstrip("> ").split("|"))
        flags = [f for f in (extra[0] if extra else "").split("+") if f and f != "-"]
        due_at = None if due == "-" else datetime.fromisoformat(due).replace(tzinfo=TZ)
        gaps = [f.removeprefix("gap:") for f in flags if f.startswith("gap:")]
        channels = [f for f in flags if not f.startswith("gap:") and f != "task"] or None
        kind = Kind.REMINDER if due_at else (Kind.TASK if "task" in flags else Kind.NOTE)
        cases[-1].readings.append(Reading(text, due_at, None if rule in ("-", "") else rule, kind,
                                          gaps[0] if gaps else None, channels))
    return cases


def check(case: Case) -> list[str]:
    read, _ = RulesInterpreter().read(case.source, Context(tz=TZ, now=NOW))
    problems: list[str] = []
    if len(read) != len(case.readings):
        got = [getattr(a, "text", repr(a)) for a in read]
        return [f"{len(read)} readings {got} != {len(case.readings)} expected"]
    for n, (action, want) in enumerate(zip(read, case.readings, strict=True), start=1):
        item = action.to_item(NOW)
        if item.text != want.text:
            problems.append(f"[{n}] text {item.text!r} != {want.text!r}")
        if item.due_at != want.due:
            problems.append(f"[{n}] due {item.due_at} != {want.due}")
        if (item.recurrence or None) != want.rule:
            problems.append(f"[{n}] rule {item.recurrence} != {want.rule}")
        if item.kind != want.kind:
            problems.append(f"[{n}] kind {item.kind} != {want.kind}")
        if gap(item) != want.gap:
            problems.append(f"[{n}] gap {gap(item)} != {want.gap}")
        if (item.channels or None) != want.channels:
            problems.append(f"[{n}] channels {item.channels} != {want.channels}")
        if action.source_text != case.source:
            problems.append(f"[{n}] source_text {action.source_text!r} != the whole message")
    return problems


def test_every_split_row_reads_exactly_as_a_person_meant_it():
    cases = load()
    assert len(cases) >= 15  # the corpus is loaded, not accidentally empty
    misses = []
    for case in cases:
        for problem in check(case):
            misses.append(f"line {case.line}: {case.source.splitlines()[0]!r}: {problem}")
    assert not misses, "\n".join(misses)


def test_the_adr_0017_example_is_two_saved_and_one_needing_a_look():
    """The v0.5 exit criterion, pinned by name (#130, ADR 0017)."""
    read, confidence = RulesInterpreter().read(
        "book flights, remind me friday at nine to check in, and call the plumber sometime next week",
        Context(tz=TZ, now=NOW))
    items = [a.to_item(NOW) for a in read]
    assert [(i.text, gap(i)) for i in items] == [
        ("Book flights", None),
        ("Check in", None),
        ("Call the plumber sometime next week", "when"),
    ]
    assert items[1].due_at == datetime(2026, 10, 30, 9, 0, tzinfo=TZ)
    # Splitting explained the joins, so "several requests" is no longer a reason to ask a model.
    assert "several_requests" not in confidence.doubts
