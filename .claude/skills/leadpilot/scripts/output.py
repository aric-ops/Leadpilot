"""
Output builder — writes the final 25-column file.

- Enforces exact column order from references/output_template.json
- Sorts by composite confidence score, high to low
- Confidence Score and Confidence Tier columns surface scoring per row
- NOTES column carries verification flags + score breakdown summary
- Defaults to .xlsx (matching K&K + Warrior delivery format), .csv available with --format csv

Usage:
    python -m scripts.output --input scored.json --client "Warrior Restoration" \\
        --format xlsx --out output/Warrior_Restoration_2026-04-27.xlsx

Phase 1 status: STUB-but-functional output writer. Mapping from contact dict to
the 25 columns is implemented; just needs the verified contact schema finalized.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

_DOTENV_PATH = find_dotenv(usecwd=True)
load_dotenv(_DOTENV_PATH)
REPO_ROOT = Path(_DOTENV_PATH).parent if _DOTENV_PATH else Path.cwd()

TEMPLATE_PATH = REPO_ROOT / ".claude" / "skills" / "leadpilot" / "references" / "output_template.json"


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


def load_columns() -> list[str]:
    return json.loads(TEMPLATE_PATH.read_text())["columns"]


def build_notes(contact: dict) -> str:
    """Compose the NOTES cell from verification + score breakdown."""
    score = contact.get("_score", {})
    bd = score.get("breakdown", {})
    parts = []
    if bd.get("linkedin"):
        parts.append(f"LinkedIn:{bd['linkedin']['reason']}({bd['linkedin']['pts']}pts)")
    if bd.get("website"):
        parts.append(f"Web:{bd['website']['reason']}({bd['website']['pts']}pts)")
    if bd.get("freshness"):
        parts.append(f"Freshness:{bd['freshness']['reason']}({bd['freshness']['pts']}pts)")
    if bd.get("edgar_bonus") and bd["edgar_bonus"]["pts"] > 0:
        parts.append(f"EDGAR:+{bd['edgar_bonus']['pts']}")
    return " | ".join(parts)


def _flat(v) -> str:
    """Coerce ZoomInfo's occasional list values (e.g. industry) to a string for
    the spreadsheet cell. Lists -> first element. None -> empty string."""
    if isinstance(v, list):
        return str(v[0]) if v else ""
    if v is None:
        return ""
    return str(v)


def row_for_contact(contact: dict, client: str, columns: list[str]) -> dict:
    """Map a scored, normalized contact dict to the 25-column row."""
    score = contact.get("_score", {})
    company = contact.get("company", {})

    # In our normalized schema, the contact's mailing address is the company
    # address (we don't track home addresses separately).
    parts = [company.get("street"), company.get("city"), company.get("state"),
             company.get("zipCode"), company.get("country")]
    full_address  = ", ".join([p for p in parts if p])
    local_address = ", ".join([p for p in parts[:-1] if p])  # without country

    return {
        "Organization": _flat(company.get("name")),
        "Deal Title": f"{client} - {_flat(company.get('name'))}",
        "Industry": _flat(company.get("industry")),
        "Contact Name": _flat(contact.get("fullName")),
        "Job Title": _flat(contact.get("jobTitle")),
        "Email Address": _flat(contact.get("email")),
        "Direct Phone Number": _flat(contact.get("directPhone")),
        "Mobile phone": _flat(contact.get("mobilePhone")),
        "Contact Name (2nd)": "",
        "Job Title (2nd)": "",
        "Email Address (2nd)": "",
        "Direct Phone Number (2nd)": "",
        "Mobile phone (2nd)": "",
        "Website": _flat(company.get("website")),
        "Company HQ Phone": _flat(company.get("phone")),
        "Street Address": _flat(company.get("street")),
        "City": _flat(company.get("city")),
        "State": _flat(company.get("state")),
        "Zipcode": _flat(company.get("zipCode")),
        "Country": _flat(company.get("country")),
        "Full Address": full_address,
        "Local address": local_address,
        "Confidence Score": score.get("total", 0),
        "Confidence Tier": score.get("tier", ""),
        "NOTES": build_notes(contact),
    }


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def write_csv(rows: list[dict], columns: list[str], out_path: Path) -> None:
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def write_xlsx(rows: list[dict], columns: list[str], out_path: Path) -> None:
    try:
        from openpyxl import Workbook
    except ImportError:
        print("ERROR: openpyxl not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        raise
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(columns)
    for r in rows:
        ws.append([r.get(c, "") for c in columns])
    wb.save(out_path)


def main() -> int:
    p = argparse.ArgumentParser(description="LeadPilot output builder")
    p.add_argument("--input", required=True, help="scored contacts JSON")
    p.add_argument("--client", required=True)
    p.add_argument("--format", choices=["xlsx", "csv"], default="xlsx")
    p.add_argument("--out", help="output path (defaults to output/<client>_<date>.<ext>)")
    p.add_argument("--partial", action="store_true",
                   help="mark filename as PARTIAL (e.g. credit ceiling hit mid-run)")
    args = p.parse_args()

    columns = load_columns()
    contacts = json.loads(_resolve(args.input).read_text())

    # Sort high-to-low by score
    contacts.sort(key=lambda c: c.get("_score", {}).get("total", 0), reverse=True)
    rows = [row_for_contact(c, args.client, columns) for c in contacts]

    if not args.out:
        suffix = "_PARTIAL" if args.partial else ""
        fname = f"{slugify(args.client)}_{date.today().isoformat()}{suffix}.{args.format}"
        args.out = f"output/{fname}"

    out_path = _resolve(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.format == "csv":
        write_csv(rows, columns, out_path)
    else:
        write_xlsx(rows, columns, out_path)

    print(f"Wrote {len(rows)} rows to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
