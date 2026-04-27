"""
Credit and request-limit monitor for ZoomInfo.

Calls the User Usage endpoint at the start and end of every job. Logs balance to
a running tally file. Warns at 75% of annual pool consumed, halts at 90% unless
--override is set.

Usage:
    python -m scripts.credit_monitor --check         # report current balance
    python -m scripts.credit_monitor --start <client>  # called at job start
    python -m scripts.credit_monitor --finalize <client> --used <N>  # called at job end

Phase 1 status: STUB.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TALLY_PATH = Path("logs/credit_tally.jsonl")
WARN_THRESHOLD = 0.75
HALT_THRESHOLD = 0.90


def get_usage() -> dict:
    """
    Call ZoomInfo /usage endpoint.

    Returns:
        {
            "credits_remaining": int,
            "credits_total": int,
            "requests_remaining": int,
            "requests_total": int,
            "period_end": "YYYY-MM-DD"
        }
    """
    raise NotImplementedError("credit_monitor.get_usage: call ZoomInfo User Usage endpoint")


def log_event(client: str, event: str, payload: dict) -> None:
    """Append one JSONL line to logs/credit_tally.jsonl."""
    TALLY_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": int(time.time()), "client": client, "event": event, **payload}
    with TALLY_PATH.open("a") as f:
        f.write(json.dumps(line) + "\n")


def check_ceiling(usage: dict, override: bool = False) -> tuple[str, str]:
    """
    Return (level, message) where level is one of: ok, warn, halt.
    """
    used_frac = 1 - (usage["credits_remaining"] / max(usage["credits_total"], 1))
    if used_frac >= HALT_THRESHOLD and not override:
        return "halt", f"Credit pool at {used_frac:.0%}. Halt unless --override is set."
    if used_frac >= WARN_THRESHOLD:
        return "warn", f"Credit pool at {used_frac:.0%}. Approaching ceiling."
    return "ok", f"Credit pool at {used_frac:.0%}."


def main() -> int:
    p = argparse.ArgumentParser(description="ZoomInfo credit monitor")
    p.add_argument("--check", action="store_true")
    p.add_argument("--start", metavar="CLIENT")
    p.add_argument("--finalize", metavar="CLIENT")
    p.add_argument("--used", type=int, default=0, help="credits used in this run (for --finalize)")
    p.add_argument("--override", action="store_true", help="ignore halt threshold")
    args = p.parse_args()

    try:
        usage = get_usage()
    except NotImplementedError:
        print("STUB: credit_monitor not yet implemented")
        return 0

    level, msg = check_ceiling(usage, override=args.override)
    print(f"[{level.upper()}] {msg}")
    print(f"  credits_remaining: {usage['credits_remaining']}/{usage['credits_total']}")

    if args.start:
        log_event(args.start, "start", usage)
    if args.finalize:
        log_event(args.finalize, "finalize", {**usage, "credits_used_this_run": args.used})

    return 1 if level == "halt" else 0


if __name__ == "__main__":
    sys.exit(main())
