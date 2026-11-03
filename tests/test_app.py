import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from app import auth, db
from app.config import get_settings
from app.llm.base import LLMError, Message, get_llm


def test_password_hash_roundtrip():
    h = auth.hash_password("a-long-test-password")
    assert auth.verify_password("a-long-test-password", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", "garbage")


@pytest.fixture
def client(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{tmp_path}/app.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LOGIN_PASSWORD_HASH", auth.hash_password("a-long-test-password"))
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    from app.main import app
    with TestClient(app) as c:
        yield c
    db.get_engine.cache_clear()


def test_health_login_and_session_event(client):
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/me").status_code == 401
    assert client.post("/login", data={"password": "nope"}).status_code == 401
    r = client.post("/login", data={"password": "a-long-test-password", "device_type": "mobile"})
    assert r.status_code == 200
    assert client.get("/me").json() == {"user": "braj"}
    with db.get_engine().connect() as c:
        row = c.execute(select(db.events)).mappings().one()
    assert row["event_name"] == "session_started" and row["device_type"] == "mobile"


def test_neon_url_is_rewritten(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db?sslmode=require")
    get_settings.cache_clear()
    assert get_settings().sqlalchemy_url.startswith("postgresql+psycopg://")


def test_prod_refuses_placeholder_secrets(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("APP_SECRET", "change-me-local-only")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        get_settings().require_prod_secrets()


def test_llm_not_configured_is_a_clear_error():
    with pytest.raises(LLMError):
        get_llm()


def test_llm_guard_blocks_third_party_text_on_unverified_provider(monkeypatch):
    client = get_llm("gemini", "gemini-2.0-flash")
    with pytest.raises(LLMError, match="no-training"):
        client.complete([Message("user", "hi")], contains_third_party_text=True)


def test_llm_unknown_provider():
    with pytest.raises(LLMError):
        get_llm("nope", "m")
