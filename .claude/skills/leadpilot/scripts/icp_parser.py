"""
ICP parser — translates plain-English requests into structured ZoomInfo filter objects.

Calls Lookup Data first to convert plain-English values (industry, employee band,
revenue band, location, management level) into the coded values the Search
Contacts/Companies endpoints require. Caches Lookup results to
references/industry_codes_cache.json so we are not hammering Lookup on every run.

Usage:
    python -m scripts.icp_parser \\
        --industry "healthcare" \\
        --employees "250+" \\
        --revenue "$5M+" \\
        --location "300mi of Atlanta, GA" \\
        --titles "Facility Director,Facilities Manager" \\
        --out filter.json

Phase 1 status: STUB.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

CACHE_PATH = Path(".claude/skills/leadpilot/references/industry_codes_cache.json")


def lookup(field: str, value: str) -> list[str]:
    """
    Resolve a plain-English value to one or more ZoomInfo coded values.

    Args:
        field: one of {"industry", "employees", "revenue", "location", "managementLevel"}
        value: e.g. "healthcare", "250+", "$5M+", "Atlanta, GA, 300mi"

    Returns:
        list of coded values (string or numeric IDs depending on field)
    """
    raise NotImplementedError("icp_parser.lookup: call ZoomInfo Lookup Data")


def build_filter(
    industry: str,
    employees: str,
    revenue: str,
    location: str,
    titles: list[str],
    management_level: str | None = None,
) -> dict:
    """
    Build the JSON body Search Contacts expects.

    Title matching uses `jobTitle` array with exact match enabled (OR logic across
    titles). Location is converted to a radius search around a city/state.

    Returns:
        dict ready to POST to /contacts/search
    """
    raise NotImplementedError("icp_parser.build_filter: assemble Search Contacts payload")


def main() -> int:
    p = argparse.ArgumentParser(description="LeadPilot ICP parser")
    p.add_argument("--industry", required=True)
    p.add_argument("--employees", required=True, help='e.g. "250+", "100-500"')
    p.add_argument("--revenue", required=True, help='e.g. "$5M+"')
    p.add_argument("--location", required=True, help='e.g. "300mi of Atlanta, GA"')
    p.add_argument("--titles", required=True, help="comma-separated list of job titles")
    p.add_argument("--management-level", help="optional, e.g. 'Director, VP'")
    p.add_argument("--out", default="filter.json", help="path to write the filter JSON")
    args = p.parse_args()

    titles = [t.strip() for t in args.titles.split(",") if t.strip()]

    try:
        filt = build_filter(
            industry=args.industry,
            employees=args.employees,
            revenue=args.revenue,
            location=args.location,
            titles=titles,
            management_level=args.management_level,
        )
    except NotImplementedError as e:
        print(f"STUB: {e}", file=sys.stderr)
        return 0

    Path(args.out).write_text(json.dumps(filt, indent=2))
    print(f"Wrote filter to {args.out}")
    print(json.dumps(filt, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
