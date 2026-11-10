"""Live check of share-to-draft against a running app, with MADE-UP text only (Dev 6).

    $env:MINI_ME_TOKEN = '<your token>'          # PowerShell; keeps the token out of the command line
    python -m scripts.share_smoke --url https://<your-service>.onrender.com

Checks, in order: /healthz; a wrong token gets 401; a made-up message gets a plain-text draft with the headers the
shortcut reads; the copied call returns ok (it logs `draft_copied`); the JSON form works; a money message is asked
first (HTTP 409, no draft). It prints what it saw, never the token. The message used contains a made-up phone number
so you can confirm in /events that it was masked.
"""
import argparse
import os
import sys

import httpx

MADE_UP = "Hi! Is the club meet on Friday at 5? Call Test Person on 9876543210 if the hall changes."
MONEY = "Please pay Rs 500 for the hall booking by tonight."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("MINI_ME_URL", ""), help="your Render address, or set MINI_ME_URL")
    args = ap.parse_args(argv)
    token = os.environ.get("MINI_ME_TOKEN", "").strip()
    if not token:
        print("Set MINI_ME_TOKEN first (see the docstring).")
        return 2
    if not args.url:
        print("Give the app address: --url https://<your-service>.onrender.com (or set MINI_ME_URL).")
        return 2
    base, ok = args.url.rstrip("/"), True
    auth = {"Authorization": f"Bearer {token}"}
    h = {"Content-Type": "text/plain; charset=utf-8"}

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and cond
        print(f"[{'OK' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))

    with httpx.Client(timeout=60) as c:
        r = c.get(f"{base}/healthz")
        check("healthz", r.status_code == 200, r.text[:60])
        r = c.post(f"{base}/api/draft?format=text", content=MADE_UP.encode(), headers={**h, "Authorization": "Bearer mm_wrong"})
        check("wrong token gets 401", r.status_code == 401, f"HTTP {r.status_code}")
        r = c.post(f"{base}/api/draft?format=text", content=MADE_UP.encode(), headers={**h, **auth})
        check("plain-text draft", r.status_code == 200 and bool(r.text.strip()), f"HTTP {r.status_code}")
        mid = r.headers.get("x-mini-me-message-id", "")
        print(f"      message id: {mid}  confidence: {r.headers.get('x-mini-me-confidence')}")
        print(f"      draft: {r.text.strip()[:300]}")
        check("message id header present", mid.startswith("sh-"))
        if mid:
            r2 = c.post(f"{base}/api/draft/{mid}/copied", headers=auth)
            check("copied call logs draft_copied", r2.status_code == 200 and r2.json().get("outcome") == "waiting", r2.text[:100])
        r = c.post(f"{base}/api/draft?format=json", content=MADE_UP.encode(), headers={**h, **auth})
        j = r.json() if r.status_code == 200 else {}
        check("JSON draft", j.get("action") == "draft" and "9876543210" not in j.get("masked_text", ""), f"HTTP {r.status_code}")
        r = c.post(f"{base}/api/draft?format=text", content=MONEY.encode(), headers={**h, **auth})
        check("money message is asked first (409, no draft)", r.status_code == 409, f"HTTP {r.status_code}: {r.text[:80]}")
    print("\nAll checks passed." if ok else "\nSome checks failed.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
