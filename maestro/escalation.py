"""Escalation: if a delivered reminder isn't acknowledged, try louder channels.

    MAESTRO_ESCALATION=10m=twilio_sms, 30m=telegram+twilio_sms

means: 10 minutes after the due time, if nobody has said done or snooze (#19), also send an SMS; at 30
minutes, Telegram and another SMS. Each stage is delivered once per occurrence through the normal
delivery tracking (retries, per-channel state), under the scheduler lease. Stage deliveries are
recorded as `escalation<N>:<channel>`, so they don't collide with the first notice on the same channel.

Only occurrences within ACK_WINDOW escalate: turning escalation on never pages you about old reminders.
"""

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from maestro.config import Config
from maestro.models import Item
from maestro.notifier import CHANNELS, ConfigError, Notifier
from maestro.observability import METRICS
from maestro.parser import parse_duration
from maestro.store import ACK_WINDOW, Store

log = logging.getLogger(__name__)

PREFIX = "Still waiting: "

# The scheduler's delivery step: (store, item, item_id, occurrence, channels, now) -> True once no channel
# is left to retry. Passed in to avoid a circular import.
DeliverFn = Callable[[Store, Item, int, datetime, dict[str, Notifier], datetime], bool]


@dataclass
class Stage:
    number: int  # 1, 2, ...
    delay: timedelta  # after the due time
    channels: dict[str, Notifier]  # keyed "escalation<N>:<channel>"


def parse_spec(spec: str) -> list[tuple[timedelta, list[str]]]:
    """'10m=twilio_sms, 30m=telegram+twilio_sms' -> [(10 min, [twilio_sms]), (30 min, [...])]."""
    stages = []
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        delay_text, sep, names = part.partition("=")
        channels = [n.strip() for n in names.split("+") if n.strip()]
        if not sep or not channels:
            raise ConfigError(f"MAESTRO_ESCALATION stage {part!r} should look like 10m=twilio_sms")
        try:
            delay = parse_duration(delay_text)
        except ValueError as e:
            raise ConfigError(f"MAESTRO_ESCALATION stage {part!r}: {e}") from e
        stages.append((delay, channels))
    delays = [d for d, _ in stages]
    if delays != sorted(delays) or len(set(delays)) != len(delays):
        raise ConfigError("MAESTRO_ESCALATION stages must have increasing delays")
    if delays and delays[0] < timedelta(minutes=1):
        raise ConfigError("MAESTRO_ESCALATION delays must be at least 1 minute")
    if delays and delays[-1] >= ACK_WINDOW:
        raise ConfigError(f"MAESTRO_ESCALATION delays must be under {ACK_WINDOW} (acknowledgement window)")
    return stages


def build_stages(config: Config) -> list[Stage]:
    stages = []
    for number, (delay, names) in enumerate(parse_spec(config.escalation), start=1):
        channels: dict[str, Notifier] = {}
        for name in names:
            if name not in CHANNELS:
                available = ", ".join(CHANNELS)
                raise ConfigError(f"MAESTRO_ESCALATION: unknown channel {name!r}. Available: {available}")
            channels[f"escalation{number}:{name}"] = CHANNELS[name](config)
        stages.append(Stage(number, delay, channels))
    return stages


def escalate(store: Store, stages: list[Stage], now: datetime, deliver: DeliverFn,
             max_attempts: int) -> int:
    """Send every stage that's due for unacknowledged occurrences. Returns how many stages were
    attempted this call (stages already delivered or given up on are skipped)."""
    attempted = 0
    for item, occurrence in store.unacknowledged():
        if item.id is None or now - occurrence >= ACK_WINDOW:
            continue  # too old to be what anyone is waiting on
        notice = dataclasses.replace(item, due_at=occurrence, text=f"{PREFIX}{item.text}")
        state = store.deliveries(item.id, occurrence)
        for stage in stages:
            if now - occurrence < stage.delay:
                break  # later stages aren't due either
            pending = [key for key in stage.channels
                       if not (d := state.get(key)) or not (d.delivered_at or d.attempts >= max_attempts)]
            if not pending:
                continue
            try:
                first_try = all(key not in state for key in pending)
                deliver(store, notice, item.id, occurrence, stage.channels, now)
                attempted += 1
                if first_try:
                    METRICS.inc("maestro_escalations_total", stage=str(stage.number))
                    log.info("Escalated reminder #%s to stage %d (%s)", item.id, stage.number,
                             ", ".join(k.split(":", 1)[1] for k in stage.channels))
            except Exception:
                METRICS.inc("maestro_scheduler_errors_total")
                log.exception("Escalation stage %d failed for reminder #%s", stage.number, item.id)
    return attempted
