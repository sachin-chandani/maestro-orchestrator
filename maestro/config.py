"""Settings for self-hosted deployments, read from environment variables (and an optional .env file).

Nothing here is required for local CLI use; channels that need keys stay off until configured.
"""

import os
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


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass
class Config:
    db_path: str = str(DEFAULT_DB)
    channels: list[str] = field(default_factory=lambda: ["console"])
    api_token: str | None = None

    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from: str | None = None
    sms_to: str | None = None

    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            db_path=env.get("MAESTRO_DB", str(DEFAULT_DB)),
            channels=_csv(env.get("MAESTRO_CHANNELS", "console")),
            api_token=env.get("MAESTRO_API_TOKEN") or None,
            twilio_account_sid=env.get("TWILIO_ACCOUNT_SID") or None,
            twilio_auth_token=env.get("TWILIO_AUTH_TOKEN") or None,
            twilio_from=env.get("TWILIO_FROM_NUMBER") or None,
            sms_to=env.get("MAESTRO_SMS_TO") or None,
            ntfy_url=env.get("NTFY_URL", "https://ntfy.sh").rstrip("/"),
            ntfy_topic=env.get("NTFY_TOPIC") or None,
        )
