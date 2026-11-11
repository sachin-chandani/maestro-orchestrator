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

from maestro import captures as confirmations
from maestro import commands, gaps
from maestro.actions import apply
from maestro.interpret import Context, Interpreter, interpret_assessed
from maestro.models import Capture, Item, Kind
from maestro.observability import METRICS
from maestro.storage import Store

log = logging.getLogger(__name__)

# (method, params, timeout) -> the "result" field of the Bot API response
Transport = Callable[[str, dict, float], object]

SNOOZE_CHOICES = ["10m", "1h", "1d"]
CHANNEL = "telegram"  # replies act on the reminder last delivered on this channel


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


def _confirmation_buttons(capture_id: int | None) -> list[list[dict]]:
    return [[{"text": "✅ Yes", "callback_data": f"cap:{capture_id}:yes"},
             {"text": "✏️ Change", "callback_data": f"cap:{capture_id}:change"},
             {"text": "✖ Cancel", "callback_data": f"cap:{capture_id}:no"}]]


def _undo_button(capture_id: int | None) -> list[list[dict]]:
    return [[{"text": "↩ Undo", "callback_data": f"cap:{capture_id}:undo"}]]


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

    def __init__(self, client: TelegramClient, store: Store, chat_id: str | None,
                 requestable: frozenset[str] | None = None,
                 interpreter: "Interpreter | None" = None,
                 confirm: "confirmations.Confirmation | None" = None):
        self.client = client
        self.store = store
        # How a capture is read; None means the rules (maestro.interpret). Injected, never global.
        self.interpreter = interpreter
        # Ask before acting on an unsure interpretation (#117); None means never, as before.
        self.confirm = confirm
        self.chat_id = str(chat_id) if chat_id else None
        # The per-item channels this deployment can honour; None skips the capture-time check (#20).
        self.requestable = requestable
        self.offset: int | None = None

    def fetch(self, timeout: int = 25) -> list[dict]:
        """Long-poll for updates. Network only: safe to run in a worker thread."""
        updates = self.client.call(
            "getUpdates",
            {"offset": self.offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
            http_timeout=timeout + 10,  # must outlast the server-side long poll
        )
        if not isinstance(updates, list):
            raise TelegramError(f"getUpdates returned {type(updates).__name__}, expected a list")
        if updates:
            self.offset = updates[-1]["update_id"] + 1
        return updates

    def poll_once(self, timeout: int = 25) -> None:
        for update in self.fetch(timeout):
            self.handle(update)

    def handle(self, update: object) -> None:
        """Handle one update. Never raises: a bad update is logged and skipped so polling continues."""
        try:
            if not isinstance(update, dict):
                raise TelegramError(f"update is {type(update).__name__}, expected an object")
            if query := update.get("callback_query"):
                self._handle_callback(query)
            elif (message := update.get("message")) and "text" in message:
                self._handle_message(message)
        except Exception:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            log.exception("Failed to handle Telegram update %s", update_id)

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

        if command := commands.parse_command(text):
            self.client.send(chat_id, commands.run(self.store, command, channel=CHANNEL, prefix="/"))
            return

        # "yes", "no 12" or "undo" answers a confirmation — but only when this chat has a capture
        # the word could answer; otherwise it is captured as any other text (ADR 0005, #117).
        if (reply := confirmations.match_reply(text)) and (
                target := confirmations.target(self.store, CHANNEL, str(chat_id), *reply)):
            self._answer_confirmation(chat_id, target, reply[0])
            return

        # "#12 5pm", "5pm" or "dismiss" answers a question about something needing a look — but only
        # while this chat has one it could answer; otherwise it is captured (ADR 0005, #126).
        if self.confirm is not None and self.confirm.splits and (
                answered := gaps.match_reply(self.store, CHANNEL, str(chat_id), text,
                                             window=self.confirm.window)):
            # Not counted as a capture: the item was counted when the message that produced it
            # was split, and filling its gap completes that item rather than making a new one.
            said, _ = gaps.answer(self.store, answered, requestable=self.requestable)
            self.client.send(chat_id, said)
            return

        if not text.startswith("/"):
            captured, confidence = interpret_assessed(text, Context(tz=self.store.tz), self.interpreter)
            if self.confirm is not None and self.confirm.splits:
                split = gaps.split_apply(self.store, captured, confidence, CHANNEL, str(chat_id),
                                         text, requestable=self.requestable)
                for _ in split.saved:
                    METRICS.inc("maestro_captures_total", source="telegram")
                buttons = _action_buttons(split.added[0]) \
                    if len(split.saved) == 1 and split.added and split.added[0].kind == Kind.REMINDER \
                    else (_undo_button(split.undo_id) if split.added else None)
                self.client.send(chat_id, gaps.worded(split), buttons)
                return
            if self.confirm and confirmations.needs_confirmation(captured, confidence):
                proposal, _ = confirmations.propose(
                    self.store, captured, confidence, CHANNEL, str(chat_id), text,
                    ttl=self.confirm.ttl)
                self.client.send(chat_id, confirmations.question(proposal),
                                 _confirmation_buttons(proposal.id))
                return
            results = apply(self.store, captured, source=CHANNEL, requestable=self.requestable)
            for _ in results:
                METRICS.inc("maestro_captures_total", source="telegram")
            items = [r.saved for r in results]
            note = ""
            if dropped := sorted({c for r in results for c in r.dropped}):
                chosen = {c for i in items for c in (i.channels or [])}
                kept = ", ".join(sorted(chosen)) if chosen else "the default channels"
                note = f"\n({', '.join(dropped)} isn't configured; using {kept})"
            # Buttons act on one reminder, so they are offered when the capture saved exactly one.
            buttons = _action_buttons(items[0]) if len(items) == 1 and items[0].kind == Kind.REMINDER \
                else None
            self.client.send(chat_id, f"{commands.saved_lines(items)}{note}", buttons)
            return

        # A slash command that didn't parse: explain its usage, or that it doesn't exist.
        word, *args = text.split()
        word = word.split("@")[0].lower()  # "/done@MyMaestroBot" in group chats
        if word in ("/done", "/stop", "/rm", "/snooze"):
            self.client.send(chat_id, commands.act(self.store, word[1:], args, prefix="/", via=CHANNEL))
        else:
            self.client.send(chat_id, f"Unknown command {word}. Try /help")

    # --- confirmations (#117) ---------------------------------------------------------------

    def _answer_confirmation(self, chat_id: int | str, target: Capture, verb: str) -> None:
        outcome, items, removed = confirmations.answer(self.store, target, verb, via=CHANNEL,
                                                       requestable=self.requestable)
        if outcome == confirmations.Outcome.APPLIED:  # ALREADY echoes items someone else saved
            for _ in items:
                METRICS.inc("maestro_captures_total", source="telegram")
        hint = "" if outcome != confirmations.Outcome.APPLIED else "\nWrong? Reply undo within 10 min."
        buttons = (_undo_button(target.id) if outcome == confirmations.Outcome.APPLIED else None)
        self.client.send(chat_id, confirmations.worded(outcome, target, items, removed, hint), buttons)

    # --- buttons ---------------------------------------------------------------------------

    def _handle_callback(self, query: dict) -> None:
        message = query.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        if not self._allowed(chat_id):
            self.client.call("answerCallbackQuery", {"callback_query_id": query["id"]})
            return
        action, _, rest = query.get("data", "").partition(":")
        args = rest.split(":") if rest else []
        if action == "cap" and len(args) == 2 and args[0].isdigit():
            result, buttons = self._confirmation_pressed(int(args[0]), args[1])
        else:
            result = commands.act(self.store, action, args, via=CHANNEL) if action in ("done", "snooze") \
                else "Unknown action"
            buttons = None
        self.client.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": result[:200]})
        # Replace the buttons with the outcome so old reminders can't be acted on twice by accident.
        self.client.call("editMessageText", {"chat_id": chat_id, "message_id": message.get("message_id"),
                                             "text": f"{message.get('text', '')}\n{result}",
                                             "reply_markup": {"inline_keyboard": buttons} if buttons
                                             else None})

    def _confirmation_pressed(self, capture_id: int, verb: str) -> tuple[str, list[list[dict]] | None]:
        """A Yes / Change / Cancel / Undo button on a confirmation question (#117). The claim in
        `captures.confirm` is what makes a double tap harmless, so no state is kept here."""
        target = self.store.get_capture(capture_id)
        if target is None or target.address != self.chat_id:
            return "Unknown capture", None
        if verb == "change":  # cancel, and invite the corrected wording
            outcome = confirmations.cancel(self.store, capture_id, via=CHANNEL)
            if outcome == confirmations.Outcome.CANCELLED:
                return "Cancelled — send it again with the wording or time corrected.", None
            return confirmations.worded(outcome, target, [], 0), None
        if verb not in ("yes", "no", "undo"):
            return "Unknown action", None
        outcome, items, removed = confirmations.answer(self.store, target, verb, via=CHANNEL,
                                                       requestable=self.requestable)
        if outcome == confirmations.Outcome.APPLIED:  # ALREADY echoes items someone else saved
            for _ in items:
                METRICS.inc("maestro_captures_total", source="telegram")
        hint = "" if outcome != confirmations.Outcome.APPLIED else "\nWrong? Undo within 10 min."
        buttons = _undo_button(capture_id) if outcome == confirmations.Outcome.APPLIED else None
        return confirmations.worded(outcome, target, items, removed, hint), buttons
