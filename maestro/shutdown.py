"""Stopping without cutting a reminder in half (#60, ADR 0012).

`docker stop` and `systemctl stop` send SIGTERM. The long-running loops (`maestro run`, the
scheduler inside `maestro serve`, the Telegram and email workers) share one `Shutdown` and check it
between units of work. Work already in flight — a reminder being sent and its bookkeeping, a chat
command being handled — is finished first; nothing new is started; then the scheduler lease is
released so a standby takes over at once.

The loops mark the stretches that must not be interrupted with `busy()`. A signal that arrives while
nothing is busy may interrupt the wait straight away (the handler raises); one that arrives during
busy work only sets the flag, and the loop stops at the next check.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType

log = logging.getLogger(__name__)

Handler = Callable[[int, FrameType | None], None] | int | None


class Shutdown:
    """A cooperative stop request shared by the loops of one process."""

    def __init__(self) -> None:
        self._requested = False
        self._busy = 0

    @property
    def requested(self) -> bool:
        """Whether a stop was asked for. A property, so callers re-read it after work that may have set it."""
        return self._requested

    def request(self) -> bool:
        """Ask every loop to stop. Returns True when nothing is in flight, so the caller may interrupt
        the current wait instead of letting it run out."""
        first = not self._requested
        self._requested = True
        if self._busy:
            if first:
                log.info("Shutdown requested; finishing the work in flight first")
            return False
        if first:
            log.info("Shutdown requested")
        return True

    @contextmanager
    def busy(self) -> Iterator[None]:
        """Mark work that must finish once started: a send and the record of it, a command and its reply."""
        self._busy += 1
        try:
            yield
        finally:
            self._busy -= 1

    def install(self, chain: bool = True, signum: int = signal.SIGTERM) -> Handler:
        """Handle `signum` by requesting a stop. Returns the handler that was installed before.

        With `chain`, the previous handler is called too (uvicorn's, which stops the HTTP server), and
        the wait is never interrupted by raising: the event loop notices the request on its own.
        Without it (`maestro run`), an idle wait is cut short by raising SystemExit, as before.

        Signal handlers can only be installed from the main thread; elsewhere (the API under a test
        client) nothing is installed and None is returned.
        """
        if threading.current_thread() is not threading.main_thread():
            return None
        previous = signal.getsignal(signum)

        def handle(received: int, frame: FrameType | None) -> None:
            idle = self.request()
            if chain and callable(previous):
                previous(received, frame)
            elif not chain and idle:
                raise SystemExit(0)

        signal.signal(signum, handle)
        return previous
