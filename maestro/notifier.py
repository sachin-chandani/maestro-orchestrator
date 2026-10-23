"""Delivery channels for reminders that come due."""

import platform
import subprocess
from typing import Protocol

from maestro.models import Item


class Notifier(Protocol):
    def send(self, item: Item) -> None: ...


class ConsoleNotifier:
    def send(self, item: Item) -> None:
        print(f"⏰ Reminder #{item.id}: {item.text}", flush=True)


class MacNotifier:
    """Native macOS notification via osascript. Text is passed as an argument, never interpolated."""

    SCRIPT = 'on run argv\ndisplay notification (item 1 of argv) with title "Maestro"\nend run'

    def send(self, item: Item) -> None:
        subprocess.run(["osascript", "-e", self.SCRIPT, item.text], check=False)


def default_notifier() -> Notifier:
    return MacNotifier() if platform.system() == "Darwin" else ConsoleNotifier()


class MultiNotifier:
    def __init__(self, *notifiers: Notifier):
        self.notifiers = notifiers

    def send(self, item: Item) -> None:
        for notifier in self.notifiers:
            notifier.send(item)
