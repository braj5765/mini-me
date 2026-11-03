"""Prompt for your login password and print the hash to use as LOGIN_PASSWORD_HASH.
Run locally:  python -m scripts.hash_password   (the password is never echoed or stored)."""
import getpass

from app.auth import hash_password

if __name__ == "__main__":
    pw = getpass.getpass("Choose a login password: ")
    if pw != getpass.getpass("Repeat it: "):
        raise SystemExit("Passwords differ.")
    if len(pw) < 12:
        raise SystemExit("Use at least 12 characters.")
    print(hash_password(pw))
