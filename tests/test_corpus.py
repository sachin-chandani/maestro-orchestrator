"""#55: parser accuracy against a corpus of realistic captures.

Run `pytest tests/test_corpus.py -s` to print every miss. The target is in ENGINEERING.md; parser
changes must not lower the score. Phrases the parser deliberately doesn't handle yet go in
KNOWN_GAPS (with a reason) rather than being deleted, so the score stays honest.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from maestro.parser import parse

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 27, 8, 0, tzinfo=TZ)  # a Tuesday morning
CORPUS = Path(__file__).parent / "corpus" / "phrases.txt"
TARGET = 0.95

# input -> reason. Counted as misses in the score; listed so they're decided, not forgotten.
KNOWN_GAPS: dict[str, str] = {
    "work from 9am to 5pm": "time ranges aren't modelled; the reminder would be at the start",
    "pick up the kids before 3pm": "'before X' should fire ahead of X; needs a lead-time rule",
    "call mom morning": "a lone part-of-day word after a task is ambiguous with names like 'movie night';"
                        " 'in the morning' or 'tomorrow morning' work",
}


@dataclass
class Case:
    source: str
    text: str
    due: datetime | None
    rule: str | None
    line: int


def load() -> list[Case]:
    cases = []
    for number, raw in enumerate(CORPUS.read_text().splitlines(), start=1):
        if not raw.strip() or raw.startswith("#"):
            continue
        source, text, due, rule = (part.strip() for part in raw.split("|"))
        due_at = None if due == "-" else datetime.fromisoformat(due).replace(tzinfo=TZ)
        cases.append(Case(source, text, due_at, None if rule == "-" else rule, number))
    return cases


def check(case: Case) -> str | None:
    """None if the parse matches, else a description of the difference."""
    item = parse(case.source, now=NOW, tz=TZ)
    got_due = item.due_at.replace(tzinfo=None).isoformat(timespec="minutes") if item.due_at else "-"
    want_due = case.due.replace(tzinfo=None).isoformat(timespec="minutes") if case.due else "-"
    problems = []
    if item.text != case.text:
        problems.append(f"text {item.text!r} != {case.text!r}")
    if got_due != want_due:
        problems.append(f"due {got_due} != {want_due}")
    if (item.recurrence or None) != case.rule:
        problems.append(f"rule {item.recurrence} != {case.rule}")
    return "; ".join(problems) or None


def test_corpus_is_well_formed():
    cases = load()
    assert len(cases) >= 200
    assert len({c.source for c in cases}) == len(cases), "duplicate inputs"
    assert set(KNOWN_GAPS) <= {c.source for c in cases}, "a known gap is not in the corpus"


def test_parser_accuracy():
    cases = load()
    misses = [(c, problem) for c in cases if (problem := check(c))]
    accuracy = 1 - len(misses) / len(cases)
    report = "\n".join(f"  line {c.line}: {c.source!r}: {problem}"
                       + (f"  [known gap: {KNOWN_GAPS[c.source]}]" if c.source in KNOWN_GAPS else "")
                       for c, problem in misses)
    print(f"\nParser accuracy: {accuracy:.1%} ({len(cases) - len(misses)}/{len(cases)})\n{report}")
    unexpected = [c.source for c, _ in misses if c.source not in KNOWN_GAPS]
    assert accuracy >= TARGET, f"accuracy {accuracy:.1%} below {TARGET:.0%}:\n{report}"
    assert not unexpected, f"misses not listed in KNOWN_GAPS:\n{report}"
