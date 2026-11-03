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

    # Telegram bot (D4a). Bot disabled until the token is set. All build-level placeholders.
    telegram_bot_token: str = ""           # Fly secret
    telegram_webhook_secret: str = ""      # Fly secret; Telegram echoes it in a header on every call
    telegram_allowed_user_id: str = ""     # Braj's numeric Telegram id; empty = reject everyone
    telegram_public_base_url: str = "https://mini-me.fly.dev"
    telegram_api_base: str = "https://api.telegram.org"
    telegram_blind_ratio: float = 0.2      # ~1 in 5 (5.15 pt 3) [adjustable after testing]
    telegram_capture_window_hours: float = 24.0  # no capture in this window -> outcome unknown [placeholder]
    telegram_sweep_minutes: float = 10.0   # background check for expired capture windows

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
