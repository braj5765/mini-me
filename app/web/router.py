"""The installable web app (Dev 7; decisions 5.11, 5.20 pt 1 C, 5.21, 5.23).

Pages (public, no data): the app shell, manifest, service worker, static files.
  POST /share-target   Android share target (POST, not GET, so the text never sits in a URL or access log). It
                       returns the app shell with the shared text carried IN THE PAGE ONLY (in memory in the
                       browser): nothing is stored or logged here, and nothing is shown before login.
API (cookie login, JSON; the existing single-user login at POST /login):
  /app/api/session, /drafts, /items/{id}, /items/{id}/copied|tag|reply|reveal|not-replying, /tags, /pending, /confirm
Every route that reads or writes data needs the signed-in cookie (401 otherwise).
"""
import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from app import auth, db
from app.classifier.classifier import EditClassifier
from app.config import Settings, get_settings
from app.drafting.factory import build_drafter
from app.events import EventName, EventWriter
from app.events.writer import make_key
from app.share import cache, capture, pending, service, tags
from app.share.service import ShareError
from app.telegram import format as fmt

log = logging.getLogger("minime.web")
STATIC = Path(__file__).parent / "static"
router = APIRouter(tags=["web"])

MAX_SHARE_BYTES = 64 * 1024
SHELL_HEADERS = {"Cache-Control": "no-cache"}


# --------------------------------------------------------------------------- auth + deps
def web_user(request: Request) -> str:
    user = auth.read_session_token(request.cookies.get(auth.COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user


def get_web_deps() -> tuple:
    """(engine, writer, settings, drafter, classifier hook). Overridden in tests (app.dependency_overrides)."""
    s: Settings = get_settings()
    engine = db.get_engine()
    writer = EventWriter(engine)
    return engine, writer, s, build_drafter(s, engine), EditClassifier(engine, writer, settings=s)


def _deps(request: Request) -> tuple:
    fn = request.app.dependency_overrides.get(get_web_deps, get_web_deps)
    return fn()


def _fail(e: ShareError):
    raise HTTPException(status_code=e.status, detail=str(e)) from None


# --------------------------------------------------------------------------- pages
def _shell(boot: dict | None) -> HTMLResponse:
    html = (STATIC / "app.html").read_text(encoding="utf-8")
    payload = json.dumps(boot).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return HTMLResponse(html.replace("__BOOT__", payload), headers=SHELL_HEADERS)


@router.get("/app", include_in_schema=False)
@router.get("/app/", include_in_schema=False)
def app_home() -> HTMLResponse:
    return _shell(None)


@router.get("/app/static/{name:path}", include_in_schema=False)
def app_static(name: str) -> FileResponse:
    path = (STATIC / name).resolve()
    if STATIC.resolve() not in path.parents or not path.is_file() or path.name == "app.html":
        raise HTTPException(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json",
                        headers=SHELL_HEADERS)


@router.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@router.post("/share-target", include_in_schema=False)
async def share_target(request: Request) -> HTMLResponse:
    """The share sheet posts here. The text is NOT stored, logged or processed here: it is put into the returned
    page (in memory in the browser), which drafts it after login. No part of it is shown before login."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_SHARE_BYTES:
        raise HTTPException(status_code=413, detail="Too large")
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    shared: dict[str, str] = {}
    if ctype in ("application/x-www-form-urlencoded", "multipart/form-data"):
        form = await request.form()
        shared = {k: str(form.get(k) or "")[:8000] for k in ("title", "text", "url")}
    r = _shell({"shared": shared} if any(shared.values()) else None)
    r.headers["Cache-Control"] = "no-store"
    return r


# --------------------------------------------------------------------------- view helpers
def _view(out: service.ShareOutcome, settings: Settings) -> dict:
    action = "held" if (out.action == "draft" and out.shown_state == "blind") else out.action
    v = {"message_id": out.message_id or None, "action": action, "masked_text": out.masked_text,
         "masked_count": out.masked_count, "test_mode": out.test_mode,
         "test_mode_label": service.TEST_MODE_LABEL if out.test_mode else None,
         "confirm_within_hours": out.window_hours}
    if action == "draft":
        v.update(draft=out.draft, confidence_label=out.confidence_label, why=out.why, unrestored_tokens=out.unrestored)
    elif action == "ask_first":
        v.update(reason=out.reason)
    elif action == "failed":
        v.update(error=out.error, message=fmt.draft_failed(out.error or "error"))
    elif action == "copy_first":
        v.update(message=out.message)
    return v


# --------------------------------------------------------------------------- API
@router.get("/app/api/session")
def session(request: Request) -> dict:
    s = get_settings()
    user = auth.read_session_token(request.cookies.get(auth.COOKIE))
    return {"signed_in": bool(user), "test_mode": s.test_mode,
            "test_mode_label": service.TEST_MODE_LABEL if s.test_mode else None}


@router.post("/app/api/session")
async def session_started(request: Request, _u: str = Depends(web_user)) -> dict:
    """One `session_started` per app open (entry_screen: home | share | paste), device mobile."""
    body = await _json(request)
    engine, writer, *_ = _deps(request)
    sid = (request.cookies.get(auth.COOKIE) or "")[-12:]
    entry = str(body.get("entry") or "home")[:16]
    writer.log_safe(EventName.session_started, idempotency_key=make_key("session_started", sid, str(body.get("nonce") or "")),
                    session_id=sid, device_type="mobile", payload={"entry_screen": entry})
    return {"ok": True}


async def _json(request: Request) -> dict:
    if (request.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Send JSON")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Body is not valid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")
    return body


@router.post("/app/api/drafts")
async def create_draft(request: Request, _u: str = Depends(web_user)) -> Response:
    body = await _json(request)
    entry = str(body.get("entry") or "paste")
    if entry not in ("share", "paste"):
        raise HTTPException(status_code=422, detail="entry must be share or paste")
    engine, writer, settings, drafter, _hook = _deps(request)
    try:
        out = await asyncio.to_thread(
            service.create_share_draft, engine, writer, settings, drafter,
            text=str(body.get("text") or ""), title=body.get("title"), url=body.get("url"), channel="web_share",
            origin="shared" if entry == "share" else "pasted", entry=entry,
            retry_of=(str(body["retry_of"])[:40] if body.get("retry_of") else None))
    except ShareError as e:
        _fail(e)
    if out.action == "draft" and out.shown_state != "blind":
        cache.put(out.message_id, out.draft or "", out.unrestored, settings.restore_cache_minutes)
    return JSONResponse(_view(out, settings), status_code=502 if out.action == "failed" else 200)


@router.get("/app/api/items/{message_id}")
def get_item(message_id: str, request: Request, _u: str = Depends(web_user)) -> dict:
    """Re-open a draft screen. Real values come only from the short in-memory cache; otherwise the masked draft."""
    engine, writer, settings, *_ = _deps(request)
    rec = writer.get_message(message_id)
    if not rec or rec.get("channel") not in service.SHARE_CHANNELS:
        raise HTTPException(status_code=404, detail="No such item")
    held = rec.get("shown_state") == "blind" and rec.get("outcome") not in ("sent_as_is", "edited_sent")
    v = {"message_id": message_id, "outcome": rec.get("outcome"), "test_mode": settings.test_mode,
         "test_mode_label": service.TEST_MODE_LABEL if settings.test_mode else None,
         "masked_text": rec.get("incoming_text"), "contact_type": rec.get("contact_type")}
    if held:
        v["action"] = "held"
    elif not rec.get("draft_text"):
        v["action"] = "ask_first" if rec.get("autonomy_decision") == "ask_first" else "failed"
    else:
        hit = cache.get(message_id)
        v.update(action="draft", draft=hit[0] if hit else rec["draft_text"], unrestored_tokens=hit[1] if hit else [],
                 confidence_label=fmt.confidence_label(rec.get("confidence") or 0), why=rec.get("rationale"))
    return v


@router.post("/app/api/items/{message_id}/copied")
async def copied(message_id: str, request: Request, _u: str = Depends(web_user)) -> dict:
    """Call AFTER the clipboard write succeeded (5.20 pt 3). Logs `draft_copied`; copy is not sent."""
    body = await _json(request) if request.headers.get("content-length") not in (None, "0") else {}
    engine, writer, *_ = _deps(request)
    copy_no = body.get("copy_no")
    try:
        return await asyncio.to_thread(service.log_copied, engine, writer, message_id,
                                       int(copy_no) if copy_no is not None else None)
    except ShareError as e:
        _fail(e)


@router.get("/app/api/tags")
def get_tags(request: Request, _u: str = Depends(web_user)) -> dict:
    engine, *_ = _deps(request)
    return {"tags": tags.list_tags(engine)}


@router.post("/app/api/items/{message_id}/tag")
async def tag_item(message_id: str, request: Request, _u: str = Depends(web_user)) -> dict:
    body = await _json(request)
    engine, writer, settings, *_ = _deps(request)
    try:
        return await asyncio.to_thread(
            tags.apply_tag, engine, writer, settings, message_id, tag_id=body.get("tag_id"),
            label=body.get("label"), kind=body.get("kind"), someone_new=bool(body.get("someone_new")),
            clear=bool(body.get("clear")))
    except ShareError as e:
        _fail(e)


@router.get("/app/api/pending")
def get_pending(request: Request, _u: str = Depends(web_user)) -> dict:
    engine, _w, settings, *_ = _deps(request)
    return pending.list_pending(engine, settings)


@router.post("/app/api/confirm")
async def confirm(request: Request, _u: str = Depends(web_user)) -> dict:
    """Commit one or more list outcomes (the list holds them ~10 s so Undo can happen before anything is logged,
    design C-7). Each result is independent."""
    body = await _json(request)
    engine, writer, settings, _d, hook = _deps(request)
    results = []
    for it in (body.get("items") or [])[:20]:
        mid = str(it.get("message_id") or "")
        try:
            r = await asyncio.to_thread(capture.confirm, engine, writer, settings, mid, str(it.get("action") or ""),
                                        it.get("text"), hook=hook)
            results.append({"message_id": mid, **r})
        except ShareError as e:
            results.append({"message_id": mid, "ok": False, "status": e.status, "error": str(e)})
    return {"results": results}


@router.post("/app/api/items/{message_id}/not-replying")
async def not_replying(message_id: str, request: Request, _u: str = Depends(web_user)) -> dict:
    engine, writer, settings, _d, hook = _deps(request)
    try:
        return await asyncio.to_thread(capture.confirm, engine, writer, settings, message_id, "not_replying",
                                       None, hook=hook)
    except ShareError as e:
        _fail(e)


@router.post("/app/api/items/{message_id}/reply")
async def reply(message_id: str, request: Request, _u: str = Depends(web_user)) -> dict:
    """The user's own reply: for a held (blind) draft, or, optionally, for an ask-first item (user_authored)."""
    body = await _json(request)
    engine, writer, settings, _d, hook = _deps(request)
    rec = writer.get_message(message_id) or {}
    text, cap = str(body.get("text") or ""), str(body.get("capture") or "paste")
    try:
        if rec.get("shown_state") == "blind":
            return await asyncio.to_thread(capture.capture_held_reply, engine, writer, settings, message_id, text,
                                           cap, hook=hook)
        return await asyncio.to_thread(capture.reply_ask_first, engine, writer, settings, message_id, text, cap)
    except ShareError as e:
        _fail(e)


@router.post("/app/api/items/{message_id}/reveal")
def reveal(message_id: str, request: Request, _u: str = Depends(web_user)) -> dict:
    engine, writer, *_ = _deps(request)
    try:
        return capture.reveal_held(engine, writer, message_id)
    except ShareError as e:
        _fail(e)
