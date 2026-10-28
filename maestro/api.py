"""HTTP API for self-hosted deployments. Install with `pip install 'maestro-orchestrator[server]'`.

Every route except /health requires `Authorization: Bearer $MAESTRO_API_TOKEN` when a token is configured.
Handlers are async on purpose: they all run on the event-loop thread, so the single SQLite
connection is never used from two threads at once.
"""

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from importlib.resources import files
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from maestro import __version__, commands, recurrence
from maestro.models import Item
from maestro.notifier import Notifier
from maestro.observability import METRICS, Labels
from maestro.parser import parse, parse_duration
from maestro.ratelimit import RateLimits, TokenBucket
from maestro.scheduler import Lease, tick
from maestro.sms import WEBHOOK_PATH, SmsHandler, TwilioHandler
from maestro.store import Store
from maestro.tz import add_elapsed
from maestro.voice import (
    VOICE_CAPTURE_PATH,
    VOICE_PATH,
    VOICE_REMINDER_PATH,
    CallReminderHandler,
    VoiceHandler,
)

if TYPE_CHECKING:
    from maestro.email_in import EmailWorker
    from maestro.escalation import Stage
    from maestro.quiet import QuietHours
    from maestro.telegram import TelegramBot

log = logging.getLogger(__name__)

WEB_UI = files("maestro").joinpath("web/index.html").read_text(encoding="utf-8")


async def _scheduler_loop(store: Store, notifier: Notifier, interval: float,
                          escalation: "list[Stage] | None" = None, quiet: "QuietHours | None" = None) -> None:
    # Runs on the event loop with the request handlers, keeping SQLite access single-threaded.
    lease = Lease.for_interval(interval)
    try:
        while True:
            tick(store, notifier, lease, escalation, quiet)
            await asyncio.sleep(interval)
    finally:  # cancelled at shutdown: hand over to any standby scheduler right away
        lease.release(store)


async def _email_loop(worker: "EmailWorker") -> None:
    # IMAP runs in the worker's thread; capturing (which touches SQLite) happens here, on the event loop.
    while True:
        worker.process()
        await asyncio.sleep(1)


async def _bot_loop(bot: "TelegramBot") -> None:
    # The long poll runs in a worker thread; handling (which touches SQLite) stays on the event loop.
    while True:
        try:
            updates = await asyncio.to_thread(bot.fetch)
        except Exception:
            log.exception("Telegram polling failed; retrying in 10s")
            await asyncio.sleep(10)
            continue
        for update in updates:
            bot.handle(update)  # never raises: one bad update can't stop the bot


class CaptureIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000, examples=["call mom tomorrow at 5pm"])


class SnoozeIn(BaseModel):
    duration: str = Field(examples=["10m", "2h", "1d"])


class ItemOut(BaseModel):
    id: int
    text: str
    kind: str
    status: str
    due_at: datetime | None
    snoozed_until: datetime | None  # a recurring reminder's snoozed occurrence, sent before due_at
    created_at: datetime
    recurrence: str | None
    recurrence_label: str | None
    when: str  # human-readable, in the server's timezone: "Wed 28 Oct 17:00, every Monday" or "note"
    awaiting_acknowledgement: bool  # delivered, and nobody has said done or snooze yet
    source_text: str

    @classmethod
    def of(cls, item: Item, awaiting: frozenset[int] = frozenset()) -> "ItemOut":
        if item.id is None:
            raise ValueError("Only stored items can be returned from the API")
        return cls(
            id=item.id, text=item.text, kind=item.kind, status=item.status,
            due_at=item.due_at, snoozed_until=item.snoozed_until, created_at=item.created_at,
            recurrence=item.recurrence,
            recurrence_label=recurrence.describe(item.recurrence) if item.recurrence else None,
            when=commands.when(item),
            awaiting_acknowledgement=item.id in awaiting,
            source_text=item.source_text,
        )


def create_app(store: Store, api_token: str | None = None, notifier: Notifier | None = None,
               interval: float = 30.0, bot: "TelegramBot | None" = None, sms: SmsHandler | None = None,
               public_url: str | None = None, limits: RateLimits | None = None,
               voice: VoiceHandler | None = None, email: "EmailWorker | None" = None,
               escalation: "list[Stage] | None" = None, quiet: "QuietHours | None" = None) -> FastAPI:
    """Build the app. With a notifier, due reminders are also fired from a background task;
    with a bot (maestro.telegram.TelegramBot), chat messages are handled from another; with an
    SmsHandler (maestro.sms), the Twilio webhook is enabled. `public_url` is the externally visible
    base URL, needed to verify webhook signatures behind a reverse proxy. `limits` defaults to
    RateLimits(): public endpoints and failed authentication are rate limited per client."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        tasks: list[asyncio.Task[None]] = []
        if notifier:
            tasks.append(asyncio.create_task(_scheduler_loop(store, notifier, interval, escalation, quiet)))
        if bot:
            tasks.append(asyncio.create_task(_bot_loop(bot)))
        if email:
            email.start()
            tasks.append(asyncio.create_task(_email_loop(email)))
        yield
        for task in tasks:
            task.cancel()
        if email:
            email.stop()

    app = FastAPI(title="Maestro", version=__version__, lifespan=lifespan,
                  description="Self-hosted second brain: capture things to remember, get reminded.")

    limits = limits or RateLimits()
    webhook_bucket = TokenBucket(limits.webhook_per_minute) if limits.webhook_per_minute else None
    failure_bucket = TokenBucket(limits.auth_failures_per_minute) if limits.auth_failures_per_minute else None

    def client_of(request: Request) -> str:
        return limits.client(request.client.host if request.client else None,
                             request.headers.getlist("X-Forwarded-For"))

    def too_many(bucket: TokenBucket, client: str, reason: str) -> HTTPException:
        METRICS.inc("maestro_rate_limited_total", reason=reason)
        log.warning("Rate limited %s (%s)", client, reason)
        return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests",
                             headers={"Retry-After": str(bucket.retry_after(client))})

    @app.middleware("http")
    async def lockout(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # A client locked out for guessing the API token is refused everywhere except webhooks, which
        # carry their own signature auth and must keep working for the owner (e.g. behind shared NAT).
        if failure_bucket and not request.url.path.startswith("/webhooks/"):
            client = client_of(request)
            if failure_bucket.is_empty(client):
                error = too_many(failure_bucket, client, "auth_failures")
                return JSONResponse({"detail": error.detail}, status_code=error.status_code,
                                    headers=error.headers)
        return await call_next(request)

    async def require_token(request: Request, authorization: str | None = Header(default=None)) -> None:
        if api_token is None:
            return
        if authorization is None:
            # Not a guess (e.g. the web UI before a token is entered), so it doesn't count.
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing API token")
        if not hmac.compare_digest(authorization, f"Bearer {api_token}"):
            # Check and record in one step: no await in between, so concurrent guesses can't all
            # slip past the limit before any failure is counted.
            client = client_of(request)
            if failure_bucket and not failure_bucket.take(client):
                raise too_many(failure_bucket, client, "auth_failures")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API token")

    async def twilio_params(request: Request, handler: TwilioHandler) -> dict[str, str]:
        """The form parameters of a Twilio webhook, after checking its signature.

        Authenticated by Twilio's signature rather than the API token, which Twilio can't send. Only
        unsigned/forged requests are rate limited: genuine callbacks are never throttled, and nothing
        reaches the parser without a valid signature.
        """
        form = parse_qs((await request.body()).decode(), keep_blank_values=True)
        params = {key: values[0] for key, values in form.items()}
        # Twilio signs the URL it called; behind a proxy that's the public URL, not what we see.
        url = str(request.url)
        if public_url:
            query = f"?{request.url.query}" if request.url.query else ""
            url = f"{public_url.rstrip('/')}{request.url.path}{query}"
        if not handler.verify(url, params, request.headers.get("X-Twilio-Signature")):
            client = client_of(request)
            if webhook_bucket and not webhook_bucket.take(client):
                raise too_many(webhook_bucket, client, "webhook")
            log.warning("Rejected Twilio webhook with a bad signature (checked against %s)", url)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid Twilio signature")
        return params

    def twiml_response(body: str) -> Response:
        return Response(body, media_type="application/xml")

    # Webhooks exist only when configured; otherwise their paths are plain 404s.
    if sms is not None:
        sms_handler = sms

        @app.post(WEBHOOK_PATH, include_in_schema=False)
        async def twilio_sms(request: Request) -> Response:
            return twiml_response(sms_handler.handle(await twilio_params(request, sms_handler)))

    if voice is not None:
        voice_handler = voice

        @app.post(VOICE_PATH, include_in_schema=False)
        async def twilio_voice(request: Request) -> Response:
            return twiml_response(voice_handler.incoming(await twilio_params(request, voice_handler)))

        @app.post(VOICE_CAPTURE_PATH, include_in_schema=False)
        async def twilio_voice_capture(request: Request) -> Response:
            return twiml_response(voice_handler.capture(await twilio_params(request, voice_handler)))

        calls = CallReminderHandler(store, voice_handler.auth_token, voice_handler.owner)

        @app.post(VOICE_REMINDER_PATH, include_in_schema=False)
        async def twilio_voice_reminder(request: Request) -> Response:
            params = await twilio_params(request, calls)
            return twiml_response(calls.keypress(dict(request.query_params), params))

    def client_name(request: Request) -> str:
        """Which interface acted, for acknowledgements: the bundled web UI says so; anything else is api."""
        return "web" if request.headers.get("X-Maestro-Client") == "web" else "api"

    def get_or_404(item_id: int) -> Item:
        if (item := store.get(item_id)) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No item #{item_id}")
        return item

    @app.get("/", include_in_schema=False)
    async def web_ui() -> HTMLResponse:
        # The page itself holds no data; it calls the token-protected API from the browser.
        return HTMLResponse(WEB_UI)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__, "schema": store.schema_version,
                "timezone": store.tz.key}

    @app.get("/metrics", response_class=PlainTextResponse, dependencies=[Depends(require_token)])
    async def metrics() -> PlainTextResponse:
        items: dict[Labels, float] = {
            (("kind", kind), ("status", status)): n for (kind, status), n in store.counts().items()
        }
        unacknowledged: dict[Labels, float] = {(): float(len(store.unacknowledged_ids()))}
        return PlainTextResponse(METRICS.render(extra={"maestro_items": items,
                                                       "maestro_unacknowledged": unacknowledged}),
                                 media_type="text/plain; version=0.0.4")

    @app.post("/items", status_code=201, dependencies=[Depends(require_token)])
    async def capture(body: CaptureIn) -> ItemOut:
        METRICS.inc("maestro_captures_total", source="api")
        return ItemOut.of(store.add(parse(body.text, tz=store.tz)))

    @app.get("/items", dependencies=[Depends(require_token)])
    async def list_items(include_done: bool = False, q: str | None = None,
                         unacknowledged: bool = False) -> list[ItemOut]:
        awaiting = store.unacknowledged_ids()
        if unacknowledged:
            items = [item for item, _ in store.unacknowledged()]
            if q:  # filters combine rather than being silently dropped
                items = [item for item in items if q.lower() in item.text.lower()]
        else:
            items = store.search(q) if q else store.list(include_done=include_done)
        return [ItemOut.of(i, awaiting) for i in items]

    @app.get("/items/{item_id}", dependencies=[Depends(require_token)])
    async def get_item(item_id: int) -> ItemOut:
        return ItemOut.of(get_or_404(item_id), store.unacknowledged_ids())

    @app.post("/items/{item_id}/done", dependencies=[Depends(require_token)])
    async def complete(item_id: int, request: Request) -> ItemOut:
        get_or_404(item_id)
        store.complete(item_id, via=client_name(request))
        return ItemOut.of(get_or_404(item_id), store.unacknowledged_ids())

    @app.post("/items/{item_id}/stop", dependencies=[Depends(require_token)])
    async def stop(item_id: int, request: Request) -> ItemOut:
        """End an item for good; for a recurring reminder, the whole series (done is one occurrence)."""
        get_or_404(item_id)
        store.stop(item_id, via=client_name(request))
        return ItemOut.of(get_or_404(item_id), store.unacknowledged_ids())

    @app.post("/items/{item_id}/snooze", dependencies=[Depends(require_token)])
    async def snooze(item_id: int, body: SnoozeIn, request: Request) -> ItemOut:
        get_or_404(item_id)
        try:
            delta = parse_duration(body.duration)
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
        store.snooze(item_id, add_elapsed(datetime.now(store.tz).replace(microsecond=0), delta),
                     via=client_name(request))
        return ItemOut.of(get_or_404(item_id), store.unacknowledged_ids())

    @app.delete("/items/{item_id}", status_code=204, dependencies=[Depends(require_token)])
    async def delete(item_id: int) -> None:
        get_or_404(item_id)
        store.delete(item_id)

    return app
