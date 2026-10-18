# MapProspector

A personal, local app to find local-business clients and run their local SEO.

**[⬇ Download for Windows (latest)](https://github.com/MJ0006/MapProspector/releases/latest/download/MapProspector-Setup.exe)**: the newest installer, rebuilt and tested after every change to `main`. Sign in to GitHub first (the repo is private). Install over the old version; your data is kept.

It covers the feature set of LeadSnap (found by inspecting leadsnap.com's public pages with [rea](https://github.com/morluto/rea); see [docs/feature-catalog.md](docs/feature-catalog.md)). The SEO logic comes from [Local SEO Brain](https://github.com/AI-Marketing-Hub/local-seo-brain), which is vendored unmodified in `vendor/local-seo-brain`.

| Area | What you get |
|---|---|
| **Prospecting** | Lead search (Google Places) with opportunity flags · geo-grid **heat maps** with competitor "flip the map", quadrants, schedules, trends, share links, PDF · website and listing **audit** · **AI visibility** checker · **citation audit** |
| **CRM** | Contacts and timeline · Kanban pipelines · tasks and calendar · **outbox with approval** · automations (triggers, filters, waits, actions) · lead forms with spam filters · onboarding questionnaires |
| **Client SEO** | A Local SEO Brain vault per client (Draft scorecard, roadmap, weekly report) · **Brain** tab: next action, evidence age, vault health, relationship map · all 37 Brain prompts in Content tools · AI posts · review replies · review requests (no gating) · photo drips · profile lock · insights and Search Console · citation profile and tracker · branded scheduled reports |
| **AI** | Drafts are written with **your own subscriptions**: Claude (Claude Code), ChatGPT (Codex CLI), Gemini (Gemini CLI), or **local models** (Ollama), in an order you choose. When one hits its limit the next takes over · *Ask* page · MCP server so Claude Code can query your data and start scans |
| **Journal** | Every job, AI call (with the platform that answered), decision on AI drafts (used as-is / edited / rejected) and failure is recorded. The AI turns your edits into **lessons** you approve, and approved lessons are added to future drafts on every platform, so drafts improve as you use the app. |

**New to the app?** Open **Guide** in the app's menu (`/guide`). It explains every feature with screenshots, and has click-by-click steps for each API key and AI platform.

## Run it in VS Code

Requirements: Python 3.11+, git, and at least one AI platform for the AI features (see [AI platforms](#ai-platforms)).

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

## Windows app

`desktop/` turns MapProspector into a normal Windows app with a Start-menu shortcut. It is the same app with every feature: the installer carries its own Python and starts the server for you.

**Get the installer:** click **[Download for Windows (latest)](https://github.com/MJ0006/MapProspector/releases/latest/download/MapProspector-Setup.exe)** (also under **Releases** on the repo's front page) and run `MapProspector-Setup.exe`. Every green build of `main` replaces it, so that link always gives the newest tested version. Builds of other branches are under **Actions → Windows app → a run → Artifacts**. The installer isn't code-signed, so Windows SmartScreen shows a warning the first time: click **More info → Run anyway**. Pushing a tag like `desktop-v1.0.0` also puts the installer on a GitHub Release.

- **AI:** install at least one [AI platform](#ai-platforms) on the laptop and sign in (Settings → AI platforms → **Sign in** opens it for you). The app finds the tools on PATH or in their usual install folders.
- **Keys:** paste them on **Settings → API keys** and press **Save keys**; they work right away. (Or **File → Open the .env file** to edit it directly, then **File → Restart server**.)
- **Your data** (database, client vaults, uploads, `.env`) lives in `%APPDATA%\MapProspector`. Updating or uninstalling the app leaves it in place. To bring data over from a VS Code checkout, quit the app and copy the contents of `./data` into `%APPDATA%\MapProspector\data`.
- **Closing the window** keeps MapProspector in the system tray, so schedules, automations and review syncs keep running. Right-click the tray icon to quit, or turn on **Start when I sign in to Windows**.
- Only one copy runs at a time. It uses port 8000 (the Google OAuth redirect above) unless something else is using that port. Don't run the VS Code server and the app at the same time.
- Google sign-in, business websites and Maps links open in your normal browser. Google blocks sign-in inside apps.
- Problems: **File → Open server log**.

To try the desktop shell from a checkout: `cd desktop && npm install && npm start`. It uses your own Python; set `MAPPROSPECTOR_PYTHON` to your venv's `python.exe`. The shell is based on electron-starter-template, which is licensed for a single product (see `desktop/TEMPLATE-LICENSE`). Keep this repo private.

## Keys (all optional)

Step-by-step setup for each key is in the in-app **Guide** (`/guide`, section 3).

Every module switches itself off when its key is missing. **Settings** shows what is on.

| `.env` key | Turns on | Cost / notes |
|---|---|---|
| `GOOGLE_API_KEY` | Lead search, Places-mode heat maps, profile-lock monitoring, PageSpeed in audits | Enable *Places API (New)* (and *PageSpeed Insights API*) in Google Cloud; billed per request by Google |
| `DATAFORSEO_LOGIN` / `_PASSWORD` | True per-point Maps / local pack / organic heat maps, keyword volume/CPC/difficulty, AI Overview grid | Billed per task by DataForSEO; this is what LeadSnap's data looks like |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Publishing posts, review replies, photos, profile reverts; GBP insights; Search Console | Business Profile API access needs Google's approval; Search Console doesn't. Redirect URI: `http://127.0.0.1:8000/gbp/oauth/callback` |
| `SMTP_*` | Sending approved emails and reports | Without it, approved emails wait in the Outbox for you to copy |

**With no keys at all**, the CRM, forms, automations, client vaults, reports and content tools all work. Heat maps and lead search run on clearly labelled **demo data**.

## AI platforms

Drafts, *Ask* and citation lookups use your own subscriptions through each company's official command-line tool, or a model running on your computer. You sign in once in that tool, in your browser. MapProspector never sees your passwords and needs no API key for these.

| Platform | Install | Sign in | Good for |
|---|---|---|---|
| Claude | [Claude Code](https://claude.com/claude-code) | run `claude` once | everything |
| ChatGPT | `npm install -g @openai/codex` | `codex login` (Sign in with ChatGPT) | everything |
| Gemini | `npm install -g @google/gemini-cli` | run `gemini` once, choose *Login with Google* | everything |
| Local | [Ollama](https://ollama.com), then e.g. `ollama pull llama3.1` | none | drafts only (no web search, no app data), no limits |

**Settings → AI platforms** sets the order and turns platforms on or off. It also picks your local model and has a **Sign in** button that opens the platform's login in a terminal.

The first ready platform answers. If it reports a usage limit, it's skipped until its reset time (or for an hour). If it isn't signed in, it's skipped for 30 minutes. The next platform answers instead, and the notice bar says so, for example *"Claude usage limit reached: using ChatGPT until 15:00"*. Every switch is in the Journal.

Citation lookups need web search, so they skip Ollama. The *Ask* page reads your data through **read-only** tools, so no platform can start scans or change anything. *AI visibility* asks Claude, ChatGPT and Gemini through these same subscriptions (no API keys). It measures each engine's own answers, so it never falls back.

If a tool is installed somewhere unusual, set `CLAUDE_PATH`, `CODEX_PATH` or `GEMINI_PATH` in `.env`. Ollama on another address: `OLLAMA_HOST`.

Every call counts toward that subscription's limits, and each company sets its own terms. Keep this app personal; don't host it for other people.

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

## Guide screenshots

`scripts/guide_screenshots.py` runs the app on demo data and refreshes the images in `app/static/guide/`. Re-run it after UI changes; it needs Playwright with Chromium, which is a dev-only tool.

## Tests

```bash
python3 -m pytest -q
```

External APIs are tested against recorded responses in `tests/fixtures`, and tests never call a real AI (`claude`, `codex` and `gemini` are fake programs in the tests, and Ollama is a fake server).

## Smoke test on your PC (live services)

These features were built against the providers' documented APIs and tested with recorded responses. They have not yet been run against the live services:

1. Add `GOOGLE_API_KEY`, then **Find leads** for "plumber" in your city. Real businesses should appear. Use **Save + heat map** and run a 3×3 Places scan.
2. With DataForSEO keys, run a 3×3 DataForSEO scan and check the pins look plausible.
3. With Claude Code logged in, open a client: **Posts → Draft**. Then **Citation audit → Run** (takes a few minutes).
4. **AI visibility**: run one check. It's slow because each engine answers 5 questions.
5. With Google OAuth set up: **Settings → Connect Google account**, then link a client's location and open **Insights**.

If an engine's model name has been retired, update it in `.env`.
