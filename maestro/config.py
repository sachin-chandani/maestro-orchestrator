"""Settings for self-hosted deployments, read from environment variables (and an optional .env file).

Nothing here is required for local CLI use; channels that need keys stay off until configured.
"""

import os
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from maestro import captures
    from maestro.email_in import EmailSettings
    from maestro.model import ModelSettings

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


_PROVIDER_KEY_VARS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def _model_key(env: Mapping[str, str]) -> str | None:
    """The model API key: MAESTRO_MODEL_API_KEY, else the variable that provider usually uses."""
    if key := (env.get("MAESTRO_MODEL_API_KEY") or "").strip():
        return key
    provider = (env.get("MAESTRO_MODEL_PROVIDER") or "").strip().lower()
    return (env.get(_PROVIDER_KEY_VARS.get(provider, ""), "") or "").strip() or None


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass
class Config:
    # A SQLite path (the default and what MAESTRO_DB has always meant) or a database URL from
    # MAESTRO_DB_URL, e.g. postgresql://…; see maestro.storage.factory.
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

    # The optional model interpreter (#115, ADR 0015). "rules" is the default and means no model is
    # ever contacted; a key on its own never turns one on. See maestro.interpret.
    interpreter: str = "rules"  # rules, auto (model only when the rules are unsure, #116) or model
    # Confirm before acting on an unsure interpretation (#117). "never" is the default: captures
    # are applied at once, exactly as before. "unsure" asks first in the channel used.
    confirm: str = "never"  # never or unsure
    confirm_hours: float = 24.0  # how long an unanswered confirmation waits before expiring
    model_provider: str = ""  # anthropic, openai or openai-compatible
    model: str = ""  # the model name, e.g. claude-sonnet-5-5; no default on purpose
    # MAESTRO_MODEL_API_KEY, else the provider's usual variable; never in a repr (#123)
    model_api_key: str | None = field(default=None, repr=False)
    model_base_url: str | None = None  # required for openai-compatible (Ollama, LM Studio, vLLM)
    model_timeout_seconds: float = 8.0

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
            # MAESTRO_DB_URL wins: someone who sets it means to move off the default SQLite file,
            # and the image or an old .env may still carry MAESTRO_DB. `maestro doctor` says so.
            db_path=env.get("MAESTRO_DB_URL") or env.get("MAESTRO_DB", str(DEFAULT_DB)),
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
            interpreter=(env.get("MAESTRO_INTERPRETER") or "rules").strip().lower(),
            confirm=(env.get("MAESTRO_CONFIRM") or "never").strip().lower(),
            confirm_hours=float(_count(env, "MAESTRO_CONFIRM_HOURS", 24)),
            model_provider=(env.get("MAESTRO_MODEL_PROVIDER") or "").strip().lower(),
            model=(env.get("MAESTRO_MODEL") or "").strip(),
            # MAESTRO_MODEL_API_KEY wins; otherwise the provider's usual variable, so a key already
            # in the environment works without being copied. Never logged or printed anywhere.
            model_api_key=_model_key(env),
            model_base_url=(env.get("MAESTRO_MODEL_BASE_URL") or "").strip().rstrip("/") or None,
            model_timeout_seconds=float(_count(env, "MAESTRO_MODEL_TIMEOUT_SECONDS", 8)),
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


def model_settings(config: Config) -> "ModelSettings | None":
    """The model settings when `MAESTRO_INTERPRETER` is model or auto, else None (the default:
    rules only).

    Raises ValueError, as one sentence an operator can act on, when the model is asked for but
    isn't fully configured. A key on its own never turns the model on.
    """
    from maestro.model import PROVIDERS, ModelSettings

    if config.interpreter == "rules":
        return None
    if config.interpreter not in ("model", "auto"):
        raise ValueError(f"MAESTRO_INTERPRETER must be rules, auto or model, not {config.interpreter!r}")
    if not config.model_provider:
        raise ValueError(f"MAESTRO_INTERPRETER={config.interpreter} needs MAESTRO_MODEL_PROVIDER: "
                         f"{', '.join(PROVIDERS)}. Maestro won't pick who sees your captures for you")
    if config.model_provider not in PROVIDERS:
        raise ValueError(f"Unknown MAESTRO_MODEL_PROVIDER {config.model_provider!r}; "
                         f"expected one of {', '.join(PROVIDERS)}")
    if not config.model:
        raise ValueError(f"MAESTRO_INTERPRETER={config.interpreter} needs MAESTRO_MODEL: there is no "
                         "default model "
                         "name (e.g. MAESTRO_MODEL=claude-sonnet-5-5)")
    if not 1 <= config.model_timeout_seconds <= 120:
        raise ValueError("MAESTRO_MODEL_TIMEOUT_SECONDS must be between 1 and 120 "
                         "(a capture waits for it, so keep it short)")
    settings = ModelSettings(provider=config.model_provider, model=config.model,
                             api_key=config.model_api_key, base_url=config.model_base_url,
                             timeout=config.model_timeout_seconds)
    # The capture text, and usually a key, go to this URL. Off your own network that means TLS,
    # the same rule the webhook channel follows.
    scheme = urllib.parse.urlparse(settings.base_url or "https://").scheme
    if scheme != "https" and not settings.on_own_network:
        raise ValueError(f"MAESTRO_MODEL_BASE_URL must use https outside your own network "
                         f"(captures and your key would be sent in the clear to {settings.host})")
    return settings


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


def confirmation(config: Config) -> "captures.Confirmation | None":
    """The confirmation settings when `MAESTRO_CONFIRM=unsure`, else None (the default: apply at
    once, as Maestro always has). Raises ValueError, as one sentence, on a bad setting (#117)."""
    from maestro.captures import Confirmation

    if config.confirm == "never":
        return None
    if config.confirm != "unsure":
        raise ValueError(f"MAESTRO_CONFIRM must be never or unsure, not {config.confirm!r}")
    if not 0 < config.confirm_hours <= 24 * 7:
        raise ValueError("MAESTRO_CONFIRM_HOURS must be between 0 and 168 (a week): an unanswered "
                         "question should expire, not queue up forever")
    from datetime import timedelta

    return Confirmation(ttl=timedelta(hours=config.confirm_hours))
