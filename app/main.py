"""FastAPI entrypoint. D1a scope: health check + single-user login. No product screens."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from sqlalchemy import text

from app import auth, db
from app.config import get_settings
from app.events import EventName, EventWriter
from app.events.writer import make_key
from app.telegram import router as tg_router
from app.telegram import runtime as tg_runtime

log = logging.getLogger("minime")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.require_prod_secrets()
    db.init_db()
    sweeper = None
    if tg_runtime.telegram_enabled(settings):
        deps = tg_router.get_bot_deps()
        try:  # register the webhook once per cold start if it is not already set (never blocks startup)
            await asyncio.to_thread(tg_runtime.ensure_webhook, settings, deps.client)
        except Exception:  # noqa: BLE001
            log.exception("could not register the Telegram webhook")
        sweeper = asyncio.create_task(
            tg_runtime.sweep_loop(tg_router.get_bot_deps, settings.telegram_sweep_minutes))
    yield
    if sweeper:
        sweeper.cancel()


app = FastAPI(title="Mini Me", version=get_settings().app_version, lifespan=lifespan)
app.include_router(tg_router.router)


def current_user(request: Request) -> str:
    user = auth.read_session_token(request.cookies.get(auth.COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user


@app.get("/healthz")
def healthz() -> dict:
    with db.get_engine().connect() as conn:
        conn.execute(text("select 1"))
    return {"ok": True, "version": get_settings().app_version}


@app.post("/login")
def login(response: Response, request: Request, password: str = Form(...), device_type: str = Form("desktop")):
    stored = get_settings().login_password_hash
    if not stored or not auth.verify_password(password, stored):
        raise HTTPException(status_code=401, detail="Wrong password")
    token = auth.make_session_token("braj")
    response.set_cookie(auth.COOKIE, token, httponly=True, secure=get_settings().app_env == "prod",
                        samesite="lax", max_age=get_settings().session_hours * 3600)
    session_id = token[-12:]
    dt = device_type if device_type in ("mobile", "desktop") else "desktop"
    EventWriter().log_safe(
        EventName.session_started, idempotency_key=make_key("session_started", session_id),
        session_id=session_id, device_type=dt, payload={"entry_screen": "login"},
    )
    return {"ok": True}


@app.get("/me")
def me(user: str = Depends(current_user)) -> dict:
    return {"user": user}
