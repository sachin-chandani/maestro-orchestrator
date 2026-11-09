"""Reading what someone meant: text in, actions out (ADR 0015).

One step, one place. `RulesInterpreter` is the offline default and is exactly today's rule-based
parser, so behaviour is unchanged. A model interpreter (#115) plugs in here as another `Interpreter`,
and whatever it proposes is validated as actions (`maestro.actions`) before anything is saved.
"""

from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Protocol

from maestro import actions
from maestro.actions import Action
from maestro.parser import parse


@dataclass(frozen=True)
class Context:
    """What an interpreter needs to know besides the text itself."""

    tz: tzinfo | None = None  # the user's timezone; None means the configured one
    now: datetime | None = None  # the moment the capture happened; None means now


class Interpreter(Protocol):
    """Turns one captured message into the actions it asks for."""

    def interpret(self, text: str, context: Context) -> list[Action]:
        ...


class RulesInterpreter:
    """Today's parser: one message, one thing to save, no network and no cost."""

    def interpret(self, text: str, context: Context) -> list[Action]:
        return [actions.from_item(parse(text, now=context.now, tz=context.tz))]


DEFAULT = RulesInterpreter()


def interpret(text: str, context: Context | None = None,
              interpreter: Interpreter | None = None) -> list[Action]:
    """The actions a captured message asks for. Uses the rules unless given another interpreter."""
    return (interpreter or DEFAULT).interpret(text, context or Context())
