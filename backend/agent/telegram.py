"""Telegram delivery for farmer warnings, plus the "Connect Telegram" listener.

Connecting: the app shows a link t.me/<bot>?start=<one-time code>. When the farmer
presses Start, Telegram delivers "/start <code>" to the bot; the listener below
(long polling, so it works on a laptop without a public URL) links that chat to the field.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from agent import store
from config.settings import settings

logger = logging.getLogger(__name__)

POLL_TIMEOUT_SECONDS = 25
# Short codes the farmer can also type by hand (no 0/O/1/I to avoid mix-ups); valid 30 minutes.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_PATTERN = re.compile(r"^[A-Z2-9]{6}$")


def new_connect_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
_bot_username: Optional[str] = None


def configured() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN)


def _url(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"


async def _call(method: str, payload: dict[str, Any], timeout: float = 15) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(_url(method), json=payload)
    data = response.json() if response.content else {}
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {data.get('error_code') or response.status_code}: {data.get('description') or response.text[:200]}")
    return data.get("result")


async def bot_username() -> Optional[str]:
    global _bot_username
    if _bot_username or not configured():
        return _bot_username
    try:
        _bot_username = (await _call("getMe", {})).get("username")
    except Exception as exc:
        logger.warning("Telegram getMe failed: %s", exc)
    return _bot_username


def escape(text: str) -> str:
    return html.escape(text, quote=False)


async def send(chat_id: Optional[str], text_html: str) -> dict[str, Any]:
    """Returns {status: sent|not_sent|failed, provider_message_id?, error?}. Never raises."""
    if not chat_id:
        return {"status": "not_sent", "error": "Telegram is not connected for this field."}
    if not configured():
        return {"status": "not_sent", "error": "Telegram bot token is missing; saved in AgriLens only."}
    try:
        result = await _call("sendMessage", {"chat_id": chat_id, "text": text_html, "parse_mode": "HTML", "disable_web_page_preview": True})
        return {"status": "sent", "provider_message_id": str(result.get("message_id"))}
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return {"status": "failed", "error": f"{str(exc)[:200]}"}


class TelegramListener:
    """Long-polls getUpdates and handles /start <code> (connect) and /stop (disconnect)."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._offset: Optional[int] = None
        self.last_poll_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.events: deque = deque(maxlen=10)

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._task and not self._task.done()),
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "recent_events": list(self.events),
        }

    def start(self) -> None:
        if not configured() or (self._task and not self._task.done()):
            return
        self._task = asyncio.create_task(self._run(), name="telegram-listener")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        await bot_username()
        while True:
            try:
                payload: dict[str, Any] = {"timeout": POLL_TIMEOUT_SECONDS, "allowed_updates": ["message"]}
                if self._offset is not None:
                    payload["offset"] = self._offset
                updates = await _call("getUpdates", payload, timeout=POLL_TIMEOUT_SECONDS + 10)
                self.last_poll_at = datetime.now(timezone.utc).isoformat()
                self.last_error = None
                for update in updates or []:
                    self._offset = update["update_id"] + 1
                    message = update.get("message") or {}
                    event = {"at": self.last_poll_at, "text": (message.get("text") or "")[:20], "result": None}
                    self.events.append(event)
                    try:
                        event["result"] = await self._handle(message)
                    except Exception as exc:
                        logger.exception("Telegram update handling failed")
                        event["result"] = f"error: {type(exc).__name__}: {str(exc)[:300]}"
                        chat_id = str((message.get("chat") or {}).get("id") or "")
                        if chat_id:
                            await send(chat_id, "Sorry, something went wrong. Please tap Connect Telegram in AgriLens and try again.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 409 means another process is polling this bot (e.g. a second server); back off quietly.
                self.last_error = str(exc)[:300]
                logger.warning("Telegram polling error: %s", exc)
                await asyncio.sleep(10)

    async def _handle(self, message: dict[str, Any]) -> str:
        chat_id = str((message.get("chat") or {}).get("id") or "")
        text = (message.get("text") or "").strip()
        if not chat_id:
            return "ignored"
        if CODE_PATTERN.match(text.upper()):
            text = f"/start {text.upper()}"  # code typed by hand instead of using the link
        if not text.startswith("/"):
            await send(chat_id, "To connect, open AgriLens, tap <b>Connect Telegram</b>, then send me the 6-letter code shown there.")
            return "plain text"
        first_name = (message.get("from") or {}).get("first_name") or "there"
        command, _, argument = text.partition(" ")
        command = command.split("@")[0].lower()

        if command == "/start" and argument.strip():
            field = await store.connect_telegram(argument.strip().upper(), chat_id, first_name)
            outcome = f"connected {field['field_id']}" if field else "unknown or expired code"
            if field:
                reply = (
                    f"✅ Connected, {escape(first_name)}!\n"
                    f"You will get AgriLens alerts for <b>{escape(field.get('farm_name') or field['field_id'])}</b> here."
                )
            else:
                reply = "That code didn't work or has expired. In AgriLens, tap <b>Connect Telegram</b> again and send me the new code."
        elif command == "/start":
            outcome = "start without code"
            reply = "👋 Hi! To get alerts, open AgriLens, tap <b>Connect Telegram</b> in the agent settings, then send me the 6-letter code shown there."
        elif command == "/stop":
            count = await store.disconnect_telegram_chat(chat_id)
            outcome = f"stopped {count}"
            reply = "🔕 Alerts stopped." if count else "You were not receiving any alerts."
        else:
            outcome = "other command"
            reply = "I only send AgriLens field alerts. Use /stop to stop them."
        delivery = await send(chat_id, reply)
        return outcome + ("" if delivery["status"] == "sent" else f"; reply failed: {delivery.get('error')}")
