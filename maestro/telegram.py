"""Telegram integration: capture by chatting with your own bot, and get reminders with action buttons.

Bring your own bot (create one with @BotFather) and set TELEGRAM_BOT_TOKEN. The bot only talks to
TELEGRAM_CHAT_ID; messages from any other chat are ignored. If the chat id isn't configured yet,
the bot replies to /start with the id so you can set it.

The bot long-polls getUpdates, so it works behind NAT with no public URL or webhook.
"""

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable

from maestro import commands
from maestro.models import Item, Kind
from maestro.observability import METRICS
from maestro.parser import parse
from maestro.store import Store

log = logging.getLogger(__name__)

# (method, params, timeout) -> the "result" field of the Bot API response
Transport = Callable[[str, dict, float], object]

SNOOZE_CHOICES = ["10m", "1h", "1d"]


class TelegramError(RuntimeError):
    pass


def http_transport(token: str) -> Transport:
    base = f"https://api.telegram.org/bot{token}/"

    def call(method: str, params: dict, timeout: float) -> object:
        request = urllib.request.Request(
            base + method, data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as e:
            # Never include the URL: it contains the bot token.
            body = json.load(e) if e.fp else {"description": f"HTTP {e.code}"}
        if not body.get("ok"):
            raise TelegramError(f"{method} failed: {body.get('description', 'unknown error')}")
        return body["result"]

    return call


class TelegramClient:
    def __init__(self, transport: Transport):
        self.transport = transport

    @classmethod
    def for_token(cls, token: str) -> "TelegramClient":
        return cls(http_transport(token))

    def call(self, method: str, params: dict | None = None, http_timeout: float = 10.0) -> object:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        return self.transport(method, params, http_timeout)

    def send(self, chat_id: str | int, text: str, buttons: list[list[dict]] | None = None) -> object:
        markup = {"inline_keyboard": buttons} if buttons else None
        return self.call("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": markup})


def _action_buttons(item: Item) -> list[list[dict]]:
    row = [{"text": "✅ Done", "callback_data": f"done:{item.id}"}]
    row += [{"text": f"+{d}", "callback_data": f"snooze:{item.id}:{d}"} for d in SNOOZE_CHOICES]
    return [row]


class TelegramNotifier:
    """Delivery channel: sends the reminder to your chat with Done / snooze buttons."""

    def __init__(self, client: TelegramClient, chat_id: str):
        self.client = client
        self.chat_id = chat_id

    def send(self, item: Item) -> None:
        self.client.send(self.chat_id, f"⏰ {item.text}", _action_buttons(item))


class TelegramBot:
    """Turns chat messages into captures and commands. Network fetches and store access are split
    (fetch / handle) so the server can fetch in a worker thread and handle on its event loop."""

    def __init__(self, client: TelegramClient, store: Store, chat_id: str | None):
        self.client = client
        self.store = store
        self.chat_id = str(chat_id) if chat_id else None
        self.offset: int | None = None

    def fetch(self, timeout: int = 25) -> list[dict]:
        """Long-poll for updates. Network only: safe to run in a worker thread."""
        updates = self.client.call(
            "getUpdates",
            {"offset": self.offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
            http_timeout=timeout + 10,  # must outlast the server-side long poll
        )
        if updates:
            self.offset = updates[-1]["update_id"] + 1
        return updates

    def poll_once(self, timeout: int = 25) -> None:
        for update in self.fetch(timeout):
            self.handle(update)

    def handle(self, update: dict) -> None:
        try:
            if query := update.get("callback_query"):
                self._handle_callback(query)
            elif (message := update.get("message")) and "text" in message:
                self._handle_message(message)
        except TelegramError:
            log.exception("Telegram call failed while handling update %s", update.get("update_id"))

    # --- messages -------------------------------------------------------------------------

    def _allowed(self, chat_id: int | str) -> bool:
        return self.chat_id is not None and str(chat_id) == self.chat_id

    def _handle_message(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        text = message["text"].strip()
        if not self._allowed(chat_id):
            if self.chat_id is None and text.startswith("/start"):
                self.client.send(chat_id, f"Your chat id is {chat_id}. Set TELEGRAM_CHAT_ID={chat_id} "
                                          "on your Maestro server and restart it.")
            else:
                log.warning("Ignoring Telegram message from unauthorised chat %s", chat_id)
            return

        if not text.startswith("/"):
            METRICS.inc("maestro_captures_total", source="telegram")
            item = self.store.add(parse(text, tz=self.store.tz))
            buttons = _action_buttons(item) if item.kind == Kind.REMINDER else None
            self.client.send(chat_id, f"Saved {commands.line(item)}", buttons)
            return

        command, *args = text.split()
        command = command.split("@")[0].lower()  # "/list@MyMaestroBot" in group chats
        if command in ("/start", "/help"):
            self.client.send(chat_id, commands.help_text(prefix="/"))
        elif command == "/list":
            self.client.send(chat_id, commands.list_items(self.store))
        elif command in ("/done", "/rm", "/snooze"):
            self.client.send(chat_id, commands.act(self.store, command[1:], args, prefix="/"))
        else:
            self.client.send(chat_id, f"Unknown command {command}. Try /help")

    # --- buttons ---------------------------------------------------------------------------

    def _handle_callback(self, query: dict) -> None:
        message = query.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        if not self._allowed(chat_id):
            self.client.call("answerCallbackQuery", {"callback_query_id": query["id"]})
            return
        action, _, rest = query.get("data", "").partition(":")
        args = rest.split(":") if rest else []
        result = commands.act(self.store, action, args) if action in ("done", "snooze") else "Unknown action"
        self.client.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": result[:200]})
        # Replace the buttons with the outcome so old reminders can't be acted on twice by accident.
        self.client.call("editMessageText", {"chat_id": chat_id, "message_id": message.get("message_id"),
                                             "text": f"{message.get('text', '')}\n{result}"})
