"""
LeadPilot MCP server.

Wraps the 8 modules under .claude/skills/leadpilot/scripts/ as MCP tools so the
same logic that powers the Claude Code skill can be called from Cowork, the
Claude desktop app, the Claude phone app, or any other MCP client.

Run modes:
    1. STDIO (for local Claude desktop app via mcp config):
         python mcp_server.py
    2. HTTP/SSE (for Cowork or remote clients, deployed to Railway/Fly/etc.):
         uvicorn mcp_server:app --host 0.0.0.0 --port ${PORT:-8000}

Tools exposed:
    check_credits           — current ZoomInfo credit balance
    parse_icp               — plain-English ICP -> Search Contacts filter
    run_full_job            — end-to-end: ICP -> search -> verify -> score -> XLSX
    search_and_enrich       — Stage B only (skip verify/score)
    verify_contacts         — run verification stack on existing enriched JSON
    get_run_summary         — read latest run from logs/runs.jsonl

Auth model:
    Same .env file the CLI uses (ZOOMINFO_*, BRIGHTDATA_*, FIRECRAWL_*,
    SEC_EDGAR_USER_AGENT). When deployed, set these as platform secrets.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Make the skill's scripts importable
SKILL_ROOT = Path(__file__).parent / ".claude" / "skills" / "leadpilot"
sys.path.insert(0, str(SKILL_ROOT))

from mcp.server import FastMCP  # type: ignore

# Import our existing modules (no logic changes — just call them)
from scripts.auth import get_access_token
from scripts.credit_monitor import get_usage, check_ceiling
from scripts.icp_parser import build_filter
from scripts.search_loop import stage_b, stage_c
from scripts.verify import verify_contact
from scripts.score import score_contact
from scripts.output import row_for_contact, load_columns, write_xlsx, slugify

REPO_ROOT = Path(__file__).parent
OUTPUT_DIR = REPO_ROOT / "output"

mcp = FastMCP("leadpilot")


# ============================================================ #
# Tool: check_credits                                          #
# ============================================================ #

@mcp.tool()
def check_credits() -> dict[str, Any]:
    """
    Check the current ZoomInfo credit balance.

    Returns a dict with: credits_total, credits_used, credits_remaining,
    requests_remaining, records_remaining, and a status of ok/warn/halt.
    Call this before kicking off a big job to confirm headroom.
    """
    usage = get_usage()
    level, msg = check_ceiling(usage, override=False)
    return {
        "status": level,
        "message": msg,
        "credits_total": usage["credits_total"],
        "credits_used": usage["credits_used"],
        "credits_remaining": usage["credits_remaining"],
        "requests_remaining": usage["requests_remaining"],
        "records_remaining": usage["records_remaining"],
    }


# ============================================================ #
# Tool: parse_icp                                              #
# ============================================================ #

@mcp.tool()
def parse_icp(
    industry: str,
    employees: str,
    revenue: str,
    location: str,
    titles: list[str],
    management_level: str | None = None,
    rpp: int = 25,
) -> dict[str, Any]:
    """
    Translate plain-English ICP into a ZoomInfo Search Contacts filter.

    Args:
        industry: e.g. "healthcare", "construction", "plumbing"
        employees: e.g. "250+", "100-500"
        revenue: e.g. "$5M+", "$1M-$10M"
        location: e.g. "300mi of Atlanta, GA", "30303, 100mi", "GA"
        titles: list of exact job titles, OR'd together
        management_level: optional, e.g. "Director", "VP"
        rpp: records per Search Contacts page (max 25)

    Returns the filter dict ready to pass to search_and_enrich or run_full_job.
    """
    return build_filter(
        industry=industry,
        employees=employees,
        revenue=revenue,
        location=location,
        titles=titles,
        management_level=management_level,
        rpp=rpp,
    )


# ============================================================ #
# Tool: search_and_enrich (Stage B only)                       #
# ============================================================ #

@mcp.tool()
def search_and_enrich(filter_obj: dict, target: int = 25) -> dict[str, Any]:
    """
    Run the discovery + enrichment pipeline (Search Contacts -> Enrich Contact).

    Search Contacts is FREE; only Enrich consumes credits, one per new record.
    Filters out hits with no email AND no phone before spending credits.

    Args:
        filter_obj: filter dict from parse_icp
        target: number of contacts to enrich

    Returns:
        {"contacts": [...], "stats": {...}}
    """
    enriched, stats = stage_b(filter_obj, target)
    return {"contacts": enriched, "stats": stats}


# ============================================================ #
# Tool: verify_contacts                                        #
# ============================================================ #

@mcp.tool()
def verify_contacts(
    contacts: list[dict],
    skip_linkedin: bool = False,
    skip_website: bool = False,
    skip_edgar: bool = False,
) -> list[dict]:
    """
    Run the triple-verification stack on a list of enriched contacts.

    For each contact: Bright Data LinkedIn (Tier A direct URL preferred,
    Tier B name+company fallback), Firecrawl team-page scrape, SEC EDGAR
    (public-company executive lookup).

    Returns the contacts with a `_verification` block attached to each.
    """
    out = []
    for c in contacts:
        v = {}
        if skip_linkedin:
            v["linkedin"] = {"tier": "skipped", "current_company_match": False,
                             "linkedin_url": None, "raw": None}
        else:
            from scripts.verify import linkedin_verify
            v["linkedin"] = linkedin_verify(c)

        if skip_website:
            v["website"] = {"team_page_found": False, "name_present": False,
                            "title_present": False, "name_explicitly_absent": False,
                            "urls_tried": []}
        else:
            from scripts.verify import website_verify
            v["website"] = website_verify(c)

        if skip_edgar:
            v["edgar"] = None
        else:
            from scripts.verify import edgar_verify
            v["edgar"] = edgar_verify(c)

        c["_verification"] = v
        out.append(c)
    return out


# ============================================================ #
# Tool: run_full_job (end-to-end orchestrator)                 #
# ============================================================ #

@mcp.tool()
def run_full_job(
    client: str,
    industry: str,
    employees: str,
    revenue: str,
    location: str,
    titles: list[str],
    target: int = 25,
    fallback_titles: list[str] | None = None,
    skip_linkedin: bool = False,
) -> dict[str, Any]:
    """
    Run the full LeadPilot pipeline end-to-end and write the 25-column XLSX.

    This is the one-shot tool — equivalent to walking through the 12-step
    SKILL.md flow with a single call.

    Args:
        client: partner name (used in filename + Deal Title column)
        industry/employees/revenue/location/titles: ICP fields
        target: desired contact count
        fallback_titles: titles to try if exact list comes up short
        skip_linkedin: skip Bright Data calls (saves $$ during testing)

    Returns:
        {
            "output_file": "output/<client>_<date>.xlsx",
            "delivered": int,
            "tier_distribution": {"HIGH": N, "MEDIUM": N, "LOW": N},
            "credits_used": int,
            "stats": {...},
        }
    """
    from datetime import date

    # 1. Credit pre-check
    usage = get_usage()
    level, msg = check_ceiling(usage)
    if level == "halt":
        return {"error": "credit ceiling hit", "message": msg}
    starting_credits_used = usage["credits_used"]

    # 2. Build filter
    filter_obj = build_filter(
        industry=industry,
        employees=employees,
        revenue=revenue,
        location=location,
        titles=titles,
    )

    # 3. Stage B — search + enrich
    enriched, stats = stage_b(filter_obj, target)

    # 4. Stage C if short
    if len(enriched) < target and fallback_titles:
        more, _, _ = stage_c(filter_obj, target - len(enriched), fallback_titles,
                              titles, "broaden_until_hit")
        enriched.extend(more)

    # 5. Verify each
    verified = verify_contacts(enriched, skip_linkedin=skip_linkedin)

    # 6. Score, drop DISCARDs
    scored = [score_contact(c) for c in verified]
    scored = [c for c in scored if c["_score"]["tier"] != "DISCARD"]
    scored.sort(key=lambda c: c["_score"]["total"], reverse=True)

    # 7. Output XLSX
    columns = load_columns()
    rows = [row_for_contact(c, client, columns) for c in scored]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{slugify(client)}_{date.today().isoformat()}.xlsx"
    write_xlsx(rows, columns, out_path)

    # 8. Credit delta
    final_usage = get_usage()
    credits_used = final_usage["credits_used"] - starting_credits_used

    tiers: dict[str, int] = {}
    for c in scored:
        t = c["_score"]["tier"]
        tiers[t] = tiers.get(t, 0) + 1

    return {
        "output_file": str(out_path.relative_to(REPO_ROOT)),
        "delivered": len(scored),
        "tier_distribution": tiers,
        "credits_used": credits_used,
        "credits_remaining_after": final_usage["credits_remaining"],
        "stats": stats,
    }


# ============================================================ #
# Tool: get_run_summary                                         #
# ============================================================ #

@mcp.tool()
def get_run_summary(client: str | None = None, limit: int = 5) -> list[dict]:
    """
    Return the most recent run summaries from logs/runs.jsonl.

    Args:
        client: optional, filter to runs for this client only
        limit: most recent N runs (default 5)
    """
    log_path = REPO_ROOT / "logs" / "runs.jsonl"
    if not log_path.exists():
        return []
    runs = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    if client:
        runs = [r for r in runs if r.get("client") == client]
    return runs[-limit:][::-1]  # most recent first


# ============================================================ #
# HTTP/SSE entry point for cloud deployment                    #
# ============================================================ #

# `app` is what uvicorn looks for when deployed to Railway/Fly/Render.
# For local STDIO mode (Claude desktop app), call mcp.run() in __main__.

try:
    app = mcp.sse_app()
except AttributeError:
    # Older mcp lib version — falls back to STDIO only.
    app = None


if __name__ == "__main__":
    # Local STDIO mode: pipe stdin/stdout, connect via Claude desktop app's
    # mcp config. This is how the Claude desktop client runs MCP servers.
    mcp.run()
