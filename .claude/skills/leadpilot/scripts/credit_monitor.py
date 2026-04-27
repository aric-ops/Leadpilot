"""
ZoomInfo credit + request-limit monitor.

Calls GET /lookup/usage at the start and end of every job. Logs balance to a
running tally file. The "credit pool" used for warn/halt thresholds is
ZoomInfo's `uniqueIdLimit` — that's the line item that ticks down per Enrich.

Other limits returned by /lookup/usage (informational):
  - requestLimit:  total API calls / period
  - recordLimit:   total records returned / period
  - webSightsApiRequestLimit / webSightsApiRecordLimit (zero if not on plan)

Usage:
    python -m scripts.credit_monitor --check
    python -m scripts.credit_monitor --start <client>
    python -m scripts.credit_monitor --finalize <client> --used <N>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from ._zi_client import zi_get

_DOTENV_PATH = find_dotenv(usecwd=True)
load_dotenv(_DOTENV_PATH)
REPO_ROOT = Path(_DOTENV_PATH).parent if _DOTENV_PATH else Path.cwd()

TALLY_PATH = REPO_ROOT / "logs" / "credit_tally.jsonl"
CREDIT_KEY = "uniqueIdLimit"  # the ZoomInfo limit that maps to "credits"
WARN_THRESHOLD = 0.75
HALT_THRESHOLD = 0.90


def get_usage() -> dict:
    """
    Call GET /lookup/usage and normalize the response.

    Returns:
        {
            "credits_total":     int,   # uniqueIdLimit.totalLimit
            "credits_used":      int,
            "credits_remaining": int,
            "requests_total":    int,
            "requests_remaining": int,
            "records_total":     int,
            "records_remaining": int,
            "raw":               <full ZoomInfo usage[] list>,
        }
    """
    data = zi_get("/lookup/usage")
    by_type = {row["limitType"]: row for row in data.get("usage", [])}

    credits = by_type.get(CREDIT_KEY, {})
    requests_lim = by_type.get("requestLimit", {})
    records = by_type.get("recordLimit", {})

    return {
        "credits_total":     credits.get("totalLimit", 0),
        "credits_used":      credits.get("currentUsage", 0),
        "credits_remaining": credits.get("usageRemaining", 0),
        "requests_total":     requests_lim.get("totalLimit", 0),
        "requests_remaining": requests_lim.get("usageRemaining", 0),
        "records_total":      records.get("totalLimit", 0),
        "records_remaining":  records.get("usageRemaining", 0),
        "raw": data.get("usage", []),
    }


def log_event(client: str, event: str, payload: dict) -> None:
    """Append one JSONL line to logs/credit_tally.jsonl."""
    TALLY_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": int(time.time()), "client": client, "event": event,
            **{k: v for k, v in payload.items() if k != "raw"}}
    with TALLY_PATH.open("a") as f:
        f.write(json.dumps(line) + "\n")


def check_ceiling(usage: dict, override: bool = False) -> tuple[str, str]:
    """Return (level, message) where level is one of: ok, warn, halt."""
    total = max(usage["credits_total"], 1)
    used_frac = usage["credits_used"] / total
    if used_frac >= HALT_THRESHOLD and not override:
        return "halt", (
            f"Credit pool at {used_frac:.0%} "
            f"({usage['credits_used']}/{usage['credits_total']}). "
            f"Halt unless --override is set."
        )
    if used_frac >= WARN_THRESHOLD:
        return "warn", (
            f"Credit pool at {used_frac:.0%} "
            f"({usage['credits_used']}/{usage['credits_total']}). "
            f"Approaching ceiling."
        )
    return "ok", (
        f"Credit pool at {used_frac:.1%} "
        f"({usage['credits_used']}/{usage['credits_total']}, "
        f"{usage['credits_remaining']:,} remaining)."
    )


def main() -> int:
    p = argparse.ArgumentParser(description="ZoomInfo credit monitor")
    p.add_argument("--check", action="store_true")
    p.add_argument("--start", metavar="CLIENT")
    p.add_argument("--finalize", metavar="CLIENT")
    p.add_argument("--used", type=int, default=0,
                   help="credits used in this run (for --finalize)")
    p.add_argument("--override", action="store_true",
                   help="ignore halt threshold")
    args = p.parse_args()

    usage = get_usage()
    level, msg = check_ceiling(usage, override=args.override)
    print(f"[{level.upper()}] {msg}")
    print(f"  requests:  {usage['requests_remaining']:,}/{usage['requests_total']:,} remaining")
    print(f"  records:   {usage['records_remaining']:,}/{usage['records_total']:,} remaining")

    if args.start:
        log_event(args.start, "start", usage)
    if args.finalize:
        log_event(args.finalize, "finalize",
                  {**usage, "credits_used_this_run": args.used})

    return 1 if level == "halt" else 0


if __name__ == "__main__":
    sys.exit(main())
