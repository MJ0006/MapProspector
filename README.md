# MapProspector

A personal, local app to find local-business clients and run their local SEO.

It covers the feature set of LeadSnap (found by inspecting leadsnap.com's public pages with [rea](https://github.com/morluto/rea); see [docs/feature-catalog.md](docs/feature-catalog.md)). The SEO logic comes from [Local SEO Brain](https://github.com/AI-Marketing-Hub/local-seo-brain), which is vendored unmodified in `vendor/local-seo-brain`.

| Area | What you get |
|---|---|
| **Prospecting** | Lead search (Google Places) with opportunity flags · geo-grid **heat maps** with competitor "flip the map", quadrants, schedules, trends, share links, PDF · website and listing **audit** · **AI visibility** checker · **citation audit** |
| **CRM** | Contacts and timeline · Kanban pipelines · tasks and calendar · **outbox with approval** · automations (triggers, filters, waits, actions) · lead forms with spam filters · onboarding questionnaires |
| **Client SEO** | A Local SEO Brain vault per client (Draft scorecard, roadmap, weekly report) · AI posts · review replies · review requests (no gating) · photo drips · profile lock · insights and Search Console · citation profile and tracker · branded scheduled reports |
| **AI** | Every draft is written by **your Claude Code subscription** through the `claude` CLI (no API key) · *Ask* page · MCP server so Claude Code can query your data and start scans |
| **Journal** | Every job, Claude call, decision on AI drafts (used as-is / edited / rejected) and failure is recorded. Claude turns your edits into **lessons** you approve, and approved lessons are added to future drafts, so drafts improve as you use the app. |

## Run it in VS Code

Requirements: Python 3.11+, git, and [Claude Code](https://claude.com/claude-code) installed and logged in (needed for the AI features).

```bash
git clone --recurse-submodules https://github.com/MJ0006/MapProspector.git
cd MapProspector
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # then fill in the keys you have
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. Data (SQLite database, client vaults, uploads) lives in `./data`, which git ignores.

Run **one** app process (no `--workers`), so scheduled jobs don't run twice. Schedules, automation waits, profile checks and review syncs only run while the app is running.

**Access:** with no `APP_PASSWORD`, admin pages only answer requests made on this computer. Public pages (forms `/f/…`, questionnaires `/q/…`, review links `/r/…`, share links `/share/…`) always work. Before exposing the app with a tunnel so clients can open links, set `APP_PASSWORD` and `PUBLIC_BASE_URL`. Requests coming from other websites are always rejected.

If you cloned without `--recurse-submodules`, run `git submodule update --init` to fetch Local SEO Brain.

## Keys (all optional)

Every module switches itself off when its key is missing. **Settings** shows what is on.

| `.env` key | Turns on | Cost / notes |
|---|---|---|
| `GOOGLE_API_KEY` | Lead search, Places-mode heat maps, profile-lock monitoring, PageSpeed in audits | Enable *Places API (New)* (and *PageSpeed Insights API*) in Google Cloud; billed per request by Google |
| `DATAFORSEO_LOGIN` / `_PASSWORD` | True per-point Maps / local pack / organic heat maps, keyword volume/CPC/difficulty, AI Overview grid | Billed per task by DataForSEO; this is what LeadSnap's data looks like |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Publishing posts, review replies, photos, profile reverts; GBP insights; Search Console | Business Profile API access needs Google's approval; Search Console doesn't. Redirect URI: `http://127.0.0.1:8000/gbp/oauth/callback` |
| `OPENAI_API_KEY`, `PERPLEXITY_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY` | Extra engines in AI visibility | Model names can be changed with `OPENAI_MODEL`, `PERPLEXITY_MODEL`, `GEMINI_MODEL`, `XAI_MODEL` |
| `SMTP_*` | Sending approved emails and reports | Without it, approved emails wait in the Outbox for you to copy |

**With no keys at all**, the CRM, forms, automations, client vaults, reports and content tools all work. Heat maps and lead search run on clearly labelled **demo data**.

**About your Claude subscription:** the app runs `claude -p` on your machine, so drafts, citation lookups, Claude in AI visibility, and *Ask* all count toward your Claude plan's usage limits. Keep this app personal; don't host it for other people.

## Rules the app follows (from Local SEO Brain)

- Nothing goes to a cold contact without your approval. Every outgoing message has an opt-out line, and do-not-contact is respected.
- Review requests send **every** customer to the same Google review link. There is no review gating.
- No invented scores. Each grade (citations, AI visibility) shows its formula, and audits list exactly which checks ran.
- No ranking guarantees or keyword-stuffed names. EXIF geotagging is offered as metadata only, with no claim that it affects rankings.
- Google Places data is stored by `place_id` and refreshed rather than kept as a permanent copy.
- Brain plans are marked **Draft**, and every line cites its evidence.

## Claude Code integration

This repo has a `.mcp.json`, so Claude Code opened in this folder can use the `mapprospector` tools. Start Claude Code from the activated venv (`source .venv/bin/activate` first), so `python3` finds the app's dependencies; on Windows, change `python3` to `python` in `.mcp.json`. Tools:

- `list_clients`, `list_leads`, `list_scans`, `get_scan`, `compare_scans`, `start_scan`
- `list_reviews`, `get_citations`, `get_ai_visibility`, `get_client_plan`
- `list_tasks`, `create_task`
- `get_journal`, `get_lessons`: failure history and your edits to AI drafts, so agents can see what breaks before changing a workflow (see `CLAUDE.md`)

Try asking: *"Which competitor owns the north side for roof repair in my latest scan?"*

## Tests

```bash
python3 -m pytest -q
```

External APIs are tested against recorded responses in `tests/fixtures`, and tests never call the real `claude` CLI.

## Smoke test on your PC (live services)

These features were built against the providers' documented APIs and tested with recorded responses. They have not yet been run against the live services:

1. Add `GOOGLE_API_KEY`, then **Find leads** for "plumber" in your city. Real businesses should appear. Use **Save + heat map** and run a 3×3 Places scan.
2. With DataForSEO keys, run a 3×3 DataForSEO scan and check the pins look plausible.
3. With Claude Code logged in, open a client: **Posts → Draft**. Then **Citation audit → Run** (takes a few minutes).
4. **AI visibility**: run one check. It's slow because each engine answers 5 questions.
5. With Google OAuth set up: **Settings → Connect Google account**, then link a client's location and open **Insights**.

If an engine's model name has been retired, update it in `.env`.
