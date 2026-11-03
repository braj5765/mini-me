import pytest
from sqlalchemy import create_engine

from app import db
from app.config import get_settings
from app.events import EventWriter


@pytest.fixture(autouse=True)
def _env(monkeypatch):
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
