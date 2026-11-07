"""Run made-up messages through the real drafter and report timings + quality notes (D8a test run).

    python -m scripts.run_made_up_batch seed/made_up_messages.txt            # prints a markdown table
    python -m scripts.run_made_up_batch seed/made_up_messages.txt --out batch_report.md

Reads the self-model from DATABASE_URL (read-only; nothing is written) and uses LLM_PROVIDER / LLM_MODEL
with OWN_DATA_ONLY_MODE=true (made-up text only). Messages are masked first, exactly as the bot does.
This is the model call only; Telegram round-trip time is on top (see the dev doc).
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

from app import db
from app.config import get_settings
from app.drafting.base import DraftError, DraftRequest
from app.drafting.llm_drafter import LLMDrafter
from app.llm.base import get_llm
from app.privacy.masking import mask_text, pseudonymise
from app.selfmodel.seed import parse_own_lines
from app.selfmodel.store import SelfModelStore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--out")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds to wait between messages (free tiers: try 8 to stay under per-minute token caps)")
    ap.add_argument("--pause", type=float, default=0.0,
                    help="seconds to wait between messages (free tiers have per-minute token limits)")
    a = ap.parse_args(argv)

    s = get_settings()
    if not s.own_data_only_mode:
        print("Set OWN_DATA_ONLY_MODE=true to run made-up messages on an unverified provider.", file=sys.stderr)
        return 1
    engine = db.get_engine()
    store = SelfModelStore(engine)
    drafter = LLMDrafter(get_llm(), store, settings=s)
    msgs = parse_own_lines(Path(a.file).read_text(encoding="utf-8"))

    rows, times = [], []
    for n, raw in enumerate(msgs, 1):
        if n > 1 and a.pause:
            time.sleep(a.pause)
        masked = mask_text(raw)
        t0 = time.monotonic()
        try:
            r = drafter.draft(DraftRequest(f"batch-{n}", masked, "telegram", "group_queries", "new_group",
                                           pseudonymise(f"batch-{n}@batch.invalid")))
            ms = int((time.monotonic() - t0) * 1000)
            times.append(ms)
            used = ",".join(f"{i.layer[0].upper()}:{i.item_id[-4:]}" for i in r.items_used) or "-"
            rows.append((n, masked, r.text, f"{r.confidence:.2f}", r.rationale, used, ms, r.meta.get("retries", 0)))
        except DraftError as e:
            print(f"#{n} failed: {e}", file=sys.stderr)
            rows.append((n, masked, f"FAILED ({e.reason})", "-", "-", "-", int((time.monotonic() - t0) * 1000), 0))

    def esc(x):
        return str(x).replace("|", "\\|").replace("\n", " ")

    out = ["| # | incoming | draft | conf | why | items | ms | retries |", "|---|---|---|---|---|---|---|---|"]
    out += ["| " + " | ".join(esc(c) for c in row) + " |" for row in rows]
    if times:
        ts = sorted(times)
        out.append(f"\nn={len(times)} ok of {len(rows)}; median {statistics.median(ts):.0f} ms; "
                   f"p90 {ts[min(len(ts) - 1, int(len(ts) * 0.9))]} ms; max {ts[-1]} ms; "
                   f"provider={drafter.llm.provider} model={drafter.llm.model}; self-model {store.version_tag()}")
    text = "\n".join(out)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
