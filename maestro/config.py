"""Settings for self-hosted deployments, read from environment variables (and an optional .env file).

Nothing here is required for local CLI use; channels that need keys stay off until configured.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB = Path.home() / ".maestro" / "maestro.db"


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, # comments. Real environment variables win."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _count(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name) or str(default)
    if not value.isdigit():
        raise ValueError(f"{name} must be a whole number (0 turns it off), not {value!r}")
    return int(value)


def _flag(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass
class Config:
    db_path: str = str(DEFAULT_DB)
    channels: list[str] = field(default_factory=lambda: ["console"])
    api_token: str | None = None
    timezone: str | None = None  # IANA name; None means the system timezone
    public_url: str | None = None  # how the outside world reaches this server, e.g. https://maestro.example.com
    rate_limit_webhook: int = 60  # requests per minute per client on public endpoints; 0 = off
    rate_limit_auth_failures: int = 10  # failed auths per minute per client before 429; 0 = off
    trust_proxy_headers: bool = False
    log_format: str = "text"  # or "json"
    log_level: str = "INFO"

    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from: str | None = None
    sms_to: str | None = None

    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str | None = None

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            db_path=env.get("MAESTRO_DB", str(DEFAULT_DB)),
            channels=_csv(env.get("MAESTRO_CHANNELS", "console")),
            api_token=env.get("MAESTRO_API_TOKEN") or None,
            timezone=env.get("MAESTRO_TIMEZONE") or None,
            public_url=(env.get("MAESTRO_PUBLIC_URL") or "").rstrip("/") or None,
            rate_limit_webhook=_count(env, "MAESTRO_RATE_LIMIT_WEBHOOK", 60),
            rate_limit_auth_failures=_count(env, "MAESTRO_RATE_LIMIT_AUTH_FAILURES", 10),
            trust_proxy_headers=_flag(env, "MAESTRO_TRUST_PROXY_HEADERS"),
            log_format=(env.get("MAESTRO_LOG_FORMAT") or "text").lower(),
            log_level=(env.get("MAESTRO_LOG_LEVEL") or "INFO").upper(),
            twilio_account_sid=env.get("TWILIO_ACCOUNT_SID") or None,
            twilio_auth_token=env.get("TWILIO_AUTH_TOKEN") or None,
            twilio_from=env.get("TWILIO_FROM_NUMBER") or None,
            sms_to=env.get("MAESTRO_SMS_TO") or None,
            ntfy_url=env.get("NTFY_URL", "https://ntfy.sh").rstrip("/"),
            ntfy_topic=env.get("NTFY_TOPIC") or None,
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=env.get("TELEGRAM_CHAT_ID") or None,
        )
