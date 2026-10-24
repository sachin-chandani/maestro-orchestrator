"""Logging setup and Prometheus metrics.

Metrics are kept in-process and rendered in the Prometheus text exposition format by hand,
so there's no client-library dependency. Counters live in the process that does the work
(`maestro serve` or `maestro run`); item counts are read from the store at scrape time.
"""

import json
import logging
import threading
from collections import defaultdict
from datetime import UTC, datetime

LOG_FORMATS = ("text", "json")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log shippers (Loki, Vector, CloudWatch...)."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(fmt: str = "text", level: str = "INFO") -> None:
    if fmt not in LOG_FORMATS:
        raise ValueError(f"MAESTRO_LOG_FORMAT must be one of {', '.join(LOG_FORMATS)}, not {fmt!r}")
    handler = logging.StreamHandler()
    text = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(JsonFormatter() if fmt == "json" else text)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


Labels = tuple[tuple[str, str], ...]


class Metrics:
    """Thread-safe counters and gauges with labels."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values: dict[str, dict[Labels, float]] = defaultdict(dict)
        self._meta: dict[str, tuple[str, str]] = {}  # name -> (type, help)

    def describe(self, name: str, kind: str, help_text: str) -> None:
        self._meta[name] = (kind, help_text)

    def inc(self, name: str, amount: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            series = self._values[name]
            series[key] = series.get(key, 0.0) + amount

    def set(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._values[name][tuple(sorted(labels.items()))] = value

    def get(self, name: str, **labels: str) -> float:
        with self._lock:
            return self._values[name].get(tuple(sorted(labels.items())), 0.0)

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def render(self, extra: dict[str, dict[Labels, float]] | None = None) -> str:
        with self._lock:
            values = {name: dict(series) for name, series in self._values.items()}
        for name, series in (extra or {}).items():
            values.setdefault(name, {}).update(series)

        lines = []
        for name in sorted(set(self._meta) | set(values)):
            kind, help_text = self._meta.get(name, ("untyped", ""))
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
            for labels, value in sorted(values.get(name, {}).items()):
                label_text = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
                number = _number(value)
                lines.append(f"{name}{{{label_text}}} {number}" if label_text else f"{name} {number}")
        return "\n".join(lines) + "\n"


def _number(value: float) -> str:
    # Full precision: %g would round a Unix timestamp to 1.79278e+09.
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


METRICS = Metrics()
METRICS.describe("maestro_captures_total", "counter", "Items captured, by source.")
METRICS.describe("maestro_deliveries_total", "counter",
                 "Delivery attempts, by channel and result (ok, error, gave_up).")
METRICS.describe("maestro_reminders_fired_total", "counter", "Reminder occurrences fully delivered.")
METRICS.describe("maestro_scheduler_ticks_total", "counter", "Scheduler ticks run.")
METRICS.describe("maestro_scheduler_errors_total", "counter",
                 "Items skipped or ticks failed because of an unexpected error; alert if it rises.")
METRICS.describe("maestro_scheduler_last_tick_timestamp_seconds", "gauge",
                 "Unix time of the last scheduler tick.")
METRICS.describe("maestro_scheduler_lag_seconds", "gauge",
                 "How late the most overdue reminder handled in the last tick was.")
METRICS.describe("maestro_items", "gauge", "Items in the store, by kind and status.")
