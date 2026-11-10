"""Startup helpers: register the webhook, and a background loop that closes expired capture windows."""
import asyncio
import logging

from app.config import Settings
from app.telegram.bot import sweep_expired
from app.telegram.client import TelegramAPI

log = logging.getLogger("minime.telegram")


def webhook_url(settings: Settings) -> str:
    return settings.base_url + "/telegram/webhook"


def ensure_webhook(settings: Settings, client: TelegramAPI) -> bool:
    """Point Telegram at this app if it isn't already. Idempotent. Returns True if it changed anything."""
    if not settings.base_url:
        log.warning("PUBLIC_BASE_URL is not set (and not on Render): Telegram webhook not registered")
        return False
    want = webhook_url(settings)
    if client.get_webhook_url() == want:
        return False
    client.set_webhook(want, settings.telegram_webhook_secret)
    return True


def telegram_enabled(settings: Settings) -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_webhook_secret)


async def sweep_loop(make_deps, minutes: float) -> None:
    """Extra, best-effort: while the app is awake, close expired windows every few minutes. Nothing depends on it:
    the real trigger is the outside scheduler calling POST /tasks/sweep (5.22), plus every incoming update."""
    while True:
        await asyncio.sleep(max(1.0, minutes * 60))
        try:
            await asyncio.to_thread(lambda: sweep_expired(make_deps()))
        except Exception:  # noqa: BLE001
            log.exception("background sweep failed")
