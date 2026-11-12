"""HTTP API for self-hosted deployments. Install with `pip install 'maestro-orchestrator[server]'`.

Every route except /health requires `Authorization: Bearer $MAESTRO_API_TOKEN` when a token is configured,
or a device token from `POST /devices` (ADR 0018), which works everywhere but on /devices itself.
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
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from maestro import __version__, commands, devices, feed, gaps, recurrence
from maestro import captures as confirmations
from maestro.actions import Action, apply
from maestro.interpret import Context, Interpreter, interpret_assessed
from maestro.models import Capture, CaptureStatus, Device, Item, Kind
from maestro.notifier import Notifier
from maestro.observability import METRICS, Labels
from maestro.parser import parse_duration
from maestro.ratelimit import RateLimits, TokenBucket
from maestro.scheduler import Lease, tick
from maestro.sms import WEBHOOK_PATH, SmsHandler, TwilioHandler
from maestro.storage import Store
from maestro.tz import add_elapsed
from maestro.voice import (
    VOICE_CAPTURE_PATH,
    VOICE_PATH,
    VOICE_REMINDER_PATH,
    CallReminderHandler,
    VoiceHandler,
)

if TYPE_CHECKING:
    from maestro.confidence import Confidence
    from maestro.email_in import EmailWorker
    from maestro.escalation import Stage
    from maestro.quiet import QuietHours
    from maestro.telegram import TelegramBot

log = logging.getLogger(__name__)

WEB_UI = files("maestro").joinpath("web/index.html").read_text(encoding="utf-8")

# The React app (ADR 0014), built into here by `npm run build` in web-app/ and served at /app.
WEB_APP_DIR = Path(__file__).parent / "web" / "app"
WEB_APP_MISSING = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Maestro web app not built</title></head>
<body><h1>The web app hasn't been built</h1>
<p>From a source checkout, run <code>cd web-app &amp;&amp; npm ci &amp;&amp; npm run build</code>,
then restart <code>maestro serve</code>. The current web UI is at <a href="/">/</a>.</p></body></html>
"""


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


class ProposalOut(BaseModel):
    """A capture awaiting confirmation (#117): what would be saved, not yet an item."""

    id: int
    status: str  # pending, applied, cancelled, expired or undone
    lines: list[str]  # what confirming would save, one line per item
    source: str
    source_text: str
    interpreter: str | None  # who read it (#116)
    doubts: list[str]  # why it was unsure (maestro.confidence)
    created_at: datetime
    expires_at: datetime
    undo_until: datetime | None  # while applied: undo is allowed until then
    item_ids: list[int]  # the items it created, once applied

    @classmethod
    def of(cls, capture: Capture, item_ids: list[int] | None = None) -> "ProposalOut":
        if capture.id is None:
            raise ValueError("Only stored captures can be returned from the API")
        return cls(id=capture.id, status=str(capture.status),
                   lines=confirmations.proposed_lines(capture),
                   source=capture.source, source_text=capture.source_text,
                   interpreter=capture.interpreter,
                   doubts=[d for d in capture.doubts.split(",") if d],
                   created_at=capture.created_at, expires_at=capture.expires_at,
                   undo_until=capture.undo_until, item_ids=item_ids or [])


class SnoozeIn(BaseModel):
    duration: str = Field(examples=["10m", "2h", "1d"])


class FillIn(BaseModel):
    """What an item that needs a look is missing: a time, a channel, or the words it never had."""

    phrase: str = Field(min_length=1, max_length=2000, examples=["tomorrow at 9am", "5pm", "email"])


class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=devices.MAX_NAME, examples=["Sachin's iPhone"])


class DeviceOut(BaseModel):
    """A phone or browser signed in with a token of its own (#108, ADR 0018)."""

    id: int
    name: str
    created_at: datetime
    last_seen_at: datetime | None  # to the hour
    current: bool  # the device making this request

    @classmethod
    def of(cls, device: Device, current: bool = False) -> "DeviceOut":
        if device.id is None:
            raise ValueError("device has no id")
        return cls(id=device.id, name=device.name, created_at=device.created_at,
                   last_seen_at=device.last_seen_at, current=current)


class DeviceCreatedOut(DeviceOut):
    """The answer to signing a device in: the one and only copy of its token."""

    token: str


class UndoOut(BaseModel):
    """The token that takes a whole capture back, for as long as it lasts (#126)."""

    id: int
    expires_at: datetime


class CaptureOut(BaseModel):
    """What one capture did: what was saved, what still needs a look, and how to undo all of it."""

    added: list["ItemOut"]  # saved and, if they have a time, scheduled
    needs_look: list["ItemOut"]  # saved with a gap: visible, never scheduled
    undo: UndoOut | None  # null when the capture saved nothing to take back


class OccurrenceOut(BaseModel):
    """One time a reminder goes (or went) out: a row in the list (#22)."""

    item: "ItemOut"
    at: datetime
    state: str  # scheduled, overdue, awaiting, held or snoozed (maestro.feed.STATES)
    held_until: datetime | None  # while held: when quiet hours let it out
    escalation_label: str | None  # while awaiting: the next stage, "text at 17:12"
    acts: bool  # Done and +1 hour apply to this row (the first occurrence of its series)


class FeedOut(BaseModel):
    """Everything the list screen shows, in one call (#22, ADR 0017). The client groups `upcoming`
    into days in `timezone`; the server only says when each row happens and what it is doing."""

    now: datetime
    until: datetime  # exclusive
    timezone: str
    needs_look: list["ItemOut"]  # saved with a gap, newest first
    upcoming: list[OccurrenceOut]  # by `at`
    anytime: list["ItemOut"]  # tasks, oldest first
    later: int  # reminders whose next time is past the window


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
    channels: list[str] | None  # this item's own delivery channels; null means MAESTRO_CHANNELS
    urgent: bool | None  # urgent reminders ignore quiet hours; null on items from before the flag
    when: str  # human-readable, in the server's timezone: "Wed 28 Oct 17:00, every Monday" or "note"
    awaiting_acknowledgement: bool  # delivered, and nobody has said done or snooze yet
    source_text: str
    # Which interpreter read the capture: "rules" or "model" (#116); null on items from before it
    # was recorded, or saved without a capture.
    interpreter: str | None = None
    # What this item still needs before anything can happen to it: "when", "what" or "channel"
    # (#126). Null — the normal case — means nothing; an item with one is never scheduled.
    gap: str | None = None

    @classmethod
    def of(cls, item: Item, awaiting: frozenset[int] = frozenset()) -> "ItemOut":
        if item.id is None:
            raise ValueError("Only stored items can be returned from the API")
        return cls(
            id=item.id, text=item.text, kind=item.kind, status=item.status,
            due_at=item.due_at, snoozed_until=item.snoozed_until, created_at=item.created_at,
            recurrence=item.recurrence,
            recurrence_label=recurrence.describe(item.recurrence) if item.recurrence else None,
            channels=item.channels, urgent=item.urgent,
            when=commands.when(item),
            awaiting_acknowledgement=item.id in awaiting,
            source_text=item.source_text,
            interpreter=item.interpreter,
            gap=item.gap,
        )


def create_app(store: Store, api_token: str | None = None, notifier: Notifier | None = None,
               interval: float = 30.0, bot: "TelegramBot | None" = None, sms: SmsHandler | None = None,
               public_url: str | None = None, limits: RateLimits | None = None,
               voice: VoiceHandler | None = None, email: "EmailWorker | None" = None,
               escalation: "list[Stage] | None" = None, quiet: "QuietHours | None" = None,
               requestable: frozenset[str] | None = None,
               interpreter: Interpreter | None = None,
               confirm: "confirmations.Confirmation | None" = None) -> FastAPI:
    """Build the app. With a notifier, due reminders are also fired from a background task;
    with a bot (maestro.telegram.TelegramBot), chat messages are handled from another; with an
    SmsHandler (maestro.sms), the Twilio webhook is enabled. `public_url` is the externally visible
    base URL, needed to verify webhook signatures behind a reverse proxy. `limits` defaults to
    RateLimits(): public endpoints and failed authentication are rate limited per client.
    `interpreter` is how a capture is read; None means the rules (maestro.interpret). With
    `confirm` (#117), an unsure interpretation is answered 202 as a proposal to confirm."""

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

    def rejected(request: Request) -> HTTPException:
        # Check and record in one step: no await in between, so concurrent guesses can't all slip
        # past the limit before any failure is counted.
        client = client_of(request)
        if failure_bucket and not failure_bucket.take(client):
            return too_many(failure_bucket, client, "auth_failures")
        return HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API token")

    async def require_token(request: Request, authorization: str | None = Header(default=None)) -> None:
        """The master token (MAESTRO_API_TOKEN) or a live device token (ADR 0018). The device, if
        one, is left in `request.state.device`; the master token leaves None."""
        request.state.device = None
        if api_token is None:
            return
        if authorization is None:
            # Not a guess (e.g. the web UI before a token is entered), so it doesn't count.
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing API token")
        if hmac.compare_digest(authorization, f"Bearer {api_token}"):
            return
        presented = authorization.removeprefix("Bearer ")
        device = devices.authenticate(store, presented, datetime.now(store.tz)) \
            if authorization != presented else None
        if device is None:
            raise rejected(request)
        request.state.device = device

    async def require_master_token(request: Request,
                                   authorization: str | None = Header(default=None)) -> None:
        """Only the master token: a device can't add or revoke devices, so a lost phone can't
        make itself a spare key."""
        await require_token(request, authorization)
        if request.state.device is not None:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Only the server's API token can manage devices")

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

    # Public like /: the app holds no data and calls the token-protected API from the browser.
    if (WEB_APP_DIR / "index.html").is_file():
        app.mount("/app", StaticFiles(directory=WEB_APP_DIR, html=True), name="web-app")
    else:
        @app.get("/app/{path:path}", include_in_schema=False)
        async def web_app_missing(path: str) -> HTMLResponse:
            return HTMLResponse(WEB_APP_MISSING, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__, "schema": store.schema_version,
                "timezone": store.tz.key, "backend": store.backend}

    # Devices (#108, ADR 0018): the app sends the master token once and keeps a token of its own.

    @app.post("/devices", status_code=201, dependencies=[Depends(require_master_token)])
    async def add_device(body: DeviceIn) -> DeviceCreatedOut:
        """Sign a device in. The answer carries the only copy of its token; store it on the device."""
        device, token = devices.register(store, body.name, datetime.now(store.tz).replace(microsecond=0))
        log.info("Device #%d signed in", device.id)
        METRICS.inc("maestro_devices_total", action="added")
        return DeviceCreatedOut(**DeviceOut.of(device).model_dump(), token=token)

    @app.get("/devices", dependencies=[Depends(require_token)])
    async def list_devices(request: Request) -> list[DeviceOut]:
        """Devices that can sign in. `current` marks the one asking, when it is one."""
        me: Device | None = request.state.device
        return [DeviceOut.of(d, current=me is not None and d.id == me.id) for d in store.devices()]

    @app.delete("/devices/{device_id}", status_code=204, dependencies=[Depends(require_master_token)])
    async def revoke_device(device_id: int) -> Response:
        """Stop a device's token working, at once and for good."""
        if not store.revoke_device(device_id, datetime.now(store.tz).replace(microsecond=0)):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No device #{device_id}")
        log.info("Device #%d revoked", device_id)
        METRICS.inc("maestro_devices_total", action="revoked")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/metrics", response_class=PlainTextResponse, dependencies=[Depends(require_token)])
    async def metrics() -> PlainTextResponse:
        items: dict[Labels, float] = {
            (("kind", kind), ("status", status)): n for (kind, status), n in store.counts().items()
        }
        unacknowledged: dict[Labels, float] = {(): float(len(store.unacknowledged_ids()))}
        # Which backend is in use, as a label on a constant: a label on the existing counters
        # would break anyone's dashboards.
        store_info: dict[Labels, float] = {(("backend", store.backend),): 1.0}
        return PlainTextResponse(METRICS.render(extra={"maestro_items": items,
                                                       "maestro_unacknowledged": unacknowledged,
                                                       "maestro_store_info": store_info}),
                                 media_type="text/plain; version=0.0.4")

    async def read_capture(text: str) -> tuple[list[Action], "Confidence"]:
        # Reading the text can mean waiting on a model (#115), so it happens in a worker thread:
        # one slow capture must not stall the scheduler task or every other request. Applying stays
        # on the event loop, where the store has always been used (ADR 0003).
        return await run_in_threadpool(
            interpret_assessed, text, Context(tz=store.tz), interpreter)

    @app.post("/items", status_code=201, dependencies=[Depends(require_token)])
    async def capture(body: CaptureIn, response: Response) -> ItemOut | ProposalOut:
        """Capture one message, answering with the first item it saved.

        The shape this has always had, kept for existing clients: `POST /capture` reports
        everything one message did, including what still needs a look (#126).
        """
        captured, confidence = await read_capture(body.text)
        if confirm is not None and confirm.splits:
            split = gaps.split_apply(store, captured, confidence, "api", "", body.text,
                                     requestable=requestable)
            for _ in split.saved:
                METRICS.inc("maestro_captures_total", source="api")
            # The first item the capture asked for, gap or no gap: the response has always been one
            # item, and an item with a gap is still an item — its `gap` field says it is waiting.
            return ItemOut.of(split.saved[0])
        if confirm and confirmations.needs_confirmation(captured, confidence):
            proposal, _ = confirmations.propose(store, captured, confidence, "api", "", body.text,
                                                ttl=confirm.ttl)
            response.status_code = status.HTTP_202_ACCEPTED  # asked, not saved (#117)
            return ProposalOut.of(proposal)
        METRICS.inc("maestro_captures_total", source="api")
        results = apply(store, captured, source="api", requestable=requestable)
        for result in results:
            if result.dropped:
                log.warning("Capture asked for %s, which isn't configured; keeping %s",
                            ", ".join(result.dropped), result.saved.channels or "the default channels")
        if len(results) > 1:
            # The response is one item, as it has always been; the rest are saved and listed by
            # GET /items. Reporting several from one capture is part of #117.
            log.info("One capture saved %d items; the response names the first", len(results))
        return ItemOut.of(results[0].saved)

    @app.post("/capture", status_code=201, dependencies=[Depends(require_token)])
    async def capture_split(body: CaptureIn, response: Response) -> CaptureOut | ProposalOut:
        """Capture one message and say what became of all of it (#126): what was added, what still
        needs a look, and the token that takes the whole lot back.

        Under `MAESTRO_CONFIRM=unsure` an unsure message is still held whole, and this answers 202
        with the proposal, exactly as `POST /items` does.
        """
        captured, confidence = await read_capture(body.text)
        if confirm is not None and not confirm.splits and \
                confirmations.needs_confirmation(captured, confidence):
            proposal, _ = confirmations.propose(store, captured, confidence, "api", "", body.text,
                                                ttl=confirm.ttl)
            response.status_code = status.HTTP_202_ACCEPTED
            return ProposalOut.of(proposal)
        if confirm is not None and confirm.splits:
            split = gaps.split_apply(store, captured, confidence, "api", "", body.text,
                                     requestable=requestable)
        else:  # MAESTRO_CONFIRM=never: everything is applied, and there is nothing to undo
            split = gaps.applied(apply(store, captured, source="api", requestable=requestable))
        for _ in split.saved:
            METRICS.inc("maestro_captures_total", source="api")
        undo = UndoOut(id=split.undo_id, expires_at=split.undo_until) \
            if split.undo_id is not None and split.undo_until is not None and split.added else None
        return CaptureOut(added=[ItemOut.of(i) for i in split.added],
                          needs_look=[ItemOut.of(i) for i in split.needs_look], undo=undo)

    @app.get("/needs-look", dependencies=[Depends(require_token)])
    async def list_needs_look() -> list[ItemOut]:
        """Readings saved but waiting for an answer (#126). They never expire, so this is the
        whole list however old."""
        return [ItemOut.of(item) for item in store.needs_look()]

    def gapped_or_404(item_id: int) -> Item:
        item = store.get(item_id)
        if item is None or item.gap is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No item #{item_id} needing a look")
        return item

    @app.post("/needs-look/{item_id}", dependencies=[Depends(require_token)])
    async def fill_needs_look(item_id: int, body: FillIn) -> ItemOut:
        """Answer what an item is missing: a time for `when`, a channel for `channel`, or the words
        for `what`. A phrase that doesn't answer it leaves the item exactly as it was."""
        item = gapped_or_404(item_id)
        # On the event loop, not in a worker thread: this writes to the store, and the one SQLite
        # connection is only ever used from this thread (ADR 0003). Only *reading* a capture can
        # wait on a model and so belongs in a thread; an answer is read by the rules alone (#126).
        filled = gaps.fill(store, item, body.phrase, requestable)
        if filled is None:
            wanted = gaps.WANTED.get(str(item.gap), "an answer")
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                f"Couldn't read {wanted} in that; #{item_id} still needs {wanted}")
        return ItemOut.of(filled)

    @app.delete("/needs-look/{item_id}", status_code=204, dependencies=[Depends(require_token)])
    async def dismiss_needs_look(item_id: int) -> Response:
        """Drop a reading rather than answering it."""
        gaps.dismiss(store, gapped_or_404(item_id))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/captures", dependencies=[Depends(require_token)])
    async def list_captures(status_filter: str | None = Query(default=None, alias="status")) -> \
            list[ProposalOut]:
        """Captures awaiting confirmation (#117): pending and expired unless ?status= names one."""
        if status_filter is None:
            wanted = [str(CaptureStatus.PENDING), str(CaptureStatus.EXPIRED)]
        elif status_filter in list(CaptureStatus):
            wanted = [status_filter]
        else:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                f"status must be one of {', '.join(CaptureStatus)}")
        confirmations.expire(store)  # so what is listed as pending really still is
        return [ProposalOut.of(c) for w in wanted for c in store.captures(status=w)]

    def capture_or_404(capture_id: int) -> Capture:
        if (capture := store.get_capture(capture_id)) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No capture #{capture_id}")
        return capture

    def decided(capture_id: int, wanted: CaptureStatus) -> ProposalOut:
        """The capture after a decision: 200 only when it ended in the state this endpoint asks
        for (so a double tap is idempotent), 409 otherwise (confirming something undone,
        cancelling something applied, an expired capture)."""
        capture = capture_or_404(capture_id)
        if capture.status != wanted:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"Capture #{capture_id} is {capture.status}; nothing was changed")
        return ProposalOut.of(capture, [i.id for i in store.items_for_capture(capture_id)
                                        if i.id is not None])

    @app.post("/captures/{capture_id}/confirm", dependencies=[Depends(require_token)])
    async def confirm_capture(capture_id: int, request: Request) -> ProposalOut:
        capture_or_404(capture_id)
        outcome, items = confirmations.confirm(store, capture_id, via=client_name(request),
                                               requestable=requestable)
        if outcome == confirmations.Outcome.APPLIED:
            for _ in items:
                METRICS.inc("maestro_captures_total", source="api")
        return decided(capture_id, CaptureStatus.APPLIED)

    @app.post("/captures/{capture_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel_capture(capture_id: int, request: Request) -> ProposalOut:
        capture_or_404(capture_id)
        confirmations.cancel(store, capture_id, via=client_name(request))
        return decided(capture_id, CaptureStatus.CANCELLED)

    @app.post("/captures/{capture_id}/undo", dependencies=[Depends(require_token)])
    async def undo_capture(capture_id: int, request: Request) -> ProposalOut:
        capture_or_404(capture_id)
        outcome, _ = confirmations.undo(store, capture_id, via=client_name(request))
        if outcome == confirmations.Outcome.TOO_LATE:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"The undo window for capture #{capture_id} has passed")
        return decided(capture_id, CaptureStatus.UNDONE)

    @app.get("/feed", dependencies=[Depends(require_token)])
    async def upcoming_feed(days: int = Query(default=feed.DEFAULT_DAYS, ge=1, le=feed.MAX_DAYS)) \
            -> FeedOut:
        """The next `days` days as the list shows them (#22): every occurrence of every pending
        reminder (recurring ones expanded, snoozed ones at their snoozed time, delivered ones
        waiting for an answer), plus tasks under Anytime and readings that need a look."""
        built = feed.build(store, days=days, quiet=quiet, escalation=escalation)
        awaiting = store.unacknowledged_ids()
        return FeedOut(
            now=built.now, until=built.until, timezone=store.tz.key,
            needs_look=[ItemOut.of(i, awaiting) for i in built.needs_look],
            upcoming=[OccurrenceOut(item=ItemOut.of(o.item, awaiting), at=o.at, state=o.state,
                                    held_until=o.held_until, escalation_label=o.escalation_label,
                                    acts=o.acts) for o in built.upcoming],
            anytime=[ItemOut.of(i, awaiting) for i in built.anytime],
            later=built.later,
        )

    @app.get("/items", dependencies=[Depends(require_token)])
    async def list_items(include_done: bool = False, q: str | None = None,
                         unacknowledged: bool = False, kind: Kind | None = None) -> list[ItemOut]:
        awaiting = store.unacknowledged_ids()
        if unacknowledged:
            items = [item for item, _ in store.unacknowledged()]
            if q:  # filters combine rather than being silently dropped
                items = [item for item in items if q.lower() in item.text.lower()]
        else:
            items = store.search(q) if q else store.list(include_done=include_done)
        if kind is not None:  # ?kind=task, ?kind=reminder, ?kind=note (#114); anything else is a 422
            items = [item for item in items if item.kind == kind]
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
