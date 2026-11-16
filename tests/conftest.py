import pytest
from sqlalchemy import create_engine

from app import db
from app.config import Settings, get_settings
from app.events import EventWriter


_ENV_THAT_CHANGES_BEHAVIOUR = (
    "DATABASE_URL", "LLM_PROVIDER", "LLM_MODEL", "OWN_DATA_ONLY_MODE", "DRAFTER", "DRAFTER_MAX_TOKENS",
    "CLASSIFIER_PROVIDER", "CLASSIFIER_MODEL", "CLASSIFIER_MAX_TOKENS", "CLASSIFIER_NOTIFY",
    "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
    "OPENAI_COMPAT_REASONING_EFFORT", "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_ALLOWED_USER_ID", "APP_ENV", "LOGIN_PASSWORD_HASH",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # Tests must not depend on the developer's real .env (Neon URL, API keys, own-data mode) or shell variables.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in _ENV_THAT_CHANGES_BEHAVIOUR:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PSEUDONYM_KEY", "test-key-1")
    monkeypatch.setenv("APP_SECRET", "test-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite+pysqlite:///{tmp_path}/t.db", future=True)
    db.metadata.create_all(eng)
    return eng


@pytest.fixture
def writer(engine):
    return EventWriter(engine)
