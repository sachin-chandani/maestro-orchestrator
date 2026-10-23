"""HTTP API for self-hosted deployments. Install with `pip install 'maestro-orchestrator[server]'`.

Every route except /health requires `Authorization: Bearer $MAESTRO_API_TOKEN` when a token is configured.
Handlers are async on purpose: they all run on the event-loop thread, so the single SQLite
connection is never used from two threads at once.
"""

import asyncio
import hmac
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from maestro import __version__, recurrence
from maestro.models import Item, Status
from maestro.notifier import Notifier
from maestro.parser import parse, parse_duration
from maestro.scheduler import fire_due
from maestro.store import Store


async def _scheduler_loop(store: Store, notifier: Notifier, interval: float) -> None:
    # Runs on the event loop with the request handlers, keeping SQLite access single-threaded.
    while True:
        fire_due(store, notifier)
        await asyncio.sleep(interval)


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
               interval: float = 30.0) -> FastAPI:
    """Build the app. With a notifier, due reminders are also fired from a background task."""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(_scheduler_loop(store, notifier, interval)) if notifier else None
        yield
        if task:
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

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__, "schema": store.schema_version}

    @app.post("/items", status_code=201, dependencies=[Depends(require_token)])
    async def capture(body: CaptureIn) -> ItemOut:
        return ItemOut.of(store.add(parse(body.text)))

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
        store.reschedule(item_id, datetime.now().replace(microsecond=0) + delta)
        return ItemOut.of(store.get(item_id))

    @app.delete("/items/{item_id}", status_code=204, dependencies=[Depends(require_token)])
    async def delete(item_id: int) -> None:
        get_or_404(item_id)
        store.delete(item_id)

    return app
