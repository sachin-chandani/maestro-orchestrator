"""In-process rate limiting for the HTTP server.

Two limits, both per client address:

- **Public endpoints** (`/webhooks/*`, reachable without the API token) allow `webhook_per_minute`
  requests, so a flood can't run the parser or fill the database.
- **Failed authentication** (401/403 responses) allows `auth_failures_per_minute`; past that the
  client gets 429 on every route until the bucket refills, which stops token guessing.

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

    def has_tokens(self, key: str) -> bool:
        """Whether a token is available, without spending it."""
        return self._refilled(key).tokens >= 1

    def retry_after(self, key: str) -> int:
        """Whole seconds until one token is available."""
        missing = 1 - self._refilled(key).tokens
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

    def client(self, peer: str | None, forwarded_for: str | None) -> str:
        if self.trust_proxy_headers and forwarded_for:
            return forwarded_for.split(",")[-1].strip() or (peer or "unknown")
        return peer or "unknown"
