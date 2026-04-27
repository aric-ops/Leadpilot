"""
Verification stack — triple-check every enriched contact before output.

Three checks per contact (all run; failures degrade the score, not the row):
  1. Bright Data LinkedIn — Tier A (direct URL from ZoomInfo's externalUrls,
     preferred path), or Tier B (name+company discover_new search) when no URL.
  2. Firecrawl website team-page scrape — find name + title on the company's
     leadership/about/team page.
  3. SEC EDGAR — only for public companies (skip if no ticker). Cross-check
     the contact appears as an executive in recent filings.

Usage:
    python -m scripts.verify --input output/.tmp/enriched.json \\
        --out output/.tmp/verified.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import find_dotenv, load_dotenv

_DOTENV_PATH = find_dotenv(usecwd=True)
load_dotenv(_DOTENV_PATH)
REPO_ROOT = Path(_DOTENV_PATH).parent if _DOTENV_PATH else Path.cwd()


# ============================================================ #
# Bright Data — LinkedIn verification                          #
# ============================================================ #

BRIGHTDATA_BASE = "https://api.brightdata.com/datasets/v3"
BRIGHTDATA_DATASET_ID = "gd_l1viktl72bvl7bjuj0"  # LinkedIn People dataset
BRIGHTDATA_POLL_INTERVAL = 5     # seconds between progress checks
BRIGHTDATA_MAX_WAIT = 120        # total seconds before falling back


def _brightdata_headers() -> dict[str, str]:
    api_key = os.getenv("BRIGHTDATA_API_KEY")
    if not api_key:
        raise RuntimeError("BRIGHTDATA_API_KEY not set in .env")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _brightdata_submit(payload: list[dict], discover: bool = False) -> str:
    """Submit a Bright Data job. Returns snapshot_id."""
    params = {"dataset_id": BRIGHTDATA_DATASET_ID, "include_errors": "true"}
    if discover:
        params["type"] = "discover_new"
        params["discover_by"] = "name"
    r = requests.post(
        f"{BRIGHTDATA_BASE}/trigger",
        headers=_brightdata_headers(),
        params=params,
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    sid = data.get("snapshot_id") or data.get("collection_id")
    if not sid:
        raise RuntimeError(f"Bright Data trigger missing snapshot_id: {data}")
    return sid


def _brightdata_poll(snapshot_id: str) -> list[dict] | None:
    """Poll until ready or until BRIGHTDATA_MAX_WAIT. Return data or None."""
    deadline = time.time() + BRIGHTDATA_MAX_WAIT
    while time.time() < deadline:
        time.sleep(BRIGHTDATA_POLL_INTERVAL)
        # Check progress
        pr = requests.get(
            f"{BRIGHTDATA_BASE}/progress/{snapshot_id}",
            headers=_brightdata_headers(),
            timeout=15,
        )
        if not pr.ok:
            continue
        status = (pr.json() or {}).get("status", "")
        if status in ("ready", "done", "completed"):
            sn = requests.get(
                f"{BRIGHTDATA_BASE}/snapshot/{snapshot_id}",
                headers=_brightdata_headers(),
                params={"format": "json"},
                timeout=30,
            )
            sn.raise_for_status()
            data = sn.json()
            return data if isinstance(data, list) else [data]
        if status in ("failed", "canceled"):
            return None
    return None  # timeout


def linkedin_verify(contact: dict) -> dict:
    """
    Run LinkedIn verification for one contact.

    Tier A:  contact.externalUrls contains a LinkedIn URL — submit URL directly.
    Tier B:  no URL — submit name + company for discover_new.

    Returns:
        {
            "tier": "A" | "B" | "timeout" | "fail",
            "current_company_match": bool,
            "linkedin_url": str | None,
            "raw": dict | None,
        }
    """
    urls = [u.get("url") for u in (contact.get("externalUrls") or [])
            if isinstance(u, dict) and "linkedin.com" in (u.get("url") or "").lower()]
    company_name = (contact.get("company") or {}).get("name") or ""

    try:
        if urls:
            sid = _brightdata_submit([{"url": urls[0]}], discover=False)
            tier_label = "A"
            li_url = urls[0]
        else:
            payload = [{
                "first_name": contact.get("firstName", ""),
                "last_name":  contact.get("lastName", ""),
                "company_name": company_name,
            }]
            sid = _brightdata_submit(payload, discover=True)
            tier_label = "B"
            li_url = None

        records = _brightdata_poll(sid)
        if records is None:
            return {"tier": "timeout", "current_company_match": False,
                    "linkedin_url": li_url, "raw": None}

        if not records:
            return {"tier": "fail", "current_company_match": False,
                    "linkedin_url": li_url, "raw": None}

        # Best record = the first one for URL-based, or top match for discovery
        record = records[0] if isinstance(records, list) else records

        # Pull out current company. Bright Data's LinkedIn dataset names this
        # field varies between snapshots — try the common ones.
        current_co = (
            record.get("current_company", {}).get("name")
            or record.get("current_company_name")
            or record.get("company")
            or ""
        )
        match = bool(company_name) and bool(current_co) and (
            company_name.lower() in current_co.lower()
            or current_co.lower() in company_name.lower()
        )
        return {
            "tier": tier_label,
            "current_company_match": match,
            "linkedin_url": record.get("url") or li_url,
            "raw": {"current_company": current_co},
        }
    except Exception as e:
        print(f"  WARN linkedin_verify failed for {contact.get('fullName')}: {e}",
              file=sys.stderr)
        return {"tier": "fail", "current_company_match": False,
                "linkedin_url": urls[0] if urls else None, "raw": None}


# ============================================================ #
# Firecrawl — company team-page scrape                         #
# ============================================================ #

FIRECRAWL_BASE = "https://api.firecrawl.dev/v1"
# Only paths that typically list multiple people. /about and /about-us are
# excluded — they're usually company narratives, not leadership lists, and
# triggered too many false-positive "name explicitly absent" flags.
TEAM_PAGE_PATHS = ["/team", "/our-team", "/leadership", "/leaders",
                   "/staff", "/people", "/management", "/executives"]


def _firecrawl_headers() -> dict[str, str]:
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        raise RuntimeError("FIRECRAWL_API_KEY not set in .env")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _firecrawl_scrape(url: str) -> tuple[str, dict]:
    """Scrape a URL. Returns (markdown_body, metadata). Empty body on failure."""
    try:
        r = requests.post(
            f"{FIRECRAWL_BASE}/scrape",
            headers=_firecrawl_headers(),
            json={"url": url, "formats": ["markdown"]},
            timeout=45,
        )
        if not r.ok:
            return "", {}
        data = r.json().get("data", {}) or {}
        return data.get("markdown", "") or "", data.get("metadata", {}) or {}
    except Exception:
        return "", {}


def website_verify(contact: dict) -> dict:
    """
    Try common team-page paths under the company website. Returns dict with:
        team_page_found       — at least one URL returned content
        name_present          — full name appears anywhere on a team page
        title_present         — job title also appears on the same page
        name_explicitly_absent — found a team page but the name wasn't on it
        urls_tried            — list of URLs that returned content
    """
    co = contact.get("company") or {}
    website = (co.get("website") or "").strip()
    if not website:
        return {"team_page_found": False, "name_present": False, "title_present": False,
                "name_explicitly_absent": False, "urls_tried": []}

    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    website = website.rstrip("/")

    full_name = (contact.get("fullName") or "").strip()
    job_title = (contact.get("jobTitle") or "").strip()

    found_urls: list[str] = []
    name_hit = False
    title_hit = False
    explicit_absent = False

    real_team_pages = 0  # pages that returned 200 AND have team-list signals

    for path in TEAM_PAGE_PATHS:
        body, meta = _firecrawl_scrape(website + path)
        if not body:
            continue

        # 404s, 500s, etc. — Firecrawl still returns the error-page body, but
        # these are not real team pages. Skip them.
        if meta.get("statusCode") and meta.get("statusCode") != 200:
            continue

        # The page title is a strong signal of "real team page" content.
        title = (meta.get("title") or "").lower()
        is_team_titled = any(kw in title for kw in
                             ["team", "leadership", "staff", "people", "executives", "management"])

        found_urls.append(website + path)
        if is_team_titled:
            real_team_pages += 1

        body_lower = body.lower()
        if full_name.lower() in body_lower:
            name_hit = True
            if job_title.lower() in body_lower:
                title_hit = True
            break  # name found, stop probing

    return {
        "team_page_found": real_team_pages > 0,
        "name_present":    name_hit,
        "title_present":   title_hit,
        # Only flag explicitly_absent when at least one page WITH team-page
        # title signals returned 200 AND the name still wasn't on it.
        "name_explicitly_absent": real_team_pages >= 1 and not name_hit,
        "urls_tried": found_urls,
    }


# ============================================================ #
# SEC EDGAR — public-company executive verification             #
# ============================================================ #

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"


def edgar_verify(contact: dict) -> dict | None:
    """
    Cross-check executive name in recent SEC filings (public companies only).

    Returns:
        {"executive_confirmed": bool, "filing_url": str|None}  for public cos.
        None for private cos (no ticker).
    """
    co = contact.get("company") or {}
    ticker = co.get("ticker") or co.get("companyTicker") or ""
    if not ticker:
        return None  # private company

    full_name = (contact.get("fullName") or "").strip()
    if not full_name:
        return {"executive_confirmed": False, "filing_url": None}

    user_agent = os.getenv("SEC_EDGAR_USER_AGENT", "LeadPilot aric@harbingermarketing.com")
    try:
        r = requests.get(
            EDGAR_SEARCH_URL,
            params={"q": f'"{full_name}" "{ticker}"', "forms": "10-K,DEF 14A",
                    "dateRange": "custom", "startdt": "2024-01-01", "enddt": "2026-12-31"},
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=20,
        )
        if not r.ok:
            return {"executive_confirmed": False, "filing_url": None}
        hits = (r.json() or {}).get("hits", {}).get("hits", [])
        if not hits:
            return {"executive_confirmed": False, "filing_url": None}
        # Build a filing URL from the first hit
        h0 = hits[0]
        adsh = h0.get("_id", "").replace("-", "")
        cik = (h0.get("_source", {}).get("ciks") or ["0"])[0]
        url = (f"https://www.sec.gov/cgi-bin/browse-edgar?"
               f"action=getcompany&CIK={cik}&type=10-K") if cik else None
        return {"executive_confirmed": True, "filing_url": url}
    except Exception as e:
        print(f"  WARN edgar_verify failed for {contact.get('fullName')}: {e}",
              file=sys.stderr)
        return {"executive_confirmed": False, "filing_url": None}


# ============================================================ #
# Orchestrator                                                  #
# ============================================================ #

def verify_contact(contact: dict) -> dict:
    """Run all three verification steps and attach results to the contact dict."""
    contact["_verification"] = {
        "linkedin": linkedin_verify(contact),
        "website":  website_verify(contact),
        "edgar":    edgar_verify(contact),
    }
    return contact


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


def main() -> int:
    p = argparse.ArgumentParser(description="LeadPilot verification stack")
    p.add_argument("--input", required=True, help="enriched contacts JSON")
    p.add_argument("--out", required=True, help="verified contacts JSON")
    p.add_argument("--skip-linkedin", action="store_true",
                   help="skip Bright Data calls (saves $$ during dev)")
    p.add_argument("--skip-website", action="store_true")
    p.add_argument("--skip-edgar", action="store_true")
    args = p.parse_args()

    contacts = json.loads(_resolve(args.input).read_text())
    verified: list[dict] = []
    for i, c in enumerate(contacts, 1):
        print(f"  [{i}/{len(contacts)}] verifying {c.get('fullName')} @ "
              f"{(c.get('company') or {}).get('name', '?')}")

        v = {}
        if args.skip_linkedin:
            v["linkedin"] = {"tier": "skipped", "current_company_match": False,
                             "linkedin_url": None, "raw": None}
        else:
            v["linkedin"] = linkedin_verify(c)

        if args.skip_website:
            v["website"] = {"team_page_found": False, "name_present": False,
                            "title_present": False, "name_explicitly_absent": False,
                            "urls_tried": []}
        else:
            v["website"] = website_verify(c)

        if args.skip_edgar:
            v["edgar"] = None
        else:
            v["edgar"] = edgar_verify(c)

        c["_verification"] = v
        verified.append(c)

    out = _resolve(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(verified, indent=2))
    print(f"Verified {len(verified)} contacts -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
