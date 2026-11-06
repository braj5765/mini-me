"""Thin Telegram Bot API client over httpx (sync). No third-party bot library.

The bot token is part of every request URL, so:
- httpx's own request logging is silenced (it prints full URLs at INFO level);
- errors are re-raised WITHOUT the URL (TelegramError carries method + status + Telegram's description only).
"""
import logging
from typing import Any, Protocol

import httpx

# httpx logs "HTTP Request: POST https://api.telegram.org/bot<TOKEN>/..." at INFO. Never allow that.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class TelegramError(RuntimeError):
    """Sanitised: never contains the bot token or the request URL."""


class TelegramAPI(Protocol):
    """What the bot needs. Tests supply a fake with the same methods."""

    def send_message(self, chat_id: int | str, text: str, *, reply_markup: dict | None = None,
                     reply_to_message_id: int | None = None) -> int: ...
    def answer_callback(self, callback_query_id: str, text: str = "") -> None: ...
    def clear_buttons(self, chat_id: int | str, message_id: int) -> None: ...
    def edit_message_text(self, chat_id: int | str, message_id: int, text: str) -> None: ...
    def get_webhook_url(self) -> str: ...
    def set_webhook(self, url: str, secret_token: str) -> None: ...


class TelegramClient:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org", timeout: float = 10.0):
        if not token:
            raise TelegramError("Telegram bot token is not configured")
        self._token = token
        self._base = api_base.rstrip("/")
        self._http = httpx.Client(timeout=timeout)

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        try:
            r = self._http.post(url, json=payload or {})
        except httpx.HTTPError as exc:  # message may embed the URL: drop it
            raise TelegramError(f"{method}: network error ({type(exc).__name__})") from None
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(f"{method}: non-JSON response (HTTP {r.status_code})") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: HTTP {r.status_code}: {data.get('description', 'error')}")
        return data["result"]

    def send_message(self, chat_id, text, *, reply_markup=None, reply_to_message_id=None) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id:
            payload["reply_parameters"] = {"message_id": reply_to_message_id,
                                           "allow_sending_without_reply": True}
        return int(self._call("sendMessage", payload)["message_id"])

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    def clear_buttons(self, chat_id, message_id) -> None:
        self._call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                              "reply_markup": {"inline_keyboard": []}})

    def edit_message_text(self, chat_id, message_id, text) -> None:
        """Replace the text and drop the buttons in one call."""
        self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text,
                                       "disable_web_page_preview": True,
                                       "reply_markup": {"inline_keyboard": []}})

    def get_webhook_url(self) -> str:
        return self._call("getWebhookInfo").get("url", "")

    def set_webhook(self, url: str, secret_token: str) -> None:
        self._call("setWebhook", {"url": url, "secret_token": secret_token,
                                  "allowed_updates": ["message", "callback_query"]})
