"""Settings for self-hosted deployments, read from environment variables (and an optional .env file).

Nothing here is required for local CLI use; channels that need keys stay off until configured.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from maestro.email_in import EmailSettings

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
    value = (env.get(name) or str(default)).strip()
    if not (value.isascii() and value.isdigit()):
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
    escalation: str = ""  # e.g. "10m=twilio_sms, 30m=telegram+twilio_sms"; see maestro/escalation.py
    quiet_hours: str = ""  # e.g. "22:00-07:00", local time; see maestro/quiet.py
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

    webhook_url: str | None = None
    webhook_secret: str | None = None  # signs requests (HMAC-SHA256) so receivers can verify them
    webhook_format: str = "json"  # json, slack or discord

    smtp_host: str | None = None
    smtp_port: int | None = None  # None: 465 for ssl, 587 otherwise
    smtp_security: str = "starttls"  # starttls, ssl or none
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    email_to: str | None = None  # where reminder emails go

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    imap_host: str | None = None
    imap_port: int = 993
    imap_user: str | None = None
    imap_password: str | None = None
    imap_folder: str = "INBOX"
    email_from: list[str] = field(default_factory=list)  # senders allowed to capture by email
    email_require_dmarc: bool = True
    email_authserv_id: str | None = None  # your provider's id in Authentication-Results headers
    email_poll_seconds: int = 60

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            db_path=env.get("MAESTRO_DB", str(DEFAULT_DB)),
            channels=_csv(env.get("MAESTRO_CHANNELS", "console")),
            escalation=env.get("MAESTRO_ESCALATION", "").strip(),
            quiet_hours=env.get("MAESTRO_QUIET_HOURS", "").strip(),
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
            webhook_url=(env.get("MAESTRO_WEBHOOK_URL") or "").strip() or None,
            webhook_secret=env.get("MAESTRO_WEBHOOK_SECRET") or None,
            webhook_format=(env.get("MAESTRO_WEBHOOK_FORMAT") or "json").strip().lower(),
            smtp_host=env.get("SMTP_HOST") or None,
            smtp_port=_count(env, "SMTP_PORT", 0) or None,
            smtp_security=(env.get("SMTP_SECURITY") or "starttls").strip().lower(),
            smtp_user=env.get("SMTP_USER") or None,
            smtp_password=env.get("SMTP_PASSWORD") or None,
            smtp_from=env.get("SMTP_FROM") or None,
            email_to=env.get("MAESTRO_EMAIL_TO") or None,
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=env.get("TELEGRAM_CHAT_ID") or None,
            imap_host=env.get("IMAP_HOST") or None,
            imap_port=_count(env, "IMAP_PORT", 993),
            imap_user=env.get("IMAP_USER") or None,
            imap_password=env.get("IMAP_PASSWORD") or None,
            imap_folder=env.get("IMAP_FOLDER") or "INBOX",
            email_from=[a.lower() for a in _csv(env.get("MAESTRO_EMAIL_FROM", ""))],
            email_require_dmarc=(env.get("MAESTRO_EMAIL_REQUIRE_DMARC") or "true").strip().lower()
            not in ("0", "false", "no", "off"),
            email_authserv_id=(env.get("MAESTRO_EMAIL_AUTHSERV_ID") or "").strip() or None,
            email_poll_seconds=_count(env, "MAESTRO_EMAIL_POLL_SECONDS", 60),
        )


def email_settings(config: Config) -> "EmailSettings | None":
    """Email-in settings when IMAP is configured, else None. Raises ValueError if half-configured."""
    from maestro.email_in import EmailSettings

    if not config.imap_host:
        return None
    user, password, senders = config.imap_user, config.imap_password, config.email_from
    if not (user and password and senders):
        missing = [name for name, value in (("IMAP_USER", user), ("IMAP_PASSWORD", password),
                                            ("MAESTRO_EMAIL_FROM", senders)) if not value]
        raise ValueError(f"email-in needs {', '.join(missing)} when IMAP_HOST is set")
    if config.email_require_dmarc and not config.email_authserv_id:
        raise ValueError("email-in needs MAESTRO_EMAIL_AUTHSERV_ID: the first word of the "
                         "Authentication-Results header your provider adds (e.g. mx.google.com), "
                         "so forged headers can't pass")
    if config.email_poll_seconds < 10:
        raise ValueError("MAESTRO_EMAIL_POLL_SECONDS must be at least 10 "
                         "(unset IMAP_HOST to turn email-in off)")
    return EmailSettings(host=config.imap_host, port=config.imap_port, user=user, password=password,
                         allowed_senders=frozenset(senders), folder=config.imap_folder,
                         require_dmarc=config.email_require_dmarc, authserv_id=config.email_authserv_id,
                         poll_seconds=config.email_poll_seconds)
