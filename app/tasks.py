"""Outside-scheduler endpoints (decision 5.22). The free Render instance sleeps, so an in-process timer can't be
trusted: a free scheduler (cron-job.org, or a GitHub Actions schedule) calls POST /tasks/sweep every ~10 minutes.
That runs the 24 h capture sweep and, as a side effect, keeps the instance awake."""
import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from app.config import get_settings
from app.share.nudge import maybe_send_nudge
from app.share.tags import purge_unused_tags
from app.telegram.bot import BotDeps, sweep_expired
from app.telegram.router import get_bot_deps

log = logging.getLogger("minime.tasks")
router = APIRouter(prefix="/tasks", tags=["tasks"])

SECRET_HEADER = "X-Sweep-Secret"


def require_sweep_secret(secret: str | None = Header(default=None, alias=SECRET_HEADER)) -> None:
    expected = get_settings().sweep_secret
    if not expected:
        raise HTTPException(status_code=503, detail="Sweep secret not configured")
    if not secret or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/sweep", dependencies=[Depends(require_sweep_secret)])
def sweep(deps: BotDeps = Depends(get_bot_deps)) -> dict:
    """Close capture windows that have passed with no sent text (`outcome_unknown`, never scored).
    Idempotent: each thread is claimed once, so repeat or overlapping calls change nothing.
    The secret is checked first, so a stranger learns nothing about the bot's configuration."""
    out = {"ok": True, "marked_unknown": sweep_expired(deps)}
    try:   # the check-in nudge rides on the same call (Dev 7); it never blocks the sweep
        if maybe_send_nudge(deps):
            out["nudge_sent"] = True
    except Exception:  # noqa: BLE001
        log.exception("nudge failed")
    try:   # sender tags unused for 30 days are deleted (5.24 pt 1); also never blocks the sweep
        n = purge_unused_tags(deps.engine, deps.settings, now=deps.now())
        if n:
            out["tags_deleted"] = n
    except Exception:  # noqa: BLE001
        log.exception("tag purge failed")
    return out
