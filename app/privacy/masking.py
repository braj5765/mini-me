"""Masking at ingestion (D3a, decisions 5.14 points 2 and 4).

- Phone numbers and email addresses inside message text are replaced by [PHONE] / [EMAIL].
- Contacts get a stable pseudonym (HMAC of the real identifier) so admin screens and the event
  log never show a real name, number or address.
- Bias: when unsure, mask (recall over precision). Obvious dates are not treated as phone numbers.

Known limits (flag, not decided): personal names inside free text, URLs and street addresses are
NOT masked in this version.
"""
import hashlib
import hmac
import re

from app.config import get_settings

PHONE_TOKEN = "[PHONE]"
EMAIL_TOKEN = "[EMAIL]"

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")

# A run that starts with a digit or '+', continues with digits and single separators.
_PHONE_CANDIDATE = re.compile(r"(?<![\w@])(\+?\(?\d[\d\s().\-]{5,}\d)(?![\w])")

_DATE_LIKE = re.compile(
    r"^(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})$"
)


def _is_phone(candidate: str) -> bool:
    s = candidate.strip()
    if _DATE_LIKE.match(s):
        return False
    digits = re.sub(r"\D", "", s)
    if s.startswith("+"):
        return 8 <= len(digits) <= 15
    return 10 <= len(digits) <= 15


def mask_text(text: str | None) -> str | None:
    """Replace emails and phone numbers. Idempotent: masking masked text changes nothing."""
    if not text:
        return text
    text = _EMAIL.sub(EMAIL_TOKEN, text)

    def repl(m: re.Match) -> str:
        return PHONE_TOKEN if _is_phone(m.group(1)) else m.group(0)

    return _PHONE_CANDIDATE.sub(repl, text)


def normalise_identifier(identifier: str) -> str:
    """Normalise a contact handle so the same person always maps to the same pseudonym."""
    s = identifier.strip().lower()
    if "@" in s:
        return s
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:  # phone: compare on the last 10 digits (ignores +91 / 0 prefixes)
        return "tel:" + digits[-10:]
    return s


def pseudonymise(identifier: str, kind: str = "contact") -> str:
    """Stable, non-reversible label like 'contact-3fa91c2b'. Needs PSEUDONYM_KEY; keep it backed up."""
    key = get_settings().pseudonym_key.encode()
    digest = hmac.new(key, normalise_identifier(identifier).encode(), hashlib.sha256).hexdigest()
    return f"{kind}-{digest[:8]}"
