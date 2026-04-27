"""
Confidence scorer — assigns 0-100 score to each verified contact.

Scoring inputs (max 100, plus EDGAR bonus):
  - LinkedIn verification (40 max): Tier A pass = 40, Tier B pass = 25,
        wrong-company = -20, fail = 0
  - Company website verification (25 max): name+title = 25, name only = 15,
        no team page = 10 (neutral), explicitly absent = -10
  - ZoomInfo data freshness (20 max): <=90d = 20, <=6mo = 15, <=12mo = 10, else 0
  - Contact data completeness (15 max): email = 5, phone = 5, linkedin URL = 5
  - SEC EDGAR (bonus 5): confirmed exec = +5, n/a or absent = 0

Tiers:
  HIGH    80-100  (call with confidence)
  MEDIUM  60-79   (worth calling, light verification on call)
  LOW     40-59   (flag for manual review before outreach)
  DISCARD <40     (logged but dropped from output)

Usage:
    python -m scripts.score --input verified.json --out scored.json

Phase 1 status: STUB.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

_DOTENV_PATH = find_dotenv(usecwd=True)
load_dotenv(_DOTENV_PATH)
REPO_ROOT = Path(_DOTENV_PATH).parent if _DOTENV_PATH else Path.cwd()


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


def score_linkedin(v: dict) -> tuple[int, str]:
    if not v:
        return 0, "no_data"
    if v.get("tier") == "A" and v.get("current_company_match"):
        return 40, "tier_A_pass"
    if v.get("tier") == "B" and v.get("current_company_match"):
        return 25, "tier_B_pass"
    if v.get("current_company_match") is False and v.get("linkedin_url"):
        return -20, "wrong_company"
    return 0, "fail_or_timeout"


def score_website(v: dict) -> tuple[int, str]:
    if not v or not v.get("team_page_found"):
        return 10, "no_team_page"  # neutral; common for SMBs
    if v.get("name_present") and v.get("title_present"):
        return 25, "name_and_title"
    if v.get("name_present"):
        return 15, "name_only"
    if v.get("name_explicitly_absent"):
        return -10, "explicitly_absent"
    return 0, "ambiguous"


def score_freshness(last_updated: str | None) -> tuple[int, str]:
    """ZoomInfo returns dates as US-format strings like '04/06/2026 09:26 AM'.
    We also tolerate ISO 8601 in case the schema changes."""
    if not last_updated:
        return 0, "unknown"
    dt = None
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(last_updated.strip().rstrip("Z"), fmt)
            break
        except ValueError:
            continue
    if not dt:
        try:
            dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
        except ValueError:
            return 0, "unparseable"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt).days
    if days <= 90:
        return 20, "<=90d"
    if days <= 180:
        return 15, "<=6mo"
    if days <= 365:
        return 10, "<=12mo"
    return 0, ">12mo"


def score_completeness(contact: dict) -> tuple[int, str]:
    pts = 0
    parts = []
    if contact.get("email"):
        pts += 5
        parts.append("email")
    if contact.get("directPhone") or contact.get("mobilePhone"):
        pts += 5
        parts.append("phone")
    # externalUrls items are {"type": "linkedin.com", "url": "..."} dicts
    if any(
        "linkedin.com" in (u.get("type") or u.get("url") or "").lower() if isinstance(u, dict)
        else "linkedin.com" in (u or "").lower()
        for u in (contact.get("externalUrls") or [])
    ):
        pts += 5
        parts.append("linkedin_url")
    return pts, "+".join(parts) if parts else "none"


def score_edgar(v: dict | None) -> tuple[int, str]:
    if not v:
        return 0, "n/a"
    if v.get("executive_confirmed"):
        return 5, "edgar_bonus"
    return 0, "edgar_absent"


def tier_for(score: int) -> str:
    if score >= 80:
        return "HIGH"
    if score >= 60:
        return "MEDIUM"
    if score >= 40:
        return "LOW"
    return "DISCARD"


def score_contact(contact: dict) -> dict:
    v = contact.get("_verification", {})
    li_pts, li_reason = score_linkedin(v.get("linkedin", {}))
    web_pts, web_reason = score_website(v.get("website", {}))
    fresh_pts, fresh_reason = score_freshness(contact.get("lastUpdatedDate"))
    comp_pts, comp_reason = score_completeness(contact)
    edgar_pts, edgar_reason = score_edgar(v.get("edgar"))

    total = li_pts + web_pts + fresh_pts + comp_pts + edgar_pts
    contact["_score"] = {
        "total": total,
        "tier": tier_for(total),
        "breakdown": {
            "linkedin": {"pts": li_pts, "reason": li_reason},
            "website": {"pts": web_pts, "reason": web_reason},
            "freshness": {"pts": fresh_pts, "reason": fresh_reason},
            "completeness": {"pts": comp_pts, "reason": comp_reason},
            "edgar_bonus": {"pts": edgar_pts, "reason": edgar_reason},
        },
    }
    return contact


def main() -> int:
    p = argparse.ArgumentParser(description="LeadPilot confidence scorer")
    p.add_argument("--input", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--keep-discard", action="store_true",
                   help="include DISCARD-tier rows in output (default: drop)")
    args = p.parse_args()

    contacts = json.loads(_resolve(args.input).read_text())
    scored = [score_contact(c) for c in contacts]
    if not args.keep_discard:
        scored = [c for c in scored if c["_score"]["tier"] != "DISCARD"]

    out = _resolve(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scored, indent=2))
    by_tier = {}
    for c in scored:
        by_tier[c["_score"]["tier"]] = by_tier.get(c["_score"]["tier"], 0) + 1
    print(f"Scored {len(scored)} contacts -> {out}")
    print(f"  Tier distribution: {by_tier}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
