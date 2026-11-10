"""Hosting move to Render (decision 5.22): protected /tasks/sweep, PUBLIC_BASE_URL, $PORT."""
import re
from pathlib import Path

import pytest

from app.config import Settings, get_settings
from app.telegram.runtime import ensure_webhook, webhook_url
from test_telegram import api, env, INCOMING, FWD_USER, _msg  # noqa: F401  (fixtures + helpers)

H = {"X-Sweep-Secret": "sweep-s3cret"}


@pytest.fixture
def swept(api, monkeypatch):
    monkeypatch.setenv("SWEEP_SECRET", "sweep-s3cret")
    get_settings.cache_clear()
    api.deps.settings = get_settings()
    return api


def _open_item(api):
    api.post("/telegram/webhook", json=_msg(INCOMING, forward=FWD_USER),
             headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"})


def test_sweep_needs_the_secret(swept):
    assert swept.post("/tasks/sweep").status_code == 403
    assert swept.post("/tasks/sweep", headers={"X-Sweep-Secret": "wrong"}).status_code == 403
    # the Telegram webhook secret is not accepted here, and the other way round
    assert swept.post("/tasks/sweep", headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"}).status_code == 403
    assert swept.post("/telegram/sweep", headers=H).status_code == 403


def test_sweep_is_off_until_the_secret_is_set(api, monkeypatch):
    monkeypatch.delenv("SWEEP_SECRET", raising=False)
    get_settings.cache_clear()
    assert api.post("/tasks/sweep", headers=H).status_code == 503


def test_sweep_closes_expired_windows_once(swept):
    _open_item(swept)
    assert swept.post("/tasks/sweep", headers=H).json() == {"ok": True, "marked_unknown": 0}   # still inside 24 h
    swept.deps.clock.advance(hours=25)
    assert swept.post("/tasks/sweep", headers=H).json() == {"ok": True, "marked_unknown": 1}
    assert swept.post("/tasks/sweep", headers=H).json() == {"ok": True, "marked_unknown": 0}   # idempotent
    assert swept.post("/tasks/sweep", headers=H).json() == {"ok": True, "marked_unknown": 0}


def test_wrong_secret_changes_nothing(swept):
    _open_item(swept)
    swept.deps.clock.advance(hours=25)
    assert swept.post("/tasks/sweep", headers={"X-Sweep-Secret": "nope"}).status_code == 403
    assert swept.post("/tasks/sweep", headers=H).json()["marked_unknown"] == 1


def test_healthz_is_unchanged(swept):
    r = swept.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_webhook_url_comes_from_public_base_url_then_render_then_the_old_setting():
    s = Settings(_env_file=None, public_base_url="https://mini-me.onrender.com/", render_external_url="https://x.onrender.com",
                 telegram_public_base_url="https://old.example")
    assert webhook_url(s) == "https://mini-me.onrender.com/telegram/webhook"
    s = Settings(_env_file=None, render_external_url="https://x.onrender.com", telegram_public_base_url="https://old.example")
    assert webhook_url(s) == "https://x.onrender.com/telegram/webhook"
    s = Settings(_env_file=None, telegram_public_base_url="https://old.example")
    assert webhook_url(s) == "https://old.example/telegram/webhook"


def test_no_hostname_means_no_registration_and_no_crash(env):
    env.settings.public_base_url = env.settings.render_external_url = env.settings.telegram_public_base_url = ""
    env.settings.telegram_webhook_secret = "s"
    assert ensure_webhook(env.settings, env.fake) is False
    assert env.fake.webhook in ("", None)


def test_no_fly_hostname_is_baked_into_the_settings_defaults():
    s = Settings(_env_file=None)
    assert "fly.dev" not in s.base_url and s.base_url == ""


def test_dockerfile_listens_on_render_port_with_a_local_fallback():
    text = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    assert re.search(r"--port \$\{PORT:-8080\}", text)
