---
name: leadpilot
description: Run autonomous, self-correcting B2B lead discovery for Harbinger Marketing partners. Use this skill whenever the user wants to find leads, build a prospect list, generate verified contacts, or "run LeadPilot" for a client. Triggers on phrases like "find me X [titles] at [industry/geo]", "build a prospect list", "run leadpilot for [client]", "I need 25 leads for [partner]", or any request to discover and verify B2B contacts via ZoomInfo + Firecrawl + Bright Data + SEC EDGAR. Output is a 25-column XLSX/CSV ranked by data-accuracy confidence.
---

# LeadPilot

Autonomous lead discovery skill. The flow is: discover real local companies via Firecrawl → match them to ZoomInfo → enrich verified matches → triple-verify each contact → score for accuracy → write a ranked 25-column file.

## Core principle

**Search is free. Enrich is not.** Iterate aggressively on Search Contacts (free) before spending a single credit on Enrich Contact. Never guess coded values — always call Lookup Data first.

## Run flow

When the user asks to run a job, follow these steps in order. Stop and prompt the user wherever the flow says "ASK USER".

### 1. Parse the request

Extract from the user's message:
- **Client name** (the partner this list is for, e.g. "Warrior Restoration")
- **Target count** (e.g. 25)
- **Job titles** (e.g. "facility directors")
- **Industry** (e.g. "healthcare")
- **Geography** (e.g. "300-mile radius of Atlanta, Georgia")
- **Employee band** (e.g. "250+")
- **Revenue band** (e.g. "$5M+")

If any of these are missing, ASK USER before proceeding.

### 2. ASK USER — title fallback policy

**Always prompt before running the search.** The user wants to control fallbacks per job. Ask:

> "Two questions before I run:
> 1. **Job-title fallbacks** — if exact-match '<title>' doesn't yield <N> contacts, what other titles should I try? (e.g. for 'Facility Director': 'Facilities Manager', 'Director of Operations'). Or should I stick to the exact title only?
> 2. **Stop condition** — if I can't hit <N> with the title list above, should I: (a) keep broadening geography until I do, (b) deliver what I have, or (c) stop and check in with you?"

Wait for the user's reply before continuing.

### 3. Auth + credit check

Run `python -m scripts.auth --check` — this confirms ZoomInfo OAuth works and caches a token.
Run `python -m scripts.credit_monitor --check` — this calls `/usage` and reports balance. If under 10% of annual pool remains, halt and warn the user.

### 4. Lookup Data → ICP filter object

Run `python -m scripts.icp_parser --industry "<industry>" --employees "<band>" --revenue "<band>" --location "<geo>" --titles "<title list>"`.
This calls ZoomInfo Lookup Data, builds the filter JSON, and prints it back. **Show the parsed filter to the user and ask them to confirm before searching.**

### 5. Stage A — Firecrawl local company discovery

Run `python -m scripts.search_loop --stage A --filter <filter.json> --target <N>`.
Discovers real companies in the target geography from Firecrawl. Outputs a deduplicated list of `{name, address, website}`.

### 6. Stage B — ZoomInfo match + enrich

Run `python -m scripts.search_loop --stage B --companies <stageA.json>`.
- Calls Search Companies for each local company (free)
- Calls Search Contacts with the title filter at each matched company (free, iterates aggressively)
- Calls Enrich Contact in batches of 25 only on verified matches (credits spent here)

### 7. Stage C — broaden if short

If contact count is below `target`, run `python -m scripts.search_loop --stage C --tried <tried.json> --target <N>`.
Broadens by: title fallbacks → geography expansion (county → adjacent counties → state region) → HQ contacts as last resort (flagged for manual review).
**Respects the stop-condition the user set in step 2.**

### 8. Verify

Run `python -m scripts.verify --input <enriched.json>`.
Per contact: Bright Data LinkedIn (Tier A if `externalUrls` has LinkedIn URL, else Tier B name-based), Firecrawl website team-page scrape, SEC EDGAR (public companies only).

### 9. Score

Run `python -m scripts.score --input <verified.json>`.
Composite 0–100 confidence score. Tags HIGH (80+) / MEDIUM (60–79) / LOW (40–59) / DISCARD (<40). DISCARD rows are dropped.

### 10. Output

Run `python -m scripts.output --input <scored.json> --client "<client>" --format xlsx`.
Writes 25-column file sorted by confidence DESC to `output/<client>_<YYYY-MM-DD>.xlsx`.

### 11. Run log

Run `python -m scripts.run_logger --finalize`.
Writes summary to `logs/runs.jsonl`.

### 12. Report to user

Print a summary like:
> Done. 25 contacts delivered to `output/Warrior_Restoration_2026-04-27.xlsx`.
> Credits used: 73 (balance: 9,427).
> Tier distribution: 14 HIGH, 8 MEDIUM, 3 LOW.
> Tier A LinkedIn hits: 19/25 (76%). Tier B fallback: 6/25.

## When things break

- **Auth fails** → halt. Check `.env` has `ZOOMINFO_USERNAME`, `ZOOMINFO_CLIENT_ID`, `ZOOMINFO_PRIVATE_KEY_PATH` set and the private key file exists.
- **Credit ceiling hit mid-run** → stop, write partial output to `output/<client>_<date>_PARTIAL.xlsx`, surface clearly. Never silently truncate.
- **Bright Data snapshot stuck >2 min** → log warning, fall back to Firecrawl-only verification for that contact (LinkedIn score = 0).
- **Search Contacts returns 0 with strict filters** → loop already handles broadening per step 7. If still zero after stage C, stop and ask user.

## Safety rules

1. Never commit `.env` or any private key file.
2. Never write a partial CSV without `_PARTIAL` in the filename and a warning.
3. Never spend Enrich credits on contacts that haven't passed Search Contacts filtering.
4. Always show the parsed ICP filter to the user before the search runs.
5. Always log every iteration of the search loop — credits, results, fallbacks taken.

## Reference files

- `references/output_template.json` — locked 25-column schema
- `references/industry_codes_cache.json` — cached Lookup Data results
- `references/client_profiles.json` — per-client filter templates and confidence thresholds

## Build state

This skill is being built in phases. See `README.md` for current phase status.
- **Phase 1** — core pipeline (auth, ICP parsing, search loop, verify, output)
- **Phase 2** — confidence scorer with tiered output
- **Phase 3** — per-client tuning after 4–6 runs each
