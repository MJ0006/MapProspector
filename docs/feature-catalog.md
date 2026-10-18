# MapProspector — feature catalog

Recon date: 2026-10-18. Every LeadSnap feature found by running **rea** (`inspect-web-page`, CDP, public pages only) over leadsnap.com, plus two public sources:
- their public OpenAPI spec (`app.leadsnap.com/docs/openapi.yaml`, 33 endpoints)
- their public help article on the API/MCP server

**How sure each "How LeadSnap does it" note is:**
- **[code]**: read from their client JavaScript or the public API spec.
- **[page]**: stated on their marketing pages.
- **[inferred]**: our deduction from the data shapes.

**What wasn't done:** no login, no account, and no calls to their APIs. Their free tools sit behind a Cloudflare Turnstile captcha, so we read the client code instead of calling them. Nothing of theirs (text, code, logos) is copied into this repo.

**What "Brain" means below:** the parts of `local-seo-brain` that define *how our version works*. The brain's refusal rules override LeadSnap's behaviour where they conflict.

---

## A. Prospecting and rank tracking (LeadSnap's flagship)

### A1. Geo-grid heat map scan
- **How LeadSnap does it:**
  - [page] A grid (7×7 to 13×13, 0.1–25 mi spacing, square, circle or drawn shape) is dropped over the map. A live Google search runs from every point and the top 20 results are read.
  - [page] Three surfaces: Google Maps, local pack, organic.
  - [code] One heatmap per keyword × search_type. Grid points are generated from a centre, `grid_size` and spacing in metres, optionally clipped to a polygon.
  - [code] Each point stores `rank` plus the full list of `places` found there.
  - [code] Statuses: queue → in_progress → completed/incomplete/failed.
  - [code] Heat-map colours: ≤3 green, ≤6 amber, ≤10 orange, otherwise red.
  - [inferred] Fields like `feature_id` and `google_place_serial` (CID) suggest scraped Maps results from a SERP provider, not the Places API.
- **MapProspector backend:**
  - `grid.py` generates the points (same maths: centre, size, metres, polygon clip).
  - A `rank_provider` interface has two adapters:
    - **Places API (New) Text Search** with `locationBias` at each point. Cheap and works with your key, but it approximates "a searcher standing here".
    - **DataForSEO Google Maps SERP** (optional). True per-point SERP, top 20, roughly $0.002 per point.
  - Jobs are queued and run in the background.
- **Brain:** `wiki/locations`, `wiki/keywords`, and `ingest_geogrid.py`: every scan is also exported as geo-grid CSV into the client vault, so the brain's synthesis can use it.
- **Status:** build.

### A2. Competitor table and "flip the map"
- **How LeadSnap does it:**
  - [code] Per scan, every competitor found gets: average position, top-3 %, top-3 position, market share and its position, points found in, top-20 points.
  - [code] Average rank per quadrant (NW, NE, SW, SE).
  - [code] Place details: categories, rating, reviews, photos, website, map link.
  - [page] Clicking a competitor re-colours every pin with their rank.
- **MapProspector backend:** computed from stored per-point `places` lists; nothing extra is fetched.
- **Brain:** `wiki/competitors`. The brain forbids claiming *why* a competitor outranks you, so the table shows data only and makes no causal claims.
- **Status:** build.

### A3. Rank tracking over time and schedules
- **How LeadSnap does it:**
  - [code] Schedules with pause/resume. Recurrence: daily, weekly, every 14 days, monthly on a day, or first Monday.
  - [code] Ranking change, previous ranking and change %.
  - [page] Per-keyword trend lines and new/improving/losing lists.
  - [page] Pricing is in credits: 1 credit = 1 grid point.
- **MapProspector backend:** local scheduler (APScheduler, running while the app is open) and a scan history table. Instead of credits, the app shows an estimated API cost per scan.
- **Brain:** `flows/Evidence Refresh Flow`, `deliverables/` (weekly report).
- **Status:** build.

### A4. Keyword data
- **How LeadSnap does it:** [code] Each keyword carries search volume, CPC, low/high top-of-page bid, competition, difficulty, and search intent with probability.
  - [inferred] This matches DataForSEO Labs' schema exactly.
- **MapProspector backend:** optional DataForSEO adapter. Without it, keyword ideas come from the brain prompts via `claude -p`, with no volume figures invented.
- **Brain:** `prompts/keyword-research-prompt`, `keyword-variations-for-topical-relevance-prompt`.
- **Status:** build; volume data needs a DataForSEO key.

### A5. Prospect scan with no login, shareable link and PDF
- **How LeadSnap does it:** [page] Scan any business. [code] Share link with a token and expiry, plus a PDF download.
- **MapProspector backend:** "scan any business" via Places search. The share link is a signed, local, read-only URL. PDF via print-CSS/WeasyPrint.
- **Status:** build. Share links only work while your PC is serving the app.

### A6. Free AI visibility checker
- **How LeadSnap does it:**
  - [code] `POST /api/ai-visibility` streams NDJSON: a `grid` stage (25 points of Google AI Overviews), then a `report` stage.
  - [code] The report includes:
    - score and letter grade
    - six engines (chatgpt, aio, gemini, perplexity, claude, grok)
    - per engine: appearances out of prompts, recommended count, cited count, top picks
    - a prompt × engine matrix (recommended / mentioned / absent, plus position)
    - competitors' share of voice
    - which sources get cited
    - competitor reasons quoted from the answers
    - an accuracy audit and recommendations
  - [code] Recommended = top 3, mentioned = lower down.
- **MapProspector backend:**
  - The question set is generated per category and city.
  - Engine adapters:
    - **Claude**, via `claude -p` with web search on your subscription. Works out of the box.
    - **OpenAI, Perplexity, Gemini and Grok:** optional API keys.
    - **Google AI Overviews:** optional, via a SERP API.
  - The same matrix, share-of-voice and sources maths as LeadSnap.
- **Brain:**
  - `wiki/llm-seo`, `prompts/visiblity-prompt`, `visibility-follow-up-prompt`, `freshness/2026-ai-overviews-state`, doctrine 003.
  - The brain says no one can promise AI citation, so the report says "snapshot, varies run to run" and gives no guarantees.
- **Status:** build. Only Claude works with no extra keys.

## B. Google Business Profile management

**GBP API:** for B1–B6, LeadSnap uses Google's Business Profile APIs [page]. Those need Google to approve your access. Each B feature gets two modes:
1. **Draft/manual** (works day one): the app drafts the content and you paste it into GBP.
2. **Connected** (after you get API approval): the app publishes.

### B1. AI business posts and scheduling
- **How LeadSnap does it:** [page] Prime the AI with company info → choose topic, length and tone → review, approve or edit → schedule or bulk-post.
- **MapProspector backend:** post composer using `claude -p` and brain prompts, an approval queue and a calendar.
- **Brain:** doctrine 001 (GBP representation). The brain marks fixed posting frequencies as unsupported, so the app does no "post X times a week" scoring.
- **Status:** build.

### B2. Automated review replies
- **How LeadSnap does it:** [page] Prime with brand info and tone → AI drafts → review and approve. Also review notifications and review-request sending.
- **MapProspector backend:** reviews are imported (CSV/JSON, or the connected API), replies are drafted with `claude -p`, and every reply goes through an approval queue.
- **Brain:**
  - `ingest_reviews.py` builds the review notes and leaves out reviewer names.
  - `wiki/reviews` covers the reply guidance.
  - The brain drafts replies for you to approve and never auto-posts. We keep approval on even in connected mode.
- **Status:** build.

### B3. Reputation funnels (review requests)
- **How LeadSnap does it:** [page] "Funnels" send positive and negative reviewers down different paths, so negative feedback is caught before it's published. This is **review gating**.
- **MapProspector backend:** review-request links (SMS/email/QR) that send *every* customer to the same Google review link. A separate private feedback form is optional, but it never filters who gets the review link.
- **Brain:** the brain refuses review gating (Google policy).
- **Status:** **build the compliant version only.**

### B4. Photo uploads, scheduling and geotagging
- **How LeadSnap does it:** [page] Upload, rename, rewrite EXIF GPS in bulk, then post instantly or drip over time.
- **MapProspector backend:** photo library, rename to descriptive filenames, drip schedule (draft or connected).
- **Brain:** EXIF geotagging is rated **unsupported folklore** (`exif-ranking-unsupported`). We offer GPS stamping only as a plain metadata tool, with no ranking claim.
- **Status:** build, but EXIF stamping is labelled "no ranking effect".

### B5. Profile lock (change monitoring)
- **How LeadSnap does it:**
  - [page] Locks name, closure status, phone, category, address, description, website and hours. [code] Each location has a `lock_changes` flag.
  - [inferred] It watches for Google or user-suggested edits and reverts them through the API.
- **MapProspector backend:**
  - Saves an "approved snapshot" and polls Places API details on a schedule.
  - Shows a diff and alerts on any change.
  - In connected mode it offers a one-click revert.
- **Brain:** `wiki/gbp/warning-for-gbp-changes`, doctrine 001.
- **Status:** build. Monitoring works with just a Places key.

### B6. GBP insights and performance
- **How LeadSnap does it:** [code]
  - appearances, actions and calls, each as current / prior / change %
  - Maps vs Search monthly trend
  - customer-action funnel and platform/device split
  - search-query table with branded filter
  - rule-generated recommendations with severity
- **MapProspector backend:** connected mode only (Business Profile Performance API). Before approval, import a GBP CSV export via the brain.
- **Brain:** `ingest_gbp.py`. The brain's synthesis gives evidence-state recommendations, never a made-up score.
- **Status:** build (CSV import now, API later).

### B7. Google Search Console performance
- **How LeadSnap does it:** [code] `gsc/performance` and `gsc/website-performance` endpoints.
- **MapProspector backend:** Search Console API (OAuth, free, no approval needed) with queries, clicks, impressions and CTR.
- **Brain:** `prompts/ctr-audit-prompt`, `content-prioritization-prompt`.
- **Status:** build.

### B8. Q&A management, GBP optimization and completeness
- **How LeadSnap does it:** [page] Q&A management. [code] A `profile_completion` field and a completion % per location.
- **MapProspector backend:** completeness checklist from Places details, plus category, services and description drafts.
- **Brain:** `flows/GBP Optimization Workflow`, `prompts/gbp-categories-prompt`, `gbp-services-prompt`, `gbp-description-claude-prompt-1..3`. The brain forbids keyword-stuffed names and allows only the fewest accurate categories.
- **Status:** build.

## C. Citations

### C1. Free citation audit
- **How LeadSnap does it:**
  - [code] `POST /api/citation-check` (Turnstile-protected) returns a `scan` id and is polled every 3 s.
  - [code] For each directory: status `live` / `wrong` / `missing` / `pending` / `unavailable`, a `claimable` flag and a listing URL.
  - [code] Fields checked: name, address, phone, hours, website, categories, photos and description (match / mismatch / absent / present).
  - [code] **Score = (live + 0.5 × wrong) ÷ (live + wrong + missing) × 100**, converted to a letter grade.
  - [code] `/api/reputation` adds the Google rating and review count.
- **MapProspector backend:** same scoring. Directory checks run per directory through web search or fetch adapters (as far as each site allows), with manual entry or CSV for the rest.
- **Brain:**
  - `ingest_citations.py`, `wiki/citations`, `backlinks/backlinking-local-citations`.
  - The brain refuses invented scores. This one is a transparent formula over observed listings, and we show the formula next to the grade.
- **Status:** build.

### C2. Citation distribution and sync (46 directories, $20/location)
- **How LeadSnap does it:** [code] The citation-profile schema includes `auto_sync`, `auto_sync_publisher`, `imprint`, per-channel tracking phones, `opening_hour_notes`, and so on.
  - [inferred] This matches a listings-syndication reseller (Uberall-style). LeadSnap resells that network.
- **MapProspector backend:** a "citation profile" (a single source of NAP data) plus a per-directory submission checklist and tracker. You submit manually, or plug in a paid syndication API later.
- **Brain:** the brain refuses mass low-quality submission, so the checklist covers only the directories that matter.
- **Status:** build tracker. Automatic syndication needs a paid provider.

## D. CRM (LeadSnap's original core)

### D1. Contacts and leads
- **How LeadSnap does it:** [code]
  - contact fields: name, email, phone, address, business, socials, source, tags, `is_spam` and spam reason, do-not-disturb, last-activity type/time
  - contact timeline and conversations by channel
  - companies (clients) and lead sources
- **MapProspector backend:** same data model in SQLite. Places-search prospects become leads with their audit attached.
- **Status:** build.

### D2. Sales pipeline
- **How LeadSnap does it:** [page] Multiple Kanban pipelines, custom stages, win % per stage, assigned user, reminders, automations on stage moves.
- **MapProspector backend:** Kanban (drag and drop), multiple pipelines, stage win %.
- **Status:** build.

### D3. Automations
- **How LeadSnap does it:** [page]
  - **Triggers:** new form submission, call, text, contact, deal; field updated; deal moved to stage.
  - **Filters:** field-value conditions.
  - **Actions:** condition, send email, send SMS, create contact, wait, update field, end-if, end-if-review-submitted, send questionnaire.
  - **Variables:** predefined, company and custom.
  - Links and buttons: review funnel, lead details, or custom URL.
  - History logs, duplicate, delete.
- **MapProspector backend:**
  - A rule engine with the same trigger, filter and action model, plus an action log.
  - Email via your SMTP and SMS via Twilio (optional).
  - Cold outreach is **never auto-sent**. Automations only message contacts who came to you (form, call or review request), with a compliance footer.
- **Status:** build.

### D4. Forms, lead capture and spam blocking
- **How LeadSnap does it:** [page] Website form capture, with spam filters on phone format, URLs in the message, foreign IPs, domains/emails, keywords/phrases and webhooks. [code] Form submissions have `is_spam`.
- **MapProspector backend:** embeddable form endpoint and filter rules with a spam-reason log.
- **Status:** build. Live website capture only works while the app is reachable; a tunnel can be added later.

### D5. Tasks, reminders and calendar
- **How LeadSnap does it:** [page] Tasks assigned to users, notifications (text/email/in-app), integrated calendar.
- **MapProspector backend:** tasks with due dates, a calendar view, desktop and in-app notifications.
- **Status:** build.

### D6. Client onboarding questionnaires
- **How LeadSnap does it:** [page] Custom questionnaires (multiple choice, Likert, open text), answers saved per client, sendable from automations.
- **MapProspector backend:** questionnaire builder. Answers go into the client vault.
- **Brain:** answers feed `ingest_source.py` (manual evidence) → `scaffold_vault.py` client vault. There's also a brain onboarding canvas.
- **Status:** build.

## E. Call tracking

### E1. Call tracking phone system
- **How LeadSnap does it:** [page]
  - tracking numbers, IVR menus, round robin, simultaneous ring, schedules, voicemail, recording, tags
  - block/challenge callers, browser phone, SMS
  - [code] transcripts
- **MapProspector backend:** optional Twilio module covering numbers, call-flow config, recording and transcripts (via `claude -p`). You pay Twilio directly.
- **Status:** build as an **optional module**; it needs a Twilio account.

## F. Reporting, white label and AI

### F1. Client reports
- **How LeadSnap does it:** [page] Branded reports with custom fields, call playback, scheduled sending (email, PDF, link) and a one-click approval step.
- **MapProspector backend:** report builder. Sends on a schedule through your SMTP, with an approval step before anything goes out.
- **Brain:** `synthesize_brain.py` → Health Scorecard, Action Roadmap and weekly report; `render_brain_report.py`.
- **Status:** build.

### F2. White label
- **How LeadSnap does it:** [page] Your logo, colours, login page, custom domain, branded link funnels.
- **MapProspector backend:** branding settings (logo, colours, agency name) applied to reports, share pages and review-request pages. There's no multi-tenant login, since this is a personal app.
- **Status:** build (branding only).

### F3. AI SEO agent, MCP and "chat with your rankings"
- **How LeadSnap does it:** [page] An MCP server lets ChatGPT or Claude query your data and start scans. [code] Data covered: locations, heatmaps, competitors, grids, schedules, contacts, calls, forms, GBP, citations.
- **MapProspector backend:** "Ask" panel using `claude -p` with read access to your local DB. A local **MCP server** (stdio) means Claude Code in VS Code can query and start scans too.
- **Brain:** `agents/local-seo-secretary.md`, plus all prompts as tools.
- **Status:** build.

## G. Added from local-seo-brain (not in LeadSnap)
- **G1. Prospect website audit:** technical, CTR and core-30 content checks. `prompts/technical-audit-prompt`, `ctr-audit-prompt`, `prompt-core-30-content-audit`, `flows/Property Audit Workflow`.
- **G2. Content tools:** title, meta, schema, homepage, service page and blog drafts. `prompt-generating-a-title-tag`, `prompt-generating-a-meta-description`, `schema-prompt-1`, `ai-homepage-rewrite-prompt`, `blog-post-*`.
- **G3. Evidence-cited client vault per client:** opens in Obsidian. The client's **Brain** tab runs `guide_next_action.py` (next step and notes past review), `lint_vault.py` (vault health) and `generate_vault_visuals.py` (relationship map), and shows each evidence type's age. Content tools list all 37 `wiki/prompts`. (`coverage_matrix.py` and `freshness.py` check the Brain's own knowledge base, not client vaults, so they aren't run per client.)

## Out of scope / changed
- **Review gating (B3):** replaced with a compliant review-request flow.
- **EXIF "ranking" geotagging (B4):** kept only as plain metadata, with no ranking claim.
- **Multi-tenant SaaS (agency sub-accounts, billing, mobile apps):** not needed for a personal app.

## Decisions needed from you
1. **Heat-map data source:**
   - **Places API (New):** cheaper per call with your Google key, but approximate.
   - **DataForSEO Maps SERP:** what LeadSnap appears to use. Real per-point SERP, top 20, about $0.10 per 7×7 scan. It also gives keyword volume, CPC and difficulty.
2. **Optional paid modules to enable now or later:** Twilio (calls/SMS), the GBP API (you'd need to apply to Google), and OpenAI/Perplexity/Gemini/Grok keys for AI visibility.

---

## Build status (2026-10-18)

**Legend:**
- **Built + tested**: unit or route tests plus the browser walkthrough, in demo mode.
- **Fixtures only**: the live API call is untested here because no keys are available in this environment.

| # | Feature | Status | Where |
|---|---|---|---|
| A1 | Geo-grid heat map | Built + tested; Places/DataForSEO live calls fixtures only | `app/modules/rank/` |
| A2 | Competitors, flip the map, quadrants | Built + tested | `rank/engine.py` `competitors()` |
| A3 | Rank tracking over time, schedules | Built + tested | `rank/engine.py` schedules |
| A4 | Keyword data | Built; DataForSEO fixtures only | `providers/dataforseo.py` |
| A5 | Prospect scan, share link, PDF (print) | Built + tested | `/share/scan/…` |
| A6 | AI visibility checker | Built + tested with mocked engines; live engines untested | `app/modules/aivis/` |
| B1 | AI posts + scheduling | Built + tested (draft mode); GBP API publishing fixtures only | `gbp/posts.py` |
| B2 | Review replies | Built + tested | `gbp/reviews.py` |
| B3 | Review requests (compliant, no gating) | Built + tested | `/r/{token}` |
| B4 | Photos, drip, GPS metadata (no ranking claim) | Built + tested | `gbp/photos.py` |
| B5 | Profile lock | Built + tested (diff/flow); live Places polling fixtures only | `gbp/lock.py` |
| B6 | GBP insights | Built; API parsing tested with a mocked response | `gbp/routes.py` |
| B7 | Search Console queries | Built; live API untested | `providers/gbp_api.py` |
| B8 | Completeness + optimization drafts | Built + tested | `/clients/{id}/optimize` |
| C1 | Citation audit | Built + tested with mocked Claude lookups | `app/modules/citations/` |
| C2 | Citation profile + tracker | Built + tested; no automatic syndication (needs a paid provider) | `/clients/{id}/citations` |
| D1–D6 | Contacts, pipeline, automations, forms + spam, tasks, questionnaires | Built + tested | `app/modules/crm/` |
| E1 | Call tracking | **Not built** (deferred by you; needs Twilio) | — |
| F1 | Client reports | Built + tested | `app/modules/reports/` |
| F2 | White label (branding) | Built (settings → branding) | `/settings` |
| F3 | MCP server + Ask | Built + tested (protocol + tools) | `app/mcp_server.py`, `.mcp.json` |
| G1–G3 | Website audit, content tools, client vaults | Built + tested; brain's own tests pass on the pinned submodule | `app/modules/audit/`, `app/brain.py` |
