"""How sure the rules are that they understood a capture (#116).

`MAESTRO_INTERPRETER=auto` asks a model only when the rules are unsure. "Sure" here means the
rules' *reading* holds together, not that they used up the words: a parse that swallowed
"before 3pm" as 3pm consumed every token and still got it wrong. So each check below looks for
evidence that the reading is incomplete or wrong, and every one is structural — there is no
word count, length or score to tune:

- **time_left**: the saved text, parsed again, still contains a time. The rules read one time
  and left another, so they didn't explain the whole message ("work from 9am to 5pm").
- **dangling_time_word**: the saved text ends in a word that only means something next to a time
  ("pick up the kids before"): the time was taken, and with it the sense of the phrase.
- **unread_time**: words that name a time the rules have no rule for ("sometime next week",
  "after work", "in 3 months", "dec 3rd"), left in the text.
- **several_requests**: a second request joined on with "and", "then" or "also" ("book flights
  and remind me to pack"). The rules save one item per message.
- **several_sentences**: a line break or a sentence boundary inside the message: dictation or a
  pasted paragraph. The rules read one phrase.
- **forwarded**: a forwarded message ("Fwd: …"), written for someone else, not as a capture.
- **filler**: dictation filler ("um", "uh") that would otherwise end up in the saved text.

A capture with no doubts is one the rules read on their own. Short, plain captures with no time
("milk", "renew the passport") are deliberately sure: there is nothing for a model to add, and
sending them away would cost time, money and privacy for nothing.
"""

import re
from dataclasses import dataclass

from maestro.models import Item
from maestro.parser import TODO_VERBS

DOUBTS = ("time_left", "dangling_time_word", "unread_time", "several_requests",
          "several_sentences", "forwarded", "filler")

# Words that only carry meaning next to a time. At the very end of the saved text, the time they
# referred to has been taken away ("before 3pm" -> "before"). "at", "on" and "by" are already
# trimmed by the parser, so they never reach here.
_DANGLING_RE = re.compile(r"\b(?:before|after|until|till|til|from|between|around|about|no later than"
                          r"|next)$", re.I)
# The same words mid-text, directly followed by a connector instead of their time: "meet john
# before in the lobby" ("before 10am" lost its 10am). "around" and "about" are left out here —
# "show him around in the office" and "ask about the invoice" are whole verbs.
_DANGLING_MID_RE = re.compile(r"\b(?:before|until|till|til|from|between|no later than)\s+"
                              r"(?:and|then|in|on|at|to|with|for)\b", re.I)
# "from … to", "between … and" with the times removed: "Work from to".
_EMPTY_RANGE_RE = re.compile(r"\b(?:from\s+(?:to|till|until)|between\s+and)\b", re.I)

_MONTHS = ("jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
           "sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_ORDINAL = r"\d{1,2}(?:st|nd|rd|th)?"
_UNREAD_TIME_RE = re.compile(
    r"\b(?:"
    r"some\s?time|soon|eventually|asap|in\s+a\s+(?:bit|while)|fortnight"
    r"|(?:next|this|last|coming)\s+(?:week|month|year|quarter)"
    r"|(?:end|start|beginning|middle)\s+of\s+(?:the\s+)?(?:month|year|quarter)"
    r"|(?:after|before)\s+(?:work|school|lunch|dinner|breakfast|class|the\s+gym)"
    r"|in\s+(?:\d+|an?|one|a\s+few|a\s+couple(?:\s+of)?)\s+(?:months?|years?)"
    # Texting shorthand the rules don't read. Only forms that aren't English words: "wed", "sat"
    # and "sun" are left out, so "sat on the sofa" and "the sun lotion" stay sure.
    r"|tmrw?|2moro|2morrow|tonite|2nite|2day|b4|l8r|nxt"
    r"|mon|tue|tues|weds|thu|thur|thurs|fri"
    rf"|(?:{_MONTHS})\.?\s+{_ORDINAL}|{_ORDINAL}\s+(?:of\s+)?(?:{_MONTHS})"
    r")\b",
    re.I,
)
# A part of the day with no time read at all: "call mom morning" (see KNOWN_GAPS in the corpus).
_LONE_PART_OF_DAY_RE = re.compile(r"\b(?:morning|afternoon|evening)\b", re.I)

# Words people put between "and" and the second request: "and then also maybe pick up …".
_HEDGE = (r"(?:and|then|also|maybe|please|to|(?:i\s+)?(?:need|have|want)\s+to|(?:i\s+)?should"
          r"|(?:i\s+)?gotta|could\s+you|can\s+you)")
_REQUEST = rf"(?:remind\s+me|remember\s+to|don'?t\s+forget|{'|'.join(TODO_VERBS)})"
# "and", "then", "also" (in any combination), then a request. "salt and pepper" and "call mom and
# dad" stay one request: what follows the joiner isn't asking for anything.
_SEVERAL_REQUESTS_RE = re.compile(
    rf"(?:\band\b|\bthen\b|\balso\b|,)(?:\s+{_HEDGE})*\s+{_REQUEST}\b",
    re.I,
)
# A sentence ends inside the message, not at its end. Titles and "a.m." aren't sentence ends.
_ABBREVIATION_RE = re.compile(r"\b(?:dr|mr|mrs|ms|st|vs|etc|e\.g|i\.e|[ap]\.m|no)\.", re.I)
_SENTENCE_BREAK_RE = re.compile(r"[\r\n]|\w[.!?;]+\s+\S")
_FORWARDED_RE = re.compile(r"^\s*(?:fwd?|fw)\s*:", re.I)
_FILLER_RE = re.compile(r"\b(?:um+|uh+|uhm+|erm+|hmm+)\b", re.I)


@dataclass(frozen=True)
class Confidence:
    """Why the rules might have misread a capture. No doubts means they are sure."""

    doubts: tuple[str, ...] = ()

    @property
    def sure(self) -> bool:
        return not self.doubts


def assess(source: str, item: Item, again: Item | None) -> Confidence:
    """How sure the rules can be that `item` is what `source` meant. Pure and offline.

    `again` is the rules' reading of `item.text`, the text they saved — or None when that text
    trips the parser, which is a reason to doubt in itself. The parser is only ever called from
    `maestro.interpret` (ADR 0015), so the caller parses it and hands it over.
    """
    doubts: list[str] = []
    text = item.text

    if again is None or again.due_at is not None or again.recurrence is not None:
        doubts.append("time_left")
    if _DANGLING_RE.search(text) or _DANGLING_MID_RE.search(text) or _EMPTY_RANGE_RE.search(text):
        doubts.append("dangling_time_word")
    if _UNREAD_TIME_RE.search(text) or (item.due_at is None and _LONE_PART_OF_DAY_RE.search(text)):
        doubts.append("unread_time")
    if _SEVERAL_REQUESTS_RE.search(text):
        doubts.append("several_requests")
    body = _ABBREVIATION_RE.sub("", source.strip())
    if _FORWARDED_RE.match(body):
        doubts.append("forwarded")
    elif _SENTENCE_BREAK_RE.search(body):
        doubts.append("several_sentences")
    if _FILLER_RE.search(text):
        doubts.append("filler")
    return Confidence(tuple(doubts))
