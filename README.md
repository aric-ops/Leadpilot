# LeadPilot

Autonomous, self-correcting lead discovery for Harbinger Marketing partners. Implemented as a Claude Code skill that orchestrates ZoomInfo (direct API), Firecrawl, Bright Data, and SEC EDGAR into a single pipeline that delivers a verified, confidence-ranked 25-column XLSX per client.

## What it does

1. Discovers real local companies in the target geography via Firecrawl (Chamber listings, association directories, association member pages).
2. Matches them to ZoomInfo records (free Search Companies + Search Contacts endpoints) so the search iterates aggressively before any credits are spent.
3. Spends Enrich Contact credits **only** on verified matches (batches of 25).
4. Triple-verifies every contact: Bright Data LinkedIn (Tier A direct URL preferred, Tier B name-based fallback), Firecrawl team-page scrape, SEC EDGAR for public companies.
5. Scores each contact 0–100 across LinkedIn / website / freshness / completeness / EDGAR-bonus and tags HIGH / MEDIUM / LOW.
6. Writes a 25-column XLSX sorted by confidence with a per-row score breakdown in NOTES.

Full design brief: see commit history / project docs.

---

## Repo layout

```
Leadpilot/
├── .claude/
│   └── skills/
│       └── leadpilot/
│           ├── SKILL.md              ← orchestrator (Claude reads this)
│           ├── scripts/              ← Python modules Claude invokes
│           │   ├── auth.py
│           │   ├── credit_monitor.py
│           │   ├── icp_parser.py
│           │   ├── search_loop.py
│           │   ├── verify.py
│           │   ├── score.py
│           │   ├── output.py
│           │   └── run_logger.py
│           ├── references/           ← static config / caches
│           │   ├── output_template.json   ← locked 25-column schema
│           │   ├── industry_codes_cache.json
│           │   └── client_profiles.json
│           └── requirements.txt
├── output/                           ← gitignored, per-client deliverables
├── logs/                             ← gitignored, run logs + credit tally
├── .env.example                      ← template for secrets
├── .gitignore
└── README.md                         ← this file
```

---

## Setup (Mac, one-time)

```bash
# 1. Clone
git clone https://github.com/aric-ops/Leadpilot.git
cd Leadpilot

# 2. Python deps (use a venv if you prefer)
pip install -r .claude/skills/leadpilot/requirements.txt

# 3. Secrets — copy the template and fill it in
cp .env.example .env
#   then edit .env in your editor and paste in real values

# 4. Save your ZoomInfo private key (.pem file) at the path you set in .env
#    Default path: ./zoominfo_private_key.pem
```

Once `.env` is filled in, the keys persist forever — no need to re-enter on subsequent runs.

---

## Setup (claude.ai/code, for phone editing)

1. Open https://claude.ai/code from your phone or laptop browser.
2. Connect your GitHub account if you haven't already.
3. Open the `aric-ops/Leadpilot` repo.
4. In the workspace settings, add the same env vars from `.env.example` to the secrets panel.
5. You can now edit `SKILL.md` and the Python modules from your phone, commit, and push.

---

## How to run a job

Inside Claude Code (Mac terminal, or claude.ai/code), just ask in plain English:

> "Run LeadPilot for Warrior Restoration. 25 facility directors within 300 miles of Atlanta GA, healthcare industry, 250+ employees, $5M+ revenue."

Claude will read `SKILL.md`, prompt you for title fallbacks and stop-condition, then walk through the 12 steps in the run flow. Output lands in `output/<client>_<date>.xlsx`.

---

## Running as an MCP server (for Cowork / Claude desktop app / phone)

The same logic that powers the Claude Code skill is also exposed as an MCP server in `mcp_server.py`. This lets you call LeadPilot from any MCP client — Cowork, the Claude desktop app, the Claude phone app, other Claude Code instances.

### Tools exposed

| Tool | What it does |
|---|---|
| `check_credits` | Current ZoomInfo credit balance + halt/warn status |
| `parse_icp` | Plain-English ICP → Search Contacts filter |
| `search_and_enrich` | Stage B only (search + enrich, no verify/score) |
| `verify_contacts` | Run triple-verification on existing enriched contacts |
| `run_full_job` | End-to-end: ICP → search → verify → score → XLSX |
| `get_run_summary` | Read the most recent runs from `logs/runs.jsonl` |

### Deploy to Railway (5 minutes, free tier)

1. Sign up at **railway.app** with GitHub
2. **New Project** → **Deploy from GitHub** → pick `aric-ops/Leadpilot`
3. **Variables** tab — paste the same env vars from `.env`:
   - `ZOOMINFO_USERNAME`, `ZOOMINFO_CLIENT_ID`
   - `ZOOMINFO_PRIVATE_KEY` — paste the **full PEM contents**, not the path
   - `BRIGHTDATA_API_KEY`, `FIRECRAWL_API_KEY`, `SEC_EDGAR_USER_AGENT`
4. **Settings** → **Networking** → **Generate Domain** to get a public URL like `https://leadpilot-production.up.railway.app`
5. Confirm it's running by visiting `<your-url>/sse` — you should see "Server-Sent Events" or similar

### Connect to Cowork

1. In Cowork, go to **Settings** → **Connectors** → **Add MCP Server**
2. Paste the Railway URL with `/sse` appended (e.g. `https://leadpilot-production.up.railway.app/sse`)
3. Save. Cowork will list the 6 tools above.
4. From any chat in Cowork, ask: *"Run LeadPilot for [Client]. 25 facility directors, healthcare 250+ employees, $5M+ revenue, 300mi of Atlanta GA."* — Cowork calls `run_full_job`, returns the XLSX path.

### Connect to Claude desktop app

1. Open the Claude desktop app → **Settings** → **Connectors** → **Add MCP server**
2. Paste the same Railway URL with `/sse`
3. Same prompt works in any chat.

### Local-only mode (Claude desktop app talking to your Mac)

If you'd rather keep everything on your Mac (no Railway, no public URL):

1. `pip install mcp uvicorn` (requires Python 3.10+ — install via `brew install python@3.11` if you only have 3.9)
2. Add to your Claude desktop app's MCP config:
   ```json
   {
     "mcpServers": {
       "leadpilot": {
         "command": "python3",
         "args": ["/Users/aricgorman/Desktop/Leadpilot/mcp_server.py"]
       }
     }
   }
   ```
3. Restart the desktop app. Tools appear automatically.

This mode uses STDIO instead of HTTP, which means your `.env` and `.pem` stay local — no secrets uploaded anywhere.

---

## Build phases

- **Phase 1 — core pipeline.** Auth, ICP parsing, search loop (Firecrawl + ZoomInfo), enrich, verification stack, output. Replaces current Cowork behaviour with cleaner delivery. *In progress — modules are stubbed; implementation pending.*
- **Phase 2 — confidence scorer.** Tier breakdown columns and per-row NOTES with score reasoning. *Stubbed.*
- **Phase 3 — per-client tuning.** After 4–6 runs per client, review which tiers actually convert and adjust scoring weights or thresholds in `client_profiles.json`.

Module-level status is in each script's docstring header.

---

## Design principles

1. **Search is free, enrich is not.** Iterate aggressively on Search Contacts before spending Enrich credits.
2. **Lookup before Search.** Never guess coded values; always call Lookup Data first.
3. **Prefer the LinkedIn URL ZoomInfo already gave us.** Tier A verification is cheaper and more accurate than name-based searches.
4. **Verify before output.** No contact lands in the file without passing the verification stack.
5. **Score for accuracy, not enthusiasm.** Confidence reflects whether the data is real and current. Buyer-intent scoring is out of scope for v2.
6. **Log everything.** Every loop iteration, every credit spent, every fallback taken.
7. **Fail loud, not silent.** Credit ceiling or auth failure → halt and warn. Partial files always carry `_PARTIAL` in the filename.
