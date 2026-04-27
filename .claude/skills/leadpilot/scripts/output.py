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

TEMPLATE_PATH = Path(".claude/skills/leadpilot/references/output_template.json")


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


def row_for_contact(contact: dict, client: str, columns: list[str]) -> dict:
    """Map a scored contact dict to the 25-column row."""
    score = contact.get("_score", {})
    addr = contact.get("address", {})
    company = contact.get("company", {})

    full_addr_parts = [addr.get("street"), addr.get("city"), addr.get("state"),
                       addr.get("zip"), addr.get("country")]
    full_address = ", ".join([p for p in full_addr_parts if p])
    local_address = ", ".join([p for p in full_addr_parts[:-1] if p])  # without country

    return {
        "Organization": company.get("name", ""),
        "Deal Title": f"{client} - {company.get('name', '')}",
        "Industry": company.get("industry", ""),
        "Contact Name": contact.get("fullName", ""),
        "Job Title": contact.get("jobTitle", ""),
        "Email Address": contact.get("email", ""),
        "Direct Phone Number": contact.get("directPhone", ""),
        "Mobile phone": contact.get("mobilePhone", ""),
        "Contact Name (2nd)": "",
        "Job Title (2nd)": "",
        "Email Address (2nd)": "",
        "Direct Phone Number (2nd)": "",
        "Mobile phone (2nd)": "",
        "Website": company.get("website", ""),
        "Company HQ Phone": company.get("hqPhone", ""),
        "Street Address": addr.get("street", ""),
        "City": addr.get("city", ""),
        "State": addr.get("state", ""),
        "Zipcode": addr.get("zip", ""),
        "Country": addr.get("country", ""),
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
    contacts = json.loads(Path(args.input).read_text())

    # Sort high-to-low by score
    contacts.sort(key=lambda c: c.get("_score", {}).get("total", 0), reverse=True)
    rows = [row_for_contact(c, args.client, columns) for c in contacts]

    if not args.out:
        suffix = "_PARTIAL" if args.partial else ""
        fname = f"{slugify(args.client)}_{date.today().isoformat()}{suffix}.{args.format}"
        args.out = f"output/{fname}"

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.format == "csv":
        write_csv(rows, columns, out_path)
    else:
        write_xlsx(rows, columns, out_path)

    print(f"Wrote {len(rows)} rows to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
