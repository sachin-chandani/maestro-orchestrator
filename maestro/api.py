"""HTTP API for self-hosted deployments. Install with `pip install 'maestro-orchestrator[server]'`.

Every route except /health requires `Authorization: Bearer $MAESTRO_API_TOKEN` when a token is configured.
Handlers are async on purpose: they all run on the event-loop thread, so the single SQLite
connection is never used from two threads at once.
"""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from importlib.resources import files

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from maestro import __version__, recurrence
from maestro.models import Item, Status
from maestro.notifier import Notifier
from maestro.parser import parse, parse_duration
from maestro.scheduler import fire_due
from maestro.store import Store
from maestro.tz import add_elapsed

log = logging.getLogger(__name__)

WEB_UI = files("maestro").joinpath("web/index.html").read_text(encoding="utf-8")


async def _scheduler_loop(store: Store, notifier: Notifier, interval: float) -> None:
    # Runs on the event loop with the request handlers, keeping SQLite access single-threaded.
    while True:
        fire_due(store, notifier)
        await asyncio.sleep(interval)


async def _bot_loop(bot) -> None:
    # The long poll runs in a worker thread; handling (which touches SQLite) stays on the event loop.
    while True:
        try:
            updates = await asyncio.to_thread(bot.fetch)
        except Exception:
            log.exception("Telegram polling failed; retrying in 10s")
            await asyncio.sleep(10)
            continue
        for update in updates:
            bot.handle(update)


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
    created_at: datetime
    recurrence: str | None
    recurrence_label: str | None
    source_text: str

    @classmethod
    def of(cls, item: Item) -> "ItemOut":
        return cls(
            id=item.id, text=item.text, kind=item.kind, status=item.status,
            due_at=item.due_at, created_at=item.created_at, recurrence=item.recurrence,
            recurrence_label=recurrence.describe(item.recurrence) if item.recurrence else None,
            source_text=item.source_text,
        )


def create_app(store: Store, api_token: str | None = None, notifier: Notifier | None = None,
               interval: float = 30.0, bot=None) -> FastAPI:
    """Build the app. With a notifier, due reminders are also fired from a background task;
    with a bot (maestro.telegram.TelegramBot), chat messages are handled from another."""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks = []
        if notifier:
            tasks.append(asyncio.create_task(_scheduler_loop(store, notifier, interval)))
        if bot:
            tasks.append(asyncio.create_task(_bot_loop(bot)))
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(title="Maestro", version=__version__, lifespan=lifespan,
                  description="Self-hosted second brain: capture things to remember, get reminded.")

    async def require_token(authorization: str | None = Header(default=None)) -> None:
        if api_token is None:
            return
        expected = f"Bearer {api_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API token")

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

    @app.post("/items", status_code=201, dependencies=[Depends(require_token)])
    async def capture(body: CaptureIn) -> ItemOut:
        return ItemOut.of(store.add(parse(body.text, tz=store.tz)))

    @app.get("/items", dependencies=[Depends(require_token)])
    async def list_items(include_done: bool = False, q: str | None = None) -> list[ItemOut]:
        items = store.search(q) if q else store.list(include_done=include_done)
        return [ItemOut.of(i) for i in items]

    @app.get("/items/{item_id}", dependencies=[Depends(require_token)])
    async def get_item(item_id: int) -> ItemOut:
        return ItemOut.of(get_or_404(item_id))

    @app.post("/items/{item_id}/done", dependencies=[Depends(require_token)])
    async def complete(item_id: int) -> ItemOut:
        get_or_404(item_id)
        store.set_status(item_id, Status.DONE)
        return ItemOut.of(store.get(item_id))

    @app.post("/items/{item_id}/snooze", dependencies=[Depends(require_token)])
    async def snooze(item_id: int, body: SnoozeIn) -> ItemOut:
        get_or_404(item_id)
        try:
            delta = parse_duration(body.duration)
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
        store.reschedule(item_id, add_elapsed(datetime.now(store.tz).replace(microsecond=0), delta))
        return ItemOut.of(store.get(item_id))

    @app.delete("/items/{item_id}", status_code=204, dependencies=[Depends(require_token)])
    async def delete(item_id: int) -> None:
        get_or_404(item_id)
        store.delete(item_id)

    return app
