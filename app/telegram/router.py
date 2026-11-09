"""HTTP side of the bot: Telegram webhook + a sweep endpoint. Both require the shared webhook secret.

Webhook (not polling): Fly scale-to-zero stops an idle machine, and an incoming webhook wakes it. Polling
would keep the machine running all day (cost). Telegram sends the secret in X-Telegram-Bot-Api-Secret-Token.
"""
import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app import db
from app.config import Settings, get_settings
from app.classifier.classifier import EditClassifier
from app.drafting.factory import build_drafter
from app.events import EventWriter
from app.results.event_view import make_events_provider
from app.results.stats import make_stats_provider
from app.telegram.bot import BotDeps, handle_update, sweep_expired
from app.telegram.client import TelegramClient

log = logging.getLogger("minime.telegram")
router = APIRouter(prefix="/telegram", tags=["telegram"])

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def get_bot_deps() -> BotDeps:
    """Overridden in tests (app.dependency_overrides). The drafter comes from build_drafter() (real drafter
    when LLM_PROVIDER / LLM_MODEL are set, else the stub). D9a passes on_captured; D11a passes stats_provider and
    events_provider."""
    s = get_settings()
    if not s.telegram_bot_token or not s.telegram_webhook_secret:
        raise HTTPException(status_code=503, detail="Telegram bot not configured")
    engine = db.get_engine()
    writer = EventWriter(engine)
    return BotDeps(
        settings=s, engine=engine, writer=writer,
        client=TelegramClient(s.telegram_bot_token, s.telegram_api_base),
        drafter=build_drafter(s, engine),
        on_captured=EditClassifier(engine, writer, settings=s),     # D9a
        stats_provider=make_stats_provider(),                        # D11a
        events_provider=make_events_provider(),                      # D11a (read-only event table)
    )


def _check_secret(settings: Settings, provided: str | None) -> None:
    if not settings.telegram_bot_token or not settings.telegram_webhook_secret:
        raise HTTPException(status_code=503, detail="Telegram bot not configured")
    if not provided or not hmac.compare_digest(provided, settings.telegram_webhook_secret):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/webhook")
async def webhook(request: Request,
                  secret: str | None = Header(default=None, alias=SECRET_HEADER),
                  deps: BotDeps = Depends(get_bot_deps)) -> dict:
    _check_secret(deps.settings, secret)
    update = await request.json()
    try:
        handle_update(update, deps)
    except Exception:  # noqa: BLE001  (answer 200 so Telegram doesn't retry a poison update forever)
        log.exception("update handling failed")
    return {"ok": True}


@router.post("/sweep")
def sweep(secret: str | None = Header(default=None, alias=SECRET_HEADER),
          deps: BotDeps = Depends(get_bot_deps)) -> dict:
    _check_secret(deps.settings, secret)
    return {"ok": True, "marked_unknown": sweep_expired(deps)}
