"""Encryption at rest for small pieces of other people's personal data (decisions 5.16, 5.24 pt 1, 5.14 pt 8).

Used for sender-tag labels today; the encrypted pseudonym mapping (D3b) should use the same helper.
- Fernet (AES-128-CBC + HMAC, from the `cryptography` package). The ciphertext is a short text token.
- Keys: DATA_ENCRYPTION_KEY holds one or more Fernet keys, comma-separated. The FIRST encrypts; all of them decrypt
  (so a key can be rotated: put the new key first; the next start of the app re-encrypts every tag label under it
  automatically (app/migrations.py), and `python -m scripts.data_controls rewrap-tags` does the same by hand).
- Fallback: a key derived from APP_SECRET is always the last key. It lets the app work before a dedicated key
  is set (and in tests). Set a dedicated key in production: with only the fallback, anyone holding APP_SECRET can read
  the labels. Generate one with:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
- A value that cannot be decrypted (wrong key) is reported as `None`, never raised into a request.
"""
import base64
import hashlib
import hmac
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import Settings, get_settings

log = logging.getLogger("minime.crypto")

UNREADABLE = "(unreadable)"


def _derived_key(settings: Settings) -> bytes:
    raw = hmac.new(settings.app_secret.encode(), b"mini-me/data-encryption/v1", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw)


def has_dedicated_key(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return any(k.strip() for k in (s.data_encryption_key or "").split(","))


def _fernet(settings: Settings) -> MultiFernet:
    keys = [k.strip().encode() for k in (settings.data_encryption_key or "").split(",") if k.strip()]
    keys.append(_derived_key(settings))
    return MultiFernet([Fernet(k) for k in keys])


def encrypt_text(value: str, settings: Settings | None = None) -> str:
    return _fernet(settings or get_settings()).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(token: str | None, settings: Settings | None = None) -> str | None:
    """The original text, or None if there is nothing to decrypt or the key does not fit."""
    if not token:
        return None
    try:
        return _fernet(settings or get_settings()).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        log.warning("could not decrypt a stored value (wrong or changed key)")
        return None


def is_current(token: str, settings: Settings | None = None) -> bool:
    """True if the token is already encrypted under the first (current) key."""
    s = settings or get_settings()
    keys = [k.strip().encode() for k in (s.data_encryption_key or "").split(",") if k.strip()] or [_derived_key(s)]
    try:
        Fernet(keys[0]).decrypt(token.encode("ascii"))
        return True
    except (InvalidToken, ValueError):
        return False


def rewrap(token: str, settings: Settings | None = None) -> str | None:
    """Re-encrypt a token under the first (current) key. None if it cannot be decrypted."""
    try:
        return _fernet(settings or get_settings()).rotate(token.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError):
        return None
