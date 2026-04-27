"""
Run logger — appends one summary line per job to logs/runs.jsonl.

Captures: timestamp, client, ICP filters, credits used + remaining, contact
counts (found/rejected/delivered), verification pass rates by source, average
confidence, tier distribution, errors.

Usage:
    python -m scripts.run_logger --start --client "Warrior Restoration"
    python -m scripts.run_logger --finalize --client "Warrior Restoration" \\
        --filter filter.json --scored scored.json --credits-used 73

Phase 1 status: functional.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

_DOTENV_PATH = find_dotenv(usecwd=True)
load_dotenv(_DOTENV_PATH)
REPO_ROOT = Path(_DOTENV_PATH).parent if _DOTENV_PATH else Path.cwd()


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


LOG_PATH = REPO_ROOT / "logs" / "runs.jsonl"
STATE_PATH = REPO_ROOT / "logs" / ".run_state.json"  # tracks current job


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text())


def start_run(client: str, filter_path: str | None) -> None:
    state = {
        "client": client,
        "started_at": int(time.time()),
        "filter": json.loads(_resolve(filter_path).read_text()) if filter_path else None,
    }
    _save_state(state)
    print(f"Run started: {client} @ {state['started_at']}")


def finalize_run(client: str, scored_path: str, credits_used: int,
                 filter_path: str | None = None) -> None:
    state = _load_state()
    contacts = json.loads(_resolve(scored_path).read_text())

    tiers = Counter(c["_score"]["tier"] for c in contacts if "_score" in c)
    total = len(contacts)
    avg_score = (sum(c["_score"]["total"] for c in contacts) / total) if total else 0

    # Verification pass rates
    li_tier_a = sum(1 for c in contacts if c.get("_verification", {}).get("linkedin", {}).get("tier") == "A")
    li_tier_b = sum(1 for c in contacts if c.get("_verification", {}).get("linkedin", {}).get("tier") == "B")
    web_pass = sum(1 for c in contacts if c.get("_verification", {}).get("website", {}).get("name_present"))
    edgar_pass = sum(1 for c in contacts
                     if (c.get("_verification", {}).get("edgar") or {}).get("executive_confirmed"))

    summary = {
        "client": client,
        "started_at": state.get("started_at"),
        "finished_at": int(time.time()),
        "filter": state.get("filter") or (json.loads(_resolve(filter_path).read_text()) if filter_path else None),
        "delivered": total,
        "tiers": dict(tiers),
        "avg_confidence": round(avg_score, 1),
        "credits_used": credits_used,
        "verification_rates": {
            "linkedin_tier_A": li_tier_a,
            "linkedin_tier_B": li_tier_b,
            "website_name_present": web_pass,
            "edgar_confirmed": edgar_pass,
        },
    }

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(summary) + "\n")

    if STATE_PATH.exists():
        STATE_PATH.unlink()

    print(f"Run finalized -> {LOG_PATH}")
    print(json.dumps(summary, indent=2))


def main() -> int:
    p = argparse.ArgumentParser(description="LeadPilot run logger")
    p.add_argument("--start", action="store_true")
    p.add_argument("--finalize", action="store_true")
    p.add_argument("--client", required=True)
    p.add_argument("--filter", help="ICP filter JSON path")
    p.add_argument("--scored", help="scored contacts JSON path (--finalize)")
    p.add_argument("--credits-used", type=int, default=0)
    args = p.parse_args()

    if args.start:
        start_run(args.client, args.filter)
        return 0
    if args.finalize:
        if not args.scored:
            print("ERROR: --finalize requires --scored", file=sys.stderr)
            return 1
        finalize_run(args.client, args.scored, args.credits_used, args.filter)
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
