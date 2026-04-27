"""
ICP parser — translates plain-English requests into a Search Contacts filter.

Calls ZoomInfo Lookup endpoints to convert plain-English values into the coded
values Search Contacts/Companies expect, then assembles the filter body.

Caches the lookup tables to references/industry_codes_cache.json so repeat runs
hit the cache instead of re-fetching.

Usage:
    python -m scripts.icp_parser \\
        --industry "healthcare" \\
        --employees "250+" \\
        --revenue "$5M+" \\
        --location "300mi of Atlanta, GA" \\
        --titles "Facility Director,Facilities Manager" \\
        --out output/.tmp/filter.json

    python -m scripts.icp_parser --refresh-cache    # rebuild lookup cache only

ZoomInfo Search Contacts filter fields used (subset of 89 available):
    jobTitle / exactJobTitle  — title list (OR'd) with exact-match toggle
    industryCodes             — comma list from /lookup/industry
    zipCode + zipCodeRadiusMiles  — radius search anchored on a ZIP
    state / country           — codes from /lookup/{state,country}
    metroRegion               — metro area name
    employeeRangeMin/Max      — string integers
    revenueMin/Max            — integers in THOUSANDS of USD
    managementLevel           — comma list from /lookup/managementlevel
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from ._zi_client import zi_get

_DOTENV_PATH = find_dotenv(usecwd=True)
load_dotenv(_DOTENV_PATH)
REPO_ROOT = Path(_DOTENV_PATH).parent if _DOTENV_PATH else Path.cwd()

CACHE_PATH = REPO_ROOT / ".claude" / "skills" / "leadpilot" / "references" / "industry_codes_cache.json"
CACHE_TTL_DAYS = 30

LOOKUP_PATHS = {
    "industry":        "/lookup/industry",
    "state":           "/lookup/state",
    "country":         "/lookup/country",
    "revenuerange":    "/lookup/revenuerange",
    "managementlevel": "/lookup/managementlevel",
    "department":      "/lookup/department",
    "jobfunction":     "/lookup/jobfunction",
}

# ZIP codes for common anchor cities. Used when the user types a city + state
# without a specific ZIP. Extend as new clients onboard.
CITY_ANCHOR_ZIP = {
    ("atlanta", "ga"):    "30303",
    ("newnan", "ga"):     "30263",
    ("nashville", "tn"):  "37203",
    ("birmingham", "al"): "35203",
    ("charlotte", "nc"):  "28202",
    ("orlando", "fl"):    "32801",
    ("tampa", "fl"):      "33602",
    ("dallas", "tx"):     "75201",
    ("houston", "tx"):    "77002",
    ("chicago", "il"):    "60601",
    ("new york", "ny"):   "10007",
}


# -------------- Cache management -------------- #

def _cache_load() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def _cache_fresh(cache: dict) -> bool:
    ts = cache.get("_fetched_at", 0)
    return (time.time() - ts) < (CACHE_TTL_DAYS * 86400)


def refresh_lookup_cache() -> dict:
    """Pull every lookup table and persist to references/industry_codes_cache.json."""
    cache: dict = {"_fetched_at": int(time.time())}
    for key, path in LOOKUP_PATHS.items():
        items = zi_get(path)
        cache[key] = items
        print(f"  cached {key}: {len(items)} items")
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2))
    return cache


def _ensure_cache() -> dict:
    cache = _cache_load()
    if not cache or not _cache_fresh(cache):
        print("Refreshing ZoomInfo lookup cache...", file=sys.stderr)
        cache = refresh_lookup_cache()
    return cache


# -------------- Plain-English -> coded values -------------- #

def lookup_industry(query: str, cache: dict) -> list[str]:
    """
    Return industry IDs matching the query. Always unions the literal whole-word
    match with any known synonym expansions, so 'healthcare' picks up hospitals,
    ambulance, dental, pharma, mental-health — but NOT 'hospitality' (which has
    'hospital' as a prefix, not a whole word).

    Matching uses word boundaries on both Name and Id (dots and other punctuation
    in the slug count as boundaries), so a term like 'hospital' matches
    `hospitals.hospital` (the trailing segment) but not `hospitality.lodging`.
    """
    q = query.strip().lower()

    # Each query expands to whole-word terms to match. Singular and plural are
    # both listed where ambiguity matters (hospital vs hospitality).
    synonyms = {
        "healthcare":    ["health", "healthcare", "hospital", "hospitals", "medical",
                           "pharma", "pharmaceuticals", "pharmacy", "dental", "clinic", "clinics"],
        "construction":  ["construction"],
        "manufacturing": ["manufacturing"],
        "logistics":     ["logistics", "freight", "trucking", "transport", "transportation"],
        "plumbing":      ["plumbing", "plumber", "plumbers"],
        "school":        ["school", "schools", "k-12", "education"],
        "education":     ["education", "school", "schools", "k-12", "college", "colleges", "university", "universities"],
        "real estate":   ["real estate", "realty"],
        "restoration":   ["restoration", "cleaning"],
    }
    terms = set(synonyms.get(q, [])) | {q}
    patterns = [re.compile(rf"\b{re.escape(t)}\b") for t in terms]

    hits: list[str] = []
    for item in cache.get("industry", []):
        name = item.get("Name", "").lower()
        idv = item.get("Id", "").lower()
        if any(p.search(name) or p.search(idv) for p in patterns):
            hits.append(item["Id"])
    return sorted(set(hits))


def parse_employees(spec: str) -> dict:
    """
    Parse '250+', '100-500', '<50' into {employeeRangeMin, employeeRangeMax}.
    ZoomInfo expects strings, not ints, for these two fields.
    """
    spec = spec.strip()
    out: dict[str, str] = {}
    if m := re.match(r"^\s*(\d+)\s*\+\s*$", spec):
        out["employeeRangeMin"] = m.group(1)
    elif m := re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", spec):
        out["employeeRangeMin"] = m.group(1)
        out["employeeRangeMax"] = m.group(2)
    elif m := re.match(r"^\s*<\s*(\d+)\s*$", spec):
        out["employeeRangeMax"] = m.group(1)
    elif m := re.match(r"^\s*(\d+)\s*$", spec):
        out["employeeRangeMin"] = m.group(1)
    return out


def parse_revenue(spec: str) -> dict:
    """
    Parse '$5M+', '$1M-$10M' into {revenueMin, revenueMax} in THOUSANDS USD.
    """
    spec = spec.strip().replace("$", "").replace(",", "")

    def to_thousands(s: str) -> int:
        s = s.upper()
        if s.endswith("B"):
            return int(float(s[:-1]) * 1_000_000)
        if s.endswith("M"):
            return int(float(s[:-1]) * 1_000)
        if s.endswith("K"):
            return int(float(s[:-1]))
        return int(float(s))

    out: dict[str, int] = {}
    if m := re.match(r"^([\d.]+[BMK]?)\s*\+$", spec, re.I):
        out["revenueMin"] = to_thousands(m.group(1))
    elif m := re.match(r"^([\d.]+[BMK]?)\s*-\s*([\d.]+[BMK]?)$", spec, re.I):
        out["revenueMin"] = to_thousands(m.group(1))
        out["revenueMax"] = to_thousands(m.group(2))
    return out


# ZoomInfo only accepts these specific radius values (in miles).
ALLOWED_RADII = [10, 25, 50, 100, 250]


def _clamp_radius(miles: int) -> str:
    """Round to the largest allowed radius <= requested miles, or 250 if larger."""
    valid = [r for r in ALLOWED_RADII if r <= miles]
    chosen = max(valid) if valid else min(ALLOWED_RADII)
    if chosen != miles:
        print(f"  note: requested {miles}mi clamped to {chosen}mi "
              f"(ZoomInfo allows only {ALLOWED_RADII})", file=sys.stderr)
    return str(chosen)


def parse_location(spec: str, cache: dict) -> dict:
    """
    Parse phrases like:
        '300mi of Atlanta, GA'   -> {zipCode: '30303', zipCodeRadiusMiles: '250'}
                                    (300 clamped to 250, ZoomInfo's max allowed)
        '30303, 100mi'           -> {zipCode: '30303', zipCodeRadiusMiles: '100'}
        'GA' / 'Georgia'         -> {state: 'usa.georgia'}

    Falls back to state-only filter if no anchor ZIP is known.
    """
    s = spec.strip().lower()

    # Pattern 1: "<N>mi of <city>, <ST>"
    if m := re.match(r"^(\d+)\s*mi(?:les)?\s*(?:of|from|around)?\s*(.+?),\s*([A-Za-z]{2})\b", spec, re.I):
        radius, city, state = int(m.group(1)), m.group(2).strip(), m.group(3).strip()
        zip_code = CITY_ANCHOR_ZIP.get((city.lower(), state.lower()))
        if zip_code:
            return {"zipCode": zip_code, "zipCodeRadiusMiles": _clamp_radius(radius)}
        state_id = _state_id(state, cache)
        if state_id:
            return {"state": state_id}

    # Pattern 2: explicit ZIP + radius
    if m := re.match(r"^(\d{5})\s*[, ]\s*(\d+)\s*mi(?:les)?", spec, re.I):
        return {"zipCode": m.group(1), "zipCodeRadiusMiles": _clamp_radius(int(m.group(2)))}

    # Pattern 3: bare 5-digit ZIP
    if re.match(r"^\d{5}$", s):
        return {"zipCode": s, "zipCodeRadiusMiles": "50"}

    # Pattern 4: bare 2-letter abbreviation or full state name
    if state_id := _state_id(s, cache):
        return {"state": state_id}

    return {}


def _state_id(abbr_or_name: str, cache: dict) -> str | None:
    """Resolve 'GA' or 'Georgia' to the ZoomInfo state Id like 'usa.georgia'."""
    q = abbr_or_name.strip().lower()
    abbr_map = {
        "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
        "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
        "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
        "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
        "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
        "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
        "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
        "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
        "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
        "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
        "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
        "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
        "wi": "wisconsin", "wy": "wyoming",
    }
    if len(q) == 2 and q in abbr_map:
        q = abbr_map[q]
    for item in cache.get("state", []):
        if item.get("Name", "").lower() == q or item.get("Id", "").lower() == f"usa.{q}":
            return item["Id"]
    return None


def lookup_management_level(query: str, cache: dict) -> list[str]:
    """Match plain-English management level (e.g. 'director', 'vp') to coded values."""
    q = query.strip().lower()
    return [
        item["Id"]
        for item in cache.get("managementlevel", [])
        if q in item.get("Name", "").lower() or q in item.get("Id", "").lower()
    ]


# -------------- Filter builder -------------- #

def build_filter(
    industry: str,
    employees: str,
    revenue: str,
    location: str,
    titles: list[str],
    management_level: str | None = None,
    rpp: int = 25,
) -> dict:
    """Assemble the JSON body Search Contacts expects."""
    cache = _ensure_cache()

    flt: dict = {"rpp": rpp}

    if titles:
        flt["exactJobTitle"] = " OR ".join(titles)

    industries = lookup_industry(industry, cache)
    if industries:
        flt["industryCodes"] = ",".join(industries)
    elif industry:
        flt["industryKeywords"] = industry

    flt.update(parse_employees(employees))
    flt.update(parse_revenue(revenue))
    flt.update(parse_location(location, cache))

    if management_level:
        levels = lookup_management_level(management_level, cache)
        if levels:
            flt["managementLevel"] = ",".join(levels)

    return flt


# -------------- CLI -------------- #

def main() -> int:
    p = argparse.ArgumentParser(description="LeadPilot ICP parser")
    p.add_argument("--refresh-cache", action="store_true",
                   help="refresh lookup cache only, don't build a filter")
    p.add_argument("--industry")
    p.add_argument("--employees", help='e.g. "250+", "100-500"')
    p.add_argument("--revenue", help='e.g. "$5M+"')
    p.add_argument("--location", help='e.g. "300mi of Atlanta, GA"')
    p.add_argument("--titles", help="comma-separated list of job titles")
    p.add_argument("--management-level", help="optional, e.g. 'Director'")
    p.add_argument("--rpp", type=int, default=25, help="records per page")
    p.add_argument("--out", default="output/.tmp/filter.json")
    args = p.parse_args()

    if args.refresh_cache:
        refresh_lookup_cache()
        return 0

    missing = [k for k in ("industry", "employees", "revenue", "location", "titles")
               if not getattr(args, k)]
    if missing:
        print(f"ERROR: missing required args: {', '.join(missing)}", file=sys.stderr)
        return 1

    titles = [t.strip() for t in args.titles.split(",") if t.strip()]
    flt = build_filter(
        industry=args.industry,
        employees=args.employees,
        revenue=args.revenue,
        location=args.location,
        titles=titles,
        management_level=args.management_level,
        rpp=args.rpp,
    )

    out = Path(args.out)
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(flt, indent=2))

    print(f"Wrote filter -> {out}")
    print(json.dumps(flt, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
