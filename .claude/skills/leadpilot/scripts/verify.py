"""
Verification stack — triple-check every enriched contact before output.

Three checks per contact:
  1. Bright Data LinkedIn (Tier A direct URL preferred, Tier B name-based fallback)
  2. Firecrawl website team-page scrape
  3. SEC EDGAR (public companies only)

Usage:
    python -m scripts.verify --input enriched.json --out verified.json

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

BRIGHTDATA_DATASET_ID = "gd_l1viktl72bvl7bjuj0"
BRIGHTDATA_POLL_INTERVAL = 5  # seconds
BRIGHTDATA_MAX_WAIT = 120  # seconds before falling back


# ---------- LinkedIn (Bright Data) ---------- #

def linkedin_verify(contact: dict) -> dict:
    """
    Tier A: if contact["externalUrls"] contains a LinkedIn URL, pass that URL
            directly to Bright Data (preferred path).
    Tier B: else fall back to name+company search.

    Bright Data is async: submit -> snapshot_id -> poll until ready (max 120s).
    On timeout, log warning and return tier="timeout" so scorer assigns 0.

    Returns:
        {
            "tier": "A" | "B" | "timeout" | "fail",
            "current_company_match": bool,
            "linkedin_url": str | None,
            "raw": dict | None,
        }
    """
    raise NotImplementedError("verify.linkedin_verify: Bright Data LinkedIn API")


def _brightdata_submit(url_or_query: dict) -> str:
    """Submit a Bright Data scrape job, return snapshot_id."""
    raise NotImplementedError("verify._brightdata_submit")


def _brightdata_poll(snapshot_id: str) -> dict | None:
    """Poll snapshot until ready or until BRIGHTDATA_MAX_WAIT. Return data or None."""
    deadline = time.time() + BRIGHTDATA_MAX_WAIT
    while time.time() < deadline:
        # check status, return when ready
        time.sleep(BRIGHTDATA_POLL_INTERVAL)
    return None


# ---------- Website (Firecrawl) ---------- #

def website_verify(contact: dict) -> dict:
    """
    Scrape the company's team / leadership / about page via Firecrawl.

    Returns:
        {
            "team_page_found": bool,
            "name_present": bool,
            "title_present": bool,
            "name_explicitly_absent": bool,  # name missing from a public team page
        }
    """
    raise NotImplementedError("verify.website_verify: Firecrawl team-page scrape")


# ---------- SEC EDGAR ---------- #

def edgar_verify(contact: dict) -> dict | None:
    """
    Cross-check executive name in SEC filings (public companies only).

    Requires User-Agent header set from SEC_EDGAR_USER_AGENT env var.
    Skip silently for private companies (return None).

    Returns:
        {"executive_confirmed": bool, "filing_url": str} or None for private cos.
    """
    raise NotImplementedError("verify.edgar_verify: SEC EDGAR lookup")


# ---------- Orchestrator ---------- #

def verify_contact(contact: dict) -> dict:
    """Run all three verification steps and attach results to the contact dict."""
    contact["_verification"] = {
        "linkedin": linkedin_verify(contact),
        "website": website_verify(contact),
        "edgar": edgar_verify(contact),
    }
    return contact


def main() -> int:
    p = argparse.ArgumentParser(description="LeadPilot verification stack")
    p.add_argument("--input", required=True, help="enriched contacts JSON")
    p.add_argument("--out", required=True, help="verified contacts JSON")
    args = p.parse_args()

    contacts = json.loads(Path(args.input).read_text())
    verified = []
    for c in contacts:
        try:
            verified.append(verify_contact(c))
        except NotImplementedError as e:
            print(f"STUB: {e}", file=sys.stderr)
            return 0

    Path(args.out).write_text(json.dumps(verified, indent=2))
    print(f"Verified {len(verified)} contacts -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
