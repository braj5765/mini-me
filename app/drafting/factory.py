"""Chooses the drafter for the bot (D8a). Used by app.telegram.router.get_bot_deps()."""
import logging

from sqlalchemy.engine import Engine

from app.config import Settings
from app.drafting.base import Drafter
from app.drafting.llm_drafter import LLMDrafter
from app.drafting.stub import StubDrafter
from app.llm.base import get_llm
from app.selfmodel.store import SelfModelStore

log = logging.getLogger("minime.drafter")


def build_drafter(settings: Settings, engine: Engine) -> Drafter:
    """DRAFTER=stub -> stub. DRAFTER=llm -> real (errors if LLM_PROVIDER / LLM_MODEL missing).
    DRAFTER=auto (default) -> real when both are set, otherwise the stub (labelled stub in the DM)."""
    mode = (settings.drafter or "auto").lower()
    if mode == "stub":
        return StubDrafter()
    configured = bool(settings.llm_provider and settings.llm_model)
    if mode == "auto" and not configured:
        log.warning("LLM_PROVIDER / LLM_MODEL not set: using the stub drafter")
        return StubDrafter()
    if settings.own_data_only_mode:
        log.warning("OWN_DATA_ONLY_MODE is on: drafts declare incoming text as own / made-up (v0.1 only)")
    return LLMDrafter(get_llm(), SelfModelStore(engine), settings=settings)
