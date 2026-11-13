"""HTTP front door for share-to-draft (Dev 6). Contract and examples: docs/mini-me-dev-share.md.

  POST /api/draft                      bearer token; body = the shared text; `?format=text` for just the draft
  POST /api/draft/{message_id}/copied  bearer token; call it AFTER the clipboard copy has happened

Auth runs first, before the body is read, so a wrong or missing token gets 401 and no text is stored (or logged).
The shared text is taken from the request BODY, never from the URL, so it stays out of access logs.
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.responses import Response

from app import db
from app.config import Settings, get_settings
from app.drafting.factory import build_drafter
from app.events import EventWriter
from app.share import service
from app.share.service import ShareError, ShareOutcome
from app.share.tokens import verify_token
from app.telegram import format as fmt

log = logging.getLogger("minime.share")
router = APIRouter(prefix="/api", tags=["share"])

MAX_BODY_BYTES = 64 * 1024
HDR_ID, HDR_ACTION, HDR_CONF = "X-Mini-Me-Message-Id", "X-Mini-Me-Action", "X-Mini-Me-Confidence"
HDR_TEST = "X-Mini-Me-Test-Mode"   # "on" while the test-mode guard is on (5.23 pt 3); the shortcut shows the label


def require_token(request: Request) -> dict:
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    row = verify_token(db.get_engine(), token.strip()) if scheme.lower() == "bearer" else None
    if row is None:
        raise HTTPException(status_code=401, detail="Missing or invalid token",
                            headers={"WWW-Authenticate": "Bearer"})
    return row


def get_share_deps() -> tuple:
    """(engine, writer, settings, drafter). Overridden in tests (app.dependency_overrides)."""
    s: Settings = get_settings()
    engine = db.get_engine()
    return engine, EventWriter(engine), s, build_drafter(s, engine)


async def _read_input(request: Request) -> dict:
    """text / title / url / channel / format from a JSON body, a form, or a raw text body (text/plain).
    title, url, channel and format may also come as query parameters; the text itself never does."""
    q = request.query_params
    data = {k: q.get(k) for k in ("title", "url", "channel", "format")}
    data["text"] = None
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype == "application/json":
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Body is not valid JSON") from None
        if isinstance(body, dict):
            for k in ("text", "title", "url", "channel", "format"):
                if body.get(k) is not None:
                    data[k] = str(body[k])
        elif isinstance(body, str):
            data["text"] = body
    elif ctype in ("application/x-www-form-urlencoded", "multipart/form-data"):
        form = await request.form()
        for k in ("text", "title", "url", "channel", "format"):
            if form.get(k) is not None:
                data[k] = str(form[k])
    else:  # text/plain or no type: the whole body is the shared text
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
        data["text"] = raw.decode("utf-8", errors="replace")
    return data


def _wants_text(request: Request, fmt_param: str | None) -> bool:
    if fmt_param:
        return fmt_param.strip().lower() == "text"
    accept = (request.headers.get("accept") or "").lower()
    return "text/plain" in accept and "application/json" not in accept


def _ascii(value: str) -> str:
    return value.encode("ascii", "replace").decode("ascii")


def _text_response(out: ShareOutcome) -> Response:
    base = {HDR_ID: out.message_id, HDR_ACTION: out.action}
    if out.test_mode:
        base[HDR_TEST] = "on"
    if out.action == "copy_first":
        # Copy-first guard (5.23 pt 1): not 2xx, so the clipboard is never overwritten with this note.
        return PlainTextResponse(out.message or "", status_code=409, headers={HDR_ACTION: "copy_first", **({HDR_TEST: "on"} if out.test_mode else {})})
    if out.action == "draft":
        # Just the draft, ready to paste. Confidence is a header so a script can show it.
        return PlainTextResponse(out.draft or "", headers={**base, HDR_CONF: _ascii(out.confidence_label or "")})
    if out.action == "ask_first":
        # Not 2xx on purpose: a client that copies "success" bodies to the clipboard must not copy this.
        r = out.reason or {}
        return PlainTextResponse(f"{r.get('headline', '')}\n{r.get('detail', '')}".strip(), status_code=409, headers=base)
    return PlainTextResponse(fmt.draft_failed(out.error or "error"), status_code=502, headers=base)


def _json_response(out: ShareOutcome) -> Response:
    body = {
        "message_id": out.message_id, "action": out.action, "channel": out.channel, "domain": out.domain,
        "contact_type": service.CONTACT_TYPE, "masked_text": out.masked_text, "masked_count": out.masked_count,
        "confirm_within_hours": out.window_hours, "shown_state": out.shown_state,
        "test_mode": out.test_mode, "test_mode_label": service.TEST_MODE_LABEL if out.test_mode else None,
    }
    status = 200
    if out.action == "copy_first":
        body.update(draft=None, message=out.message)
    elif out.action == "draft" and out.shown_state == "blind":
        # Held (web share blind sample): no draft, confidence or why until the reply has been captured.
        body.update(action="held", draft=None)
    elif out.action == "draft":
        body.update(
            draft=out.draft, confidence_label=out.confidence_label, confidence=out.confidence, why=out.why,
            unrestored_tokens=out.unrestored,
            copied_path=f"/api/draft/{out.message_id}/copied")
    elif out.action == "ask_first":
        body.update(draft=None, reason=out.reason)
    else:
        status = 502
        body.update(draft=None, error=out.error, message=fmt.draft_failed(out.error or "error"))
    return JSONResponse(body, status_code=status,
                        headers={HDR_ID: out.message_id, HDR_ACTION: body["action"], **({HDR_TEST: "on"} if out.test_mode else {})})


@router.post("/draft")
async def post_draft(request: Request, _token: dict = Depends(require_token)) -> Response:
    data = await _read_input(request)          # only after the token has been checked
    channel = (data.get("channel") or "shortcut").strip().lower()
    if channel not in service.TOKEN_ROUTE_CHANNELS:
        raise HTTPException(status_code=422, detail="channel 'web_share' is reserved for the web app; use 'shortcut'.")
    engine, writer, settings, drafter = get_share_deps_dependency(request)
    try:
        out = await asyncio.to_thread(
            service.create_share_draft, engine, writer, settings, drafter,
            text=data.get("text") or "", title=data.get("title"), url=data.get("url"), channel=channel)
    except ShareError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from None
    return _text_response(out) if _wants_text(request, data.get("format")) else _json_response(out)


@router.post("/draft/{message_id}/copied")
async def post_copied(message_id: str, request: Request, copy_no: int | None = None,
                      _token: dict = Depends(require_token)) -> dict:
    engine, writer, _settings, _drafter = get_share_deps_dependency(request)
    try:
        return await asyncio.to_thread(service.log_copied, engine, writer, message_id, copy_no)
    except ShareError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from None


def get_share_deps_dependency(request: Request) -> tuple:
    """Resolve get_share_deps through app.dependency_overrides so tests can swap it (it runs only after auth)."""
    fn = request.app.dependency_overrides.get(get_share_deps, get_share_deps)
    return fn()
