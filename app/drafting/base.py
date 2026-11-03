"""Drafter interface (D4a ships the interface and a stub; D8a ships the real drafter).

Contract for D8a:
- Input is ALWAYS masked text. The bot masks before calling; a drafter must never be handed raw text.
- Output carries everything the draft screen and the event log need: text, confidence (0.00-1.00),
  a one-line rationale, and the self-model items used (tagged Voice / Decision / Fact).
- A drafter that calls a model must go through `app.llm.get_llm()` and pass
  `contains_third_party_text=True`, so the no-training guard applies.
"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class DraftRequest:
    message_id: str
    masked_text: str            # already passed through mask_text()
    channel: str                # telegram | email | web
    domain: str                 # e.g. group_queries
    contact_type: str           # new_individual | new_group | known_individual | known_group
    contact_pseudonym: str      # never a real name or number


@dataclass(frozen=True)
class UsedItem:
    layer: str                  # voice | decision | fact
    item_id: str
    label: str = ""


@dataclass(frozen=True)
class DraftResult:
    text: str
    confidence: float           # 0.00 - 1.00, honest (never false certainty)
    rationale: str              # ONE line; detail on tap comes with D8b
    items_used: list[UsedItem] = field(default_factory=list)
    drafter: str = "unknown"    # name, e.g. "stub"
    drafter_version: str = "0"
    is_stub: bool = False       # the bot labels stub drafts clearly in the DM


class Drafter(Protocol):
    def draft(self, req: DraftRequest) -> DraftResult: ...
