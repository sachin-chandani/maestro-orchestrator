"""Devices signed in with a token of their own (#108, ADR 0018).

The master token (`MAESTRO_API_TOKEN`) is what the operator has; a phone never stores it. The app
sends it once to `POST /devices` and gets back a device token that works everywhere the master
token does except on `/devices` itself, so a lost phone is one revocation, not a rotation of every
client. Only the token's SHA-256 is stored: a copy of the database can't be used to sign in.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from maestro.models import Device
from maestro.storage import Store

#: Device tokens start with this, so one is recognisable in a log or a config file and is never
#: mistaken for the master token.
PREFIX = "mdt_"

#: last_seen_at is written at most this often per device, so authenticating is usually a read.
SEEN_EVERY = timedelta(hours=1)

MAX_NAME = 80


def new_token() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def register(store: Store, name: str, now: datetime) -> tuple[Device, str]:
    """Add a device and return it with the one and only copy of its token."""
    token = new_token()
    device = store.add_device(Device(name=name.strip()[:MAX_NAME] or "Device", token_hash=hash_token(token),
                                     created_at=now))
    return device, token


def authenticate(store: Store, token: str, now: datetime) -> Device | None:
    """The live device this token belongs to, or None. Records when it was last seen, to the hour."""
    if not token.startswith(PREFIX):
        return None  # not a device token: don't touch the database for a guess at the master token
    device = store.find_device(hash_token(token))
    if device is None:
        return None
    if device.id is not None and (device.last_seen_at is None or now - device.last_seen_at >= SEEN_EVERY):
        store.touch_device(device.id, now)
        device.last_seen_at = now
    return device
