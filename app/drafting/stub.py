"""STUB drafter. Placeholder until D8a (real drafting) lands. Not your twin.

Makes no model call and sends nothing to any provider (v0.1 rule: LLM guard stays on). It fills a fixed
template, so the whole capture -> classify -> log loop can be exercised end to end.
"""
from app.drafting.base import DraftRequest, DraftResult, snippet

STUB_NAME = "stub"
STUB_VERSION = "stub-0.1"
_SNIPPET_CHARS = 60


class StubDrafter:
    def draft(self, req: DraftRequest) -> DraftResult:
        text = f'Thanks for your message ("{snippet(req.masked_text, _SNIPPET_CHARS)}"). I\'ll get back to you shortly.'
        return DraftResult(
            text=text,
            confidence=0.5,
            rationale="Stub drafter: fixed template, no model and no self-model used.",
            items_used=[],
            drafter=STUB_NAME,
            drafter_version=STUB_VERSION,
            is_stub=True,
        )
