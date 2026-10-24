"""In-process rate limiting for the HTTP server.

Two limits, both per client address, both counted where authentication happens:

- **Wrong API tokens** allow `auth_failures_per_minute`; past that the client gets 429 on every
  token-protected route until the bucket refills, which stops token guessing. A missing token is not
  a guess and doesn't count.
- **Webhook requests with an invalid signature** allow `webhook_per_minute`. Genuinely signed
  provider callbacks are never limited, so the owner's messages aren't dropped and nothing unsigned
  reaches the parser.

Buckets live in memory and are capped at `max_clients` (least recently used evicted), so many
distinct addresses can't exhaust memory. One server process is the deployment model (ADR 0003),
so in-process state is enough.
"""

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBucket:
    """`capacity` tokens per key, refilled continuously at `capacity` per `period` seconds."""

    def __init__(self, capacity: int, period: float = 60.0, max_keys: int = 10_000,
                 clock: Callable[[], float] = time.monotonic):
        self.capacity = capacity
        self.rate = capacity / period
        self.max_keys = max_keys
        self.clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def _refilled(self, key: str) -> _Bucket:
        now = self.clock()
        bucket = self._buckets.pop(key, None) or _Bucket(tokens=float(self.capacity), updated=now)
        bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated) * self.rate)
        bucket.updated = now
        self._buckets[key] = bucket  # most recently used last
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)
        return bucket

    def take(self, key: str) -> bool:
        """Spend one token; False when the bucket is empty."""
        bucket = self._refilled(key)
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True

    def _peek(self, key: str) -> float:
        """Tokens available now, without creating or touching the bucket (so lookups from many
        addresses can't evict the buckets of clients that are being limited)."""
        bucket = self._buckets.get(key)
        if bucket is None:
            return float(self.capacity)
        return min(self.capacity, bucket.tokens + (self.clock() - bucket.updated) * self.rate)

    def is_empty(self, key: str) -> bool:
        return self._peek(key) < 1

    def retry_after(self, key: str) -> int:
        """Whole seconds until one token is available."""
        missing = 1 - self._peek(key)
        return max(1, math.ceil(missing / self.rate)) if missing > 0 else 0


@dataclass
class RateLimits:
    """Settings; 0 turns a limit off."""

    webhook_per_minute: int = 60
    auth_failures_per_minute: int = 10
    # Behind a reverse proxy every request comes from the proxy's address. When true, the client is
    # taken from the last X-Forwarded-For entry, the one your proxy appended (earlier ones are
    # whatever the client sent, so they're never trusted).
    trust_proxy_headers: bool = False

    def client(self, peer: str | None, forwarded_for: list[str]) -> str:
        """`forwarded_for` holds every X-Forwarded-For header line: a proxy may append its own line
        rather than extend the client's, so the last entry across all lines is the trusted one."""
        if self.trust_proxy_headers and forwarded_for:
            entries = [e.strip() for line in forwarded_for for e in line.split(",") if e.strip()]
            if entries:
                return entries[-1]
        return peer or "unknown"
