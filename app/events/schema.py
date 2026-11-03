"""FINAL event schema (D2a). Names and enums here are used unchanged by every later component.

Sources: decisions 5.7 (per-draft fields), 5.12 (topic / check-in events), 5.13 (outcome unknown,
contact type), 5.15 (shown vs blind); PRD section 6 (event list).
"""
from enum import Enum


class _S(str, Enum):
    def __str__(self) -> str:  # store the bare value
        return self.value


class Channel(_S):
    telegram = "telegram"
    email = "email"
    web = "web"


class DeviceType(_S):
    mobile = "mobile"
    desktop = "desktop"
    # NULL (None) for system-initiated events with no session, e.g. autonomous sends.


class ContactType(_S):
    """5.13 point 6: new vs known x individual vs group."""
    new_individual = "new_individual"
    new_group = "new_group"
    known_individual = "known_individual"
    known_group = "known_group"


class EditClass(_S):
    """5.4 edit classes. 'none' = sent as-is. 'Decision reversed' is a Signal, never an edit class (5.12)."""
    decision = "decision"
    fact = "fact"
    voice = "voice"
    cosmetic = "cosmetic"
    none = "none"


class Signal(_S):
    """5.12: after-the-fact signals. One message gets decision edit OR decision_reversed, never both."""
    decision_reversed = "decision_reversed"
    undo = "undo"
    flag_not_me = "flag_not_me"


class AutonomyDecision(_S):
    """5.2 sliding action + shadow (5.13)."""
    just_send = "just_send"
    send_undo = "send_undo"
    draft_notify = "draft_notify"
    ask_first = "ask_first"
    would_have_sent_shadow = "would_have_sent_shadow"


class ShownState(_S):
    """5.15: blind sample = draft held until the user has replied."""
    shown = "shown"
    blind = "blind"


class Outcome(_S):
    """What happened to the message. 'unknown' = no sent text captured: logged, excluded from all scores (5.13)."""
    waiting = "waiting"
    sent_as_is = "sent_as_is"
    edited_sent = "edited_sent"
    discarded = "discarded"
    not_me = "not_me"
    user_authored = "user_authored"  # ask-first reply with no draft: no score (5.13 point 5)
    unknown = "unknown"


class Mode(_S):
    shadow = "shadow"
    live = "live"


class HardLimit(_S):
    none = "none"
    money = "money"
    emotional = "emotional"
    new_contact = "new_contact"
    low_confidence = "low_confidence"
    custom_topic = "custom_topic"


class EventName(_S):
    # ingestion
    message_received = "message_received"
    # drafting
    draft_generated = "draft_generated"
    draft_shown = "draft_shown"
    draft_held_blind = "draft_held_blind"          # 5.15: blind sample, draft withheld
    draft_released_after_reply = "draft_released_after_reply"  # 5.15
    # autonomy
    autonomy_decision = "autonomy_decision"
    hard_limit_triggered = "hard_limit_triggered"
    topic_triggered = "topic_triggered"            # 5.12
    # user action / outcome
    draft_acted = "draft_acted"
    draft_sent = "draft_sent"
    draft_discarded = "draft_discarded"
    outcome_unknown = "outcome_unknown"            # 5.13 point 2
    edit_classified = "edit_classified"
    shadow_comparison = "shadow_comparison"
    undo_send = "undo_send"
    decision_reversed = "decision_reversed"
    not_me_tapped = "not_me_tapped"
    # digest
    digest_sent = "digest_sent"
    digest_opened = "digest_opened"
    digest_flag_not_me = "digest_flag_not_me"
    # onboarding / learning
    calibration_sample_answered = "calibration_sample_answered"
    calibration_me_not_me = "calibration_me_not_me"
    ask_first_question_asked = "ask_first_question_asked"
    ask_first_question_answered = "ask_first_question_answered"
    selfmodel_item_edited = "selfmodel_item_edited"
    selfmodel_version_created = "selfmodel_version_created"
    selfmodel_version_undone = "selfmodel_version_undone"
    suggestion_proposed = "suggestion_proposed"
    suggestion_approved = "suggestion_approved"
    suggestion_set_aside = "suggestion_set_aside"
    suggestion_restored = "suggestion_restored"
    # scoring / trust
    fidelity_snapshot = "fidelity_snapshot"
    milestone_reached = "milestone_reached"
    # settings (5.12)
    risk_appetite_changed = "risk_appetite_changed"
    topic_added = "topic_added"
    topic_removed = "topic_removed"
    checkin_time_changed = "checkin_time_changed"
    # integrations / session / admin
    integration_connected = "integration_connected"
    integration_disconnected = "integration_disconnected"
    session_started = "session_started"
    csv_exported = "csv_exported"
    note_added = "note_added"
    # privacy controls (5.14 point 7)
    data_exported = "data_exported"
    data_deleted = "data_deleted"


# Columns that can appear on every event (common fields) -> typed columns in `events`.
COMMON_FIELDS = (
    "user_id", "session_id", "device_type", "channel", "domain", "contact_type",
    "app_version", "self_model_version", "message_id", "mode", "classifier_version",
)

# Per-draft record fields (5.7) that the `message_records` projection may hold.
MESSAGE_RECORD_FIELDS = (
    "channel", "domain", "contact_type", "contact_pseudonym", "draft_text", "confidence",
    "sent_text", "edit_class", "signal", "autonomy_decision", "shown_state", "outcome",
    "time_to_action_ms", "device_type", "classifier_version", "mode", "rationale",
    "hard_limit", "topic_triggered",
)

ENUM_FIELDS = {
    "device_type": DeviceType, "channel": Channel, "contact_type": ContactType,
    "edit_class": EditClass, "signal": Signal, "autonomy_decision": AutonomyDecision,
    "shown_state": ShownState, "outcome": Outcome, "mode": Mode, "hard_limit": HardLimit,
}
