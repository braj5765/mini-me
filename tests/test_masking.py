import pytest

from app.config import get_settings
from app.privacy.masking import mask_text, pseudonymise


@pytest.mark.parametrize("raw,expected", [
    ("call me on +91 98765 43210 please", "call me on [PHONE] please"),
    ("my number is 9876543210", "my number is [PHONE]"),
    ("whatsapp 098765-43210 now", "whatsapp [PHONE] now"),
    ("(022) 2345 6789 is the office", "[PHONE] is the office"),
    ("+1 (415) 555-2671", "[PHONE]"),
    ("mail me at priya.s+club@iimranchi.ac.in thanks", "mail me at [EMAIL] thanks"),
    ("A@b.co and c_d@e-f.org", "[EMAIL] and [EMAIL]"),
])
def test_masks_phones_and_emails(raw, expected):
    assert mask_text(raw) == expected


@pytest.mark.parametrize("raw", [
    "Meeting on 2026-11-03 at 5pm",
    "Due 03/11/2026",
    "We have 120 members and 45 seats",
    "Room 304, batch of 2027",
    "Budget is 15000 rupees",   # 5 digits: not a phone
])
def test_leaves_dates_and_ordinary_numbers(raw):
    assert mask_text(raw) == raw


def test_idempotent_and_none_safe():
    once = mask_text("ring 9876543210 or a@b.com")
    assert mask_text(once) == once
    assert mask_text("") == ""
    assert mask_text(None) is None


def test_pseudonym_stable_and_hides_identity():
    a = pseudonymise("+91 98765 43210")
    assert a == pseudonymise("09876543210")          # same number, different format
    assert a == pseudonymise("+919876543210")
    assert "98765" not in a and a.startswith("contact-")
    assert pseudonymise("Priya@Example.com") == pseudonymise("priya@example.com")
    assert pseudonymise("someone else") != a


def test_pseudonym_depends_on_key(monkeypatch):
    a = pseudonymise("x@y.com")
    monkeypatch.setenv("PSEUDONYM_KEY", "another-key")
    get_settings.cache_clear()
    assert pseudonymise("x@y.com") != a
