"""
Search loop — the autonomous discovery engine.

Three stages, runs until target count is hit or credit ceiling reached.

Stage A: Firecrawl local company discovery
    Build a list of real companies in target geography before touching ZoomInfo.

Stage B: ZoomInfo match + enrich
    For each local company, find ZoomInfo record, run Search Contacts with title
    filter (free, iterates aggressively), then Enrich Contact in batches of 25
    (credits spent here only).

Stage C: Loop and broaden
    Title fallback list -> geography expansion -> HQ contacts as last resort.
    Respects user's stop-condition from SKILL.md step 2.

Usage:
    python -m scripts.search_loop --stage A --filter filter.json --target 25 --out stageA.json
    python -m scripts.search_loop --stage B --companies stageA.json --filter filter.json --out enriched.json
    python -m scripts.search_loop --stage C --tried tried.json --target 25 --out enriched.json

Phase 1 status: STUB.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# ---------- Stage A ---------- #

def firecrawl_discover(filter_obj: dict, target: int) -> list[dict]:
    """
    Discover real companies in target geography using Firecrawl Search + Extract.

    Generates multiple query variants per geography (Chamber of Commerce listings,
    industry directories, association member pages) to catch different naming
    conventions. Deduplicates by company name + address.

    Returns:
        list of {"name": str, "address": str, "website": str, "phone": str|None,
                 "industry_tags": list[str], "is_branch_office": bool}
    """
    raise NotImplementedError("search_loop.firecrawl_discover")


# ---------- Stage B ---------- #

def zoominfo_match_company(local_company: dict) -> dict | None:
    """
    Call ZoomInfo Search Companies for one local company by name + city/state.

    Returns the ZoomInfo company record (with companyId), or None if no match.
    Flags whether match is HQ, subsidiary, or branch office.
    """
    raise NotImplementedError("search_loop.zoominfo_match_company")


def search_contacts(company_id: str, filter_obj: dict) -> list[dict]:
    """
    Call ZoomInfo Search Contacts (FREE) at one company with title + filter logic.

    Iterate aggressively — Search Contacts is free, so try variations until matches
    look right. For multi-location companies, filter contacts to that location not
    HQ staff. Return record IDs of verified matches.
    """
    raise NotImplementedError("search_loop.search_contacts")


def enrich_contacts(record_ids: list[str]) -> list[dict]:
    """
    Call ZoomInfo Enrich Contact in batches of up to 25.

    This is where credits are spent. Returns full contact records including email,
    phone, and externalUrls (LinkedIn URL).
    """
    raise NotImplementedError("search_loop.enrich_contacts")


# ---------- Stage C ---------- #

def broaden(tried: dict, target: int, fallback_titles: list[str], stop_condition: str) -> list[dict]:
    """
    Broadening order:
      1. Expand title OR list using fallback_titles (provided per run by user)
      2. Expand geography: county -> adjacent counties -> state region
      3. Allow HQ contacts for big corporations as last resort (flagged)

    Maintains a tried-and-ruled-out list so it never repeats failed queries.

    stop_condition is one of:
      "broaden_until_hit" | "deliver_partial" | "stop_and_ask"
    """
    raise NotImplementedError("search_loop.broaden")


def main() -> int:
    p = argparse.ArgumentParser(description="LeadPilot search loop")
    p.add_argument("--stage", required=True, choices=["A", "B", "C"])
    p.add_argument("--filter", help="path to filter JSON (stages A and B)")
    p.add_argument("--companies", help="path to stage A output (stage B)")
    p.add_argument("--tried", help="path to tried-set JSON (stage C)")
    p.add_argument("--target", type=int, default=25)
    p.add_argument("--fallback-titles", default="", help="comma-separated titles for stage C")
    p.add_argument("--stop-condition", default="stop_and_ask",
                   choices=["broaden_until_hit", "deliver_partial", "stop_and_ask"])
    p.add_argument("--out", required=True)
    args = p.parse_args()

    try:
        if args.stage == "A":
            filt = json.loads(Path(args.filter).read_text())
            results = firecrawl_discover(filt, args.target)
        elif args.stage == "B":
            companies = json.loads(Path(args.companies).read_text())
            filt = json.loads(Path(args.filter).read_text())
            enriched = []
            for company in companies:
                zi = zoominfo_match_company(company)
                if not zi:
                    continue
                contact_ids = search_contacts(zi["companyId"], filt)
                if contact_ids:
                    enriched.extend(enrich_contacts(contact_ids))
            results = enriched
        else:  # C
            tried = json.loads(Path(args.tried).read_text())
            fallback_titles = [t.strip() for t in args.fallback_titles.split(",") if t.strip()]
            results = broaden(tried, args.target, fallback_titles, args.stop_condition)
    except NotImplementedError as e:
        print(f"STUB: {e}", file=sys.stderr)
        return 0

    Path(args.out).write_text(json.dumps(results, indent=2))
    print(f"Stage {args.stage}: wrote {len(results)} records to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
