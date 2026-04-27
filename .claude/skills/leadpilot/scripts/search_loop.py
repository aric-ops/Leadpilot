"""
Search loop — discovery + enrichment engine.

Three stages, runs until target count is hit or credit ceiling reached.

Stage A (optional): Firecrawl local company discovery
    Useful when target companies are small/local trades that may not be in
    ZoomInfo by name (Warrior plumbers, K&K freight subcontractors). Skip for
    big-corporate ICPs (healthcare 250+, manufacturing 500+) — ZoomInfo's own
    radius search covers those.

Stage B: ZoomInfo Search Contacts -> Enrich
    Runs a paginated Search Contacts (FREE) against the filter, optionally
    constrained to a list of companyIds from Stage A. Filters out contacts
    missing both email and phone. Enriches the top N by accuracy score in
    batches of 25 (CREDITS spent here, one per new record).

Stage C: broaden title list and rerun Stage B
    Issued by the orchestrator when Stage B comes up short. Caller passes the
    fallback title list and a "tried" set of titles already attempted, so the
    loop never re-issues the same query.

Usage:
    python -m scripts.search_loop --stage A --filter output/.tmp/filter.json \\
        --target 25 --out output/.tmp/stageA.json

    python -m scripts.search_loop --stage B --filter output/.tmp/filter.json \\
        --target 25 --out output/.tmp/enriched.json
        [--companies output/.tmp/stageA.json]   # if Stage A ran

    python -m scripts.search_loop --stage C --filter output/.tmp/filter.json \\
        --target 25 --fallback-titles "Facilities Manager,Director of Operations" \\
        --tried output/.tmp/tried.json \\
        --stop-condition broaden_until_hit \\
        --out output/.tmp/enriched.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import find_dotenv, load_dotenv

from ._zi_client import RateLimiter, zi_post

_DOTENV_PATH = find_dotenv(usecwd=True)
load_dotenv(_DOTENV_PATH)
REPO_ROOT = Path(_DOTENV_PATH).parent if _DOTENV_PATH else Path.cwd()

# -------- Output fields requested from Enrich Contact -------- #
# Pulled from /lookup/outputfields/contact/enrich; this is the working set the
# scoring + output stages need. All of these have accessGranted=true on Aric's
# account. Adjust if a future client has narrower access.
ENRICH_OUTPUT_FIELDS = [
    "id", "firstName", "lastName", "jobTitle", "jobFunction", "managementLevel",
    "email", "emailAlt", "phone", "mobilePhone", "directPhoneAlt", "mobilePhoneAlt",
    "externalUrls",  # LinkedIn URL lives here for Tier A verification
    "contactAccuracyScore", "lastUpdatedDate", "validDate",
    "companyId", "companyName", "companyWebsite", "companyPhone",
    "companyStreet", "companyCity", "companyState", "companyZipCode", "companyCountry",
    "companyEmployeeCount", "companyEmployeeRange",
    "companyRevenue", "companyRevenueNumeric", "companyRevenueRange",
    "companyPrimaryIndustry", "companyPrimaryIndustryCode",
]

ENRICH_BATCH_SIZE = 25  # ZoomInfo max
SEARCH_RPP = 25         # records per Search Contacts page
MAX_SEARCH_PAGES = 20   # safety: stop after 500 search results regardless
RATE = RateLimiter(rate_per_second=20)  # leave headroom under default 25/s tier


# ============================================================ #
# Stage A — Firecrawl local company discovery                  #
# ============================================================ #

FIRECRAWL_BASE = "https://api.firecrawl.dev/v1"


def firecrawl_search(query: str, limit: int = 10) -> list[dict]:
    """One Firecrawl Search call. Returns list of {title, url, description}."""
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        raise RuntimeError("FIRECRAWL_API_KEY not set in .env")
    r = requests.post(
        f"{FIRECRAWL_BASE}/search",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"query": query, "limit": limit},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("data", []) or []


def firecrawl_discover(filter_obj: dict, target: int) -> list[dict]:
    """
    Discover companies in target geography using Firecrawl Search.

    Builds 3-5 query variants (industry + geography + size hints) and unions
    the results, deduplicated by domain.

    Returns:
        list of {"name": str, "domain": str, "url": str, "source": str}
    """
    industries = filter_obj.get("industryCodes", "") or filter_obj.get("industryKeywords", "")
    industry_hint = filter_obj.get("industryKeywords") or industries.split(",")[0] if industries else "company"
    employee_min = filter_obj.get("employeeRangeMin", "")
    location_hint = filter_obj.get("zipCode", "") or filter_obj.get("state", "")

    # Convert state slug like 'usa.georgia' to plain 'Georgia' for the query
    if isinstance(location_hint, str) and location_hint.startswith("usa."):
        location_hint = location_hint.split(".", 1)[1].title()

    variants = [
        f"{industry_hint} companies near {location_hint}",
        f"{industry_hint} {employee_min}+ employees {location_hint}",
        f"largest {industry_hint} firms {location_hint}",
        f"{industry_hint} directory {location_hint}",
    ]

    seen_domains = set()
    results: list[dict] = []
    for q in variants:
        try:
            for hit in firecrawl_search(q, limit=10):
                url = (hit.get("url") or "").strip()
                if not url:
                    continue
                domain = url.replace("https://", "").replace("http://", "").split("/")[0].lower()
                if domain.startswith("www."):
                    domain = domain[4:]
                if domain in seen_domains:
                    continue
                seen_domains.add(domain)
                results.append({
                    "name": hit.get("title", "").strip(),
                    "domain": domain,
                    "url": url,
                    "source": q,
                })
                if len(results) >= target * 4:  # pull a wider net than target
                    break
        except Exception as e:
            print(f"  WARN Firecrawl '{q[:40]}...' failed: {e}", file=sys.stderr)

        if len(results) >= target * 4:
            break

    return results


# ============================================================ #
# Stage B — ZoomInfo Search Contacts + Enrich                  #
# ============================================================ #

def search_contacts_paginated(filter_obj: dict, max_pages: int = MAX_SEARCH_PAGES) -> list[dict]:
    """
    Run paginated Search Contacts (FREE). Stops when no more pages OR max_pages
    reached. Returns the raw search hits (NOT enriched yet).
    """
    all_hits: list[dict] = []
    page = 1
    while page <= max_pages:
        body = {**filter_obj, "rpp": SEARCH_RPP, "page": page}
        RATE.wait()
        resp = zi_post("/search/contact", body)
        data = resp.get("data", []) or []
        if not data:
            break
        all_hits.extend(data)
        # Stop early if we exceed the maxResults reported on page 1
        if len(all_hits) >= resp.get("maxResults", len(all_hits)):
            break
        page += 1
    return all_hits


def filter_qualifying_hits(hits: list[dict]) -> list[dict]:
    """
    Drop hits that have neither an email nor any phone number ZoomInfo can
    surface. Sort the rest by contactAccuracyScore DESC so the best candidates
    enrich first.
    """
    qualified = [
        h for h in hits
        if h.get("hasEmail") or h.get("hasSupplementalEmail")
        or h.get("hasDirectPhone") or h.get("hasMobilePhone")
    ]
    qualified.sort(key=lambda h: h.get("contactAccuracyScore", 0), reverse=True)
    return qualified


def _normalize_enriched(record: dict) -> dict:
    """
    Reshape ZoomInfo's Enrich Contact record into the schema downstream modules
    expect. ZoomInfo returns company fields nested under `company` and uses
    `phone` for the contact's direct phone — we rename to `directPhone` to
    match output column names. Also computes fullName.
    """
    co = record.get("company") or {}

    def _first(v):
        """ZoomInfo returns some fields as lists (e.g. primaryIndustry).
        Flatten to first non-empty string for our flat schema."""
        if isinstance(v, list):
            return v[0] if v else ""
        return v or ""

    return {
        # contact-level
        "id":                   record.get("id"),
        "firstName":            record.get("firstName") or "",
        "lastName":             record.get("lastName") or "",
        "fullName":             f"{record.get('firstName') or ''} {record.get('lastName') or ''}".strip(),
        "jobTitle":             record.get("jobTitle") or "",
        "jobFunction":          record.get("jobFunction") or "",
        "managementLevel":      record.get("managementLevel") or "",
        "email":                record.get("email") or "",
        "emailAlt":             record.get("emailAlt") or "",
        "directPhone":          record.get("phone") or "",
        "mobilePhone":          record.get("mobilePhone") or "",
        "directPhoneAlt":       record.get("directPhoneAlt") or "",
        "mobilePhoneAlt":       record.get("mobilePhoneAlt") or "",
        "externalUrls":         record.get("externalUrls") or [],
        "contactAccuracyScore": record.get("contactAccuracyScore"),
        "lastUpdatedDate":      record.get("lastUpdatedDate") or "",
        "validDate":            record.get("validDate") or "",
        # company sub-object — flatten ZoomInfo's nested keys into our schema
        "company": {
            "id":             co.get("id"),
            "name":           co.get("name") or "",
            "website":        co.get("website") or "",
            "phone":          co.get("phone") or "",
            "street":         co.get("street") or "",
            "city":           co.get("city") or "",
            "state":          co.get("state") or "",
            "zipCode":        co.get("zipCode") or "",
            "country":        co.get("country") or "",
            "industry":       _first(co.get("primaryIndustry") or co.get("industry")),
            "industryCode":   _first(co.get("primaryIndustryCode")),
            "employeeCount":  co.get("employeeCount"),
            "employeeRange":  co.get("employeeRange") or "",
            "revenue":        co.get("revenue") or "",
            "revenueRange":   co.get("revenueRange") or "",
        },
    }


def enrich_contacts(person_ids: list[int]) -> list[dict]:
    """
    Call /enrich/contact in batches of 25. Returns flattened, normalized list
    of enriched records. CREDITS are charged here.
    """
    enriched: list[dict] = []
    for i in range(0, len(person_ids), ENRICH_BATCH_SIZE):
        batch = person_ids[i:i + ENRICH_BATCH_SIZE]
        body = {
            "matchPersonInput": [{"personId": pid} for pid in batch],
            "outputFields": ENRICH_OUTPUT_FIELDS,
        }
        RATE.wait()
        resp = zi_post("/enrich/contact", body)
        # Response shape: {data: {result: [{matchStatus, data:[<contact>], ...}]}}
        for match in (resp.get("data", {}) or {}).get("result", []) or []:
            for raw in match.get("data", []) or []:
                enriched.append(_normalize_enriched(raw))
    return enriched


def stage_b(filter_obj: dict, target: int,
            company_ids: list[int] | None = None) -> tuple[list[dict], dict]:
    """
    Run Stage B end-to-end.

    Args:
        filter_obj:  Search Contacts filter from icp_parser
        target:      desired number of enriched contacts
        company_ids: optional list of ZoomInfo companyIds (from Stage A) to
                     constrain the search to. When provided, runs Search Contacts
                     once per company.

    Returns:
        (enriched_records, stats_dict)
    """
    stats = {"search_hits": 0, "qualified": 0, "enriched": 0, "credits_charged": 0}

    if company_ids:
        all_hits: list[dict] = []
        for cid in company_ids:
            hits = search_contacts_paginated({**filter_obj, "companyId": cid})
            all_hits.extend(hits)
    else:
        all_hits = search_contacts_paginated(filter_obj)

    stats["search_hits"] = len(all_hits)

    qualified = filter_qualifying_hits(all_hits)
    stats["qualified"] = len(qualified)

    # Take up to `target` best candidates. The orchestrator decides whether to
    # broaden via Stage C if `qualified < target`.
    to_enrich = qualified[:target]
    person_ids = [h["id"] for h in to_enrich if h.get("id")]
    enriched = enrich_contacts(person_ids)
    stats["enriched"] = len(enriched)
    # Each Enrich Contact call charges one credit per NEW record under management.
    # We don't know which were already managed, so report worst-case for budgeting.
    stats["credits_charged"] = len(enriched)

    return enriched, stats


# ============================================================ #
# Stage C — broaden titles and rerun                           #
# ============================================================ #

def stage_c(filter_obj: dict, target: int, fallback_titles: list[str],
            tried_titles: list[str], stop_condition: str) -> tuple[list[dict], dict, list[str]]:
    """
    Broaden by replacing the title list with fallback titles not yet tried.

    stop_condition is one of:
        "broaden_until_hit" — keep adding fallbacks until target met or list exhausted
        "deliver_partial"   — try once with fallback list and stop regardless
        "stop_and_ask"      — try once and surface the count to the user

    Returns:
        (enriched_records, stats, updated_tried_titles)
    """
    untried = [t for t in fallback_titles if t not in tried_titles]
    if not untried:
        return [], {"reason": "no untried fallback titles"}, tried_titles

    if stop_condition == "broaden_until_hit":
        # Try cumulative title sets: first new title alone, then accumulating
        results: list[dict] = []
        cumulative = list(tried_titles)
        for t in untried:
            cumulative.append(t)
            new_filter = {**filter_obj, "exactJobTitle": " OR ".join(cumulative)}
            enriched, stats = stage_b(new_filter, target - len(results))
            results.extend(enriched)
            if len(results) >= target:
                return results[:target], stats, cumulative
        return results, {"exhausted_fallbacks": True}, cumulative

    # deliver_partial / stop_and_ask: one shot with the full fallback set
    new_filter = {**filter_obj, "exactJobTitle": " OR ".join(tried_titles + untried)}
    enriched, stats = stage_b(new_filter, target)
    return enriched, stats, tried_titles + untried


# ============================================================ #
# CLI                                                          #
# ============================================================ #

def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


def main() -> int:
    p = argparse.ArgumentParser(description="LeadPilot search loop")
    p.add_argument("--stage", required=True, choices=["A", "B", "C"])
    p.add_argument("--filter", help="path to filter JSON")
    p.add_argument("--companies", help="path to Stage A output (Stage B optional input)")
    p.add_argument("--tried", help="path to tried-set JSON (Stage C)")
    p.add_argument("--target", type=int, default=25)
    p.add_argument("--fallback-titles", default="",
                   help="comma-separated fallback titles for Stage C")
    p.add_argument("--stop-condition", default="stop_and_ask",
                   choices=["broaden_until_hit", "deliver_partial", "stop_and_ask"])
    p.add_argument("--out", required=True)
    args = p.parse_args()

    filt = json.loads(_resolve(args.filter).read_text()) if args.filter else {}

    if args.stage == "A":
        results = firecrawl_discover(filt, args.target)
        out_payload = results
        print(f"Stage A: discovered {len(results)} unique domains via Firecrawl")

    elif args.stage == "B":
        company_ids = None
        if args.companies:
            stage_a_data = json.loads(_resolve(args.companies).read_text())
            # Stage A returns domains, not ZI companyIds yet. Resolution to
            # companyIds happens via Search Companies during Stage B's per-name
            # iteration. For now, only use company constraint if the file
            # already contains "companyId" entries (future enhancement).
            company_ids = [c["companyId"] for c in stage_a_data
                           if isinstance(c, dict) and c.get("companyId")]
            company_ids = company_ids or None

        enriched, stats = stage_b(filt, args.target, company_ids)
        out_payload = enriched
        print(f"Stage B: {stats}")

    else:  # C
        fallback_titles = [t.strip() for t in args.fallback_titles.split(",") if t.strip()]
        if not fallback_titles:
            print("ERROR: --fallback-titles required for stage C", file=sys.stderr)
            return 1
        tried = []
        if args.tried and _resolve(args.tried).exists():
            tried = json.loads(_resolve(args.tried).read_text()).get("titles", [])
        enriched, stats, updated_tried = stage_c(
            filt, args.target, fallback_titles, tried, args.stop_condition
        )
        out_payload = enriched
        if args.tried:
            _resolve(args.tried).write_text(json.dumps({"titles": updated_tried}, indent=2))
        print(f"Stage C: {stats}")

    out = _resolve(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(out_payload, indent=2))
    print(f"Wrote {len(out_payload) if isinstance(out_payload, list) else 1} records -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
