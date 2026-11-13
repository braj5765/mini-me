"""Settings. All secrets come from environment variables (Fly secrets in prod)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+pysqlite:///./local.db"
    app_secret: str = "change-me-local-only"
    pseudonym_key: str = "change-me-local-only"
    login_password_hash: str = ""
    app_env: str = "local"  # local | prod
    app_version: str = "0.1.0"
    session_hours: int = 24 * 14

    llm_provider: str = ""
    llm_model: str = ""
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    openai_compat_api_key: str = ""
    openai_compat_base_url: str = ""
    # Optional: sent as "reasoning_effort" to openai_compat endpoints (e.g. "low" for Groq's gpt-oss models).
    openai_compat_reasoning_effort: str = ""

    # Drafting (D8a). "auto" = the real drafter when LLM_PROVIDER and LLM_MODEL are set, else the stub.
    drafter: str = "auto"                  # auto | llm | stub
    # Reply cap for the drafter's model call. Reasoning models ("thinking") spend part of it before they
    # answer, so keep it generous; models that do not think simply stop early.
    drafter_max_tokens: int = 1500
    # v0.1 only (decision given 7 Nov 2026): the drafter declares the incoming text to be own / made-up, so
    # an unverified provider may be used. Off by default. Does NOT touch the guard in app/llm/base.py.
    # Remove before real third-party messages (D3b / M2).
    own_data_only_mode: bool = False

    # Edit classifier (D9a). Blank provider / model = use LLM_PROVIDER / LLM_MODEL (same model as the drafter).
    classifier_provider: str = ""
    classifier_model: str = ""
    # Reply cap for the classifier's call; reasoning models spend part of it thinking, so keep headroom.
    classifier_max_tokens: int = 1500
    # Send Braj a one-line "Edit read as ..." note after a capture (set false to keep the chat quiet).
    classifier_notify: bool = True

    # Telegram bot (D4a). Bot disabled until the token is set. All build-level placeholders.
    telegram_bot_token: str = ""           # Fly secret
    telegram_webhook_secret: str = ""      # Fly secret; Telegram echoes it in a header on every call
    telegram_allowed_user_id: str = ""     # Braj's numeric Telegram id; empty = reject everyone
    # Public address of this app (5.22). On Render, RENDER_EXTERNAL_URL is set automatically; PUBLIC_BASE_URL wins if set.
    # telegram_public_base_url is the older name, kept as a last fallback. No default hostname any more.
    public_base_url: str = ""
    render_external_url: str = ""
    telegram_public_base_url: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    telegram_blind_ratio: float = 0.2      # ~1 in 5 (5.15 pt 3) [adjustable after testing]
    telegram_capture_window_hours: float = 24.0  # no capture in this window -> outcome unknown [placeholder]
    telegram_sweep_minutes: float = 10.0   # background check for expired capture windows

    # Outside scheduler (5.22): POST /tasks/sweep with header X-Sweep-Secret. Unset = endpoint answers 503.
    sweep_secret: str = ""

    # Share-to-draft (Dev 6, decisions 5.20 / 5.21). Build-level placeholders.
    share_default_domain: str = "club"      # domain for shared drafts (5.18; group queries = club)
    share_max_chars: int = 4000             # longest shared text accepted
    # Your own always-ask topics (5.12 pt 2), comma-separated, e.g. "fees,attendance waiver". Interim keyword match
    # on the masked text until the autonomy engine (D22) matches by meaning. Only ever adds caution.
    share_always_ask_topics: str = ""
    # Confidence floor (5.10 pt 2). Value deferred to post-testing (Q1): unset = floor off. Wired, not decided.
    share_confidence_floor: float | None = None

    # Dev 7 (decisions 5.21, 5.23). Build-level placeholders.
    # Test-mode guard (5.23 pt 3): until D3b / M2 the shared incoming text is masked, used for the draft, then NOT
    # stored, and every draft says "Test mode". Removed at M2 (set TEST_MODE=false only then).
    test_mode: bool = True
    web_blind_ratio: float = 0.1            # ~1 in 10 on web share (5.21 pt 4) [adjustable after testing]
    restore_cache_minutes: float = 30.0     # in-memory only: how long a draft screen can be re-opened with real values
    checkin_time: str = "20:00"             # daily check-in (5.12 pt 2), local time; the Telegram nudge goes out then
    checkin_timezone: str = "Asia/Kolkata"
    nudge_window_hours: float = 3.0         # the nudge is sent only within this long after the check-in time

    @property
    def base_url(self) -> str:
        """Where Telegram should send webhooks: PUBLIC_BASE_URL, else Render's own URL, else the old setting."""
        return (self.public_base_url or self.render_external_url or self.telegram_public_base_url).rstrip("/")

    @property
    def sqlalchemy_url(self) -> str:
        """Neon gives postgresql://; SQLAlchemy needs the psycopg3 driver name."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    def require_prod_secrets(self) -> None:
        if self.app_env == "prod":
            bad = [
                n for n, v in [("APP_SECRET", self.app_secret), ("PSEUDONYM_KEY", self.pseudonym_key)]
                if v.startswith("change-me")
            ]
            if bad or not self.login_password_hash:
                raise RuntimeError(
                    "Missing/placeholder secrets in prod: "
                    + ", ".join(bad + ([] if self.login_password_hash else ["LOGIN_PASSWORD_HASH"]))
                )


@lru_cache
def get_settings() -> Settings:
    return Settings()
