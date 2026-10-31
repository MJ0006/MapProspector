"""Guide: how to use everything in MapProspector, with screenshots (app/static/guide, made by
scripts/guide_screenshots.py) and click-by-click steps for every key in .env.

Everything here is static text except the "Your setup" checklist, which reads what is switched on right now.
"""
from fastapi import APIRouter, Request

from app import ai, settings
from app.web import render

router = APIRouter()

# Every .env key, grouped by what it unlocks. tests/test_guide.py checks that every key in .env.example is here.
KEYS = [
    {
        "id": "google-places", "module": "places", "title": "Google Places API key",
        "keys": ["GOOGLE_API_KEY"], "optional_keys": ["PLACES_SCAN_DETAIL"],
        "unlocks": "Find leads with real businesses, heat maps in Places mode, profile-lock monitoring, and the "
                   "PageSpeed check in website audits.",
        "cost": "Google bills per request after each API's free monthly allowance. A lead search is one request; a "
                "7×7 heat map is 49. Set a budget alert in Google Cloud Billing.",
        "steps": [
            'Open <a href="https://console.cloud.google.com/" target="_blank" rel="noopener">console.cloud.google.com</a> '
            "and sign in with your Google account.",
            "At the top, click the project picker → <b>New project</b>. Name it e.g. <i>MapProspector</i> → <b>Create</b>, "
            "then select it.",
            "Left menu → <b>Billing</b> → link a billing account (Google requires one for Places, even inside the free allowance).",
            "Left menu → <b>APIs &amp; Services → Library</b>. Search <b>Places API (New)</b> → open it → <b>Enable</b>. "
            "(Pick the one that says <i>New</i>.)",
            "Back in Library, search <b>PageSpeed Insights API</b> → <b>Enable</b> (free; used by website audits).",
            "Left menu → <b>APIs &amp; Services → Credentials</b> → <b>Create credentials → API key</b>. Copy the key.",
            "Click the new key → under <b>API restrictions</b> choose <b>Restrict key</b> and tick <i>Places API (New)</i> "
            "and <i>PageSpeed Insights API</i> → <b>Save</b>. This limits the damage if the key ever leaks.",
            "Optional, cheaper heat maps: <code>PLACES_SCAN_DETAIL=lite</code> skips ratings and review counts for each "
            "grid point.",
        ],
        "env": "GOOGLE_API_KEY=AIza...your-key...",
    },
    {
        "id": "dataforseo", "module": "dataforseo", "title": "DataForSEO login",
        "keys": ["DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"],
        "unlocks": "True per-point Google Maps, local pack and organic heat maps (what LeadSnap-style tools show), "
                   "keyword volume / CPC / difficulty, and the AI Overview grid.",
        "cost": "Pay as you go, no monthly fee; a small minimum top-up (about $50 at the time of writing). Each grid "
                "point is one task, a fraction of a cent. Check dataforseo.com/pricing.",
        "steps": [
            'Sign up at <a href="https://dataforseo.com/" target="_blank" rel="noopener">dataforseo.com</a> and confirm your email.',
            "Add funds: dashboard → <b>Billing / Payments</b>.",
            "Open <b>API Access</b> in the dashboard. Your <b>API login</b> is shown there. The <b>API password</b> is not "
            "your account password: it's shown in the first 24 hours, or use the option there to email it to you.",
            "Paste both on <b>Settings → API keys</b> → <b>Save keys</b>, then pick <b>DataForSEO</b> as the provider on <b>Rank tracker → New scan</b>.",
        ],
        "env": "DATAFORSEO_LOGIN=you@example.com\nDATAFORSEO_PASSWORD=api-password-from-the-dashboard",
    },
    {
        "id": "google-oauth", "module": "google_oauth", "title": "Google sign-in (Business Profile + Search Console)",
        "keys": ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"],
        "unlocks": "Publish posts, review replies and photos to Google; sync reviews; restore profile edits; "
                   "Business Profile insights; Search Console clicks and queries.",
        "cost": "Free. Business Profile API access needs Google's approval (can take days to weeks); Search Console works "
                "without approval.",
        "steps": [
            "Use the same Google Cloud project as your Places key (or make one).",
            "<b>Ask for Business Profile API access:</b> Google's <i>Business Profile API</i> access form (search "
            "&ldquo;GBP API contact form&rdquo;), from the Google account that owns or manages the profiles, with your "
            "Cloud <b>project number</b> (Cloud console home page). Until approved, the API's quota shows <b>0</b>.",
            "<b>APIs &amp; Services → Library</b>: enable <b>Google Search Console API</b> now. After approval, also enable "
            "<b>My Business Account Management API</b>, <b>My Business Business Information API</b> and "
            "<b>Business Profile Performance API</b>.",
            "<b>APIs &amp; Services → OAuth consent screen</b> (or <i>Google Auth Platform</i>): app name "
            "<i>MapProspector</i>, your email as support and developer contact, audience <b>External</b>. Add your own "
            "Google account under <b>Test users</b>.",
            "<b>Credentials → Create credentials → OAuth client ID</b> → type <b>Web application</b>. Under "
            "<b>Authorized redirect URIs</b> add exactly <code>http://127.0.0.1:8000/gbp/oauth/callback</code> → "
            "<b>Create</b>. Copy the client ID and secret.",
            "Paste them on <b>Settings → API keys</b> → <b>Save keys</b>, then <b>Settings → Connect Google account</b>. Sign-in opens in "
            "your browser (Google doesn't allow it inside apps).",
            "Then open a client → <b>Overview</b> → <b>Pick from your Google account</b> to link its Business Profile location and Search Console site.",
            "While the app is in <i>Testing</i> on the consent screen, Google expires the sign-in after 7 days and "
            "MapProspector asks you to reconnect. Publishing the consent screen avoids that.",
        ],
        "env": "GOOGLE_OAUTH_CLIENT_ID=1234567890-abc.apps.googleusercontent.com\nGOOGLE_OAUTH_CLIENT_SECRET=GOCSPX-...",
    },
    {
        "id": "smtp", "module": "smtp", "title": "Email sending (SMTP)",
        "keys": ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"],
        "unlocks": "Approved emails, review requests and scheduled reports are sent for you. Without it they wait in "
                   "the Outbox to copy into your own email.",
        "cost": "Free with your existing email account (watch your provider's daily sending limit).",
        "steps": [
            "<b>Gmail / Google Workspace:</b> turn on 2-Step Verification for the account, then open "
            '<a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">myaccount.google.com/apppasswords</a>, '
            "create an app password named <i>MapProspector</i> and copy the 16 letters.",
            "Use host <code>smtp.gmail.com</code>, port <code>587</code>, your full address as user and from, and the app "
            "password as <code>SMTP_PASSWORD</code>.",
            "<b>Outlook / Microsoft 365:</b> host <code>smtp.office365.com</code>, port 587 (SMTP sending must be allowed on "
            "the mailbox). <b>Other providers:</b> use the SMTP settings from their help pages (port 587 with STARTTLS).",
            "Before approving cold emails, add your postal address under <b>Settings → Branding</b> (the law requires it).",
        ],
        "env": "SMTP_HOST=smtp.gmail.com\nSMTP_PORT=587\nSMTP_USER=you@gmail.com\nSMTP_PASSWORD=abcd efgh ijkl mnop\n"
               "SMTP_FROM=you@gmail.com",
    },
    {
        "id": "sharing", "module": None, "title": "Sharing links with clients (password + public address)",
        "keys": ["APP_PASSWORD", "PUBLIC_BASE_URL"],
        "unlocks": "Clients can open forms, questionnaires, review links, share links and reports from their own "
                   "devices, and Google can fetch photos you publish.",
        "cost": "Free with a tunnel such as Cloudflare Tunnel or ngrok.",
        "steps": [
            "Set <code>APP_PASSWORD</code> first. Then admin pages ask for it from anywhere except this computer, while "
            "public pages (forms, questionnaires, review links, share links) still work.",
            "Start a tunnel to <code>http://127.0.0.1:8000</code> (for example <code>cloudflared tunnel --url "
            "http://127.0.0.1:8000</code>) and copy the https address it gives you.",
            "Set <code>PUBLIC_BASE_URL</code> to that address, so links in emails and reports use it.",
            "Without a tunnel, links only work on this computer; the app warns you on pages that make client links.",
        ],
        "env": "APP_PASSWORD=pick-a-long-password\nPUBLIC_BASE_URL=https://your-tunnel.example.com",
    },
    {
        "id": "postgres", "module": None, "title": "PostgreSQL copy (optional)",
        "keys": ["POSTGRES_URL"],
        "unlocks": "An hourly one-way copy of all app data into your own PostgreSQL, to query with pgAdmin, DBeaver or SQL. "
                   "The app keeps running on its own SQLite file either way.",
        "cost": "Free (your own PostgreSQL).",
        "steps": [
            "In pgAdmin or <code>psql</code>, create an empty database, for example <code>CREATE DATABASE mapprospector;</code>",
            "Set <code>POSTGRES_URL</code> to <code>postgresql://USER:PASSWORD@localhost:5432/mapprospector</code> with your "
            "Postgres user and password.",
            "Press <b>Copy now</b> under <b>Settings → Copy to PostgreSQL</b>. Tables appear in the <code>mapprospector</code> schema.",
        ],
        "env": "POSTGRES_URL=postgresql://postgres:your-password@localhost:5432/mapprospector",
    },
    {
        "id": "advanced", "module": None, "title": "Advanced settings (usually leave empty)",
        "keys": ["CLAUDE_PATH", "CODEX_PATH", "AGY_PATH", "OLLAMA_HOST", "DATA_DIR"],
        "unlocks": "Only needed when the app can't find an AI tool by itself, Ollama runs elsewhere, or (outside the "
                   "Windows app) you want the data in another folder.",
        "cost": "",
        "steps": [
            "<code>CLAUDE_PATH</code> / <code>CODEX_PATH</code> / <code>AGY_PATH</code>: the full path to "
            "<code>claude</code>, <code>codex</code> or <code>agy</code> if Settings says &ldquo;not installed&rdquo; "
            "although it is (find it with <code>where codex</code> in a terminal).",
            "<code>OLLAMA_HOST</code>: Ollama's address if it isn't <code>127.0.0.1:11434</code>.",
            "<code>DATA_DIR</code>: where the database and vaults live when running from VS Code (the Windows app always "
            "uses <code>%APPDATA%\\MapProspector</code>).",
        ],
        "env": "",
    },
]

AI_STEPS = [
    {"name": "claude", "title": "Claude (your Claude subscription)",
     "steps": ['Install Claude Code from <a href="https://claude.com/claude-code" target="_blank" rel="noopener">claude.com/claude-code</a> '
               "(the Windows installer, or <code>npm install -g @anthropic-ai/claude-code</code>).",
               "Open a terminal, run <code>claude</code> once and sign in with your Claude account."]},
    {"name": "codex", "title": "ChatGPT (your ChatGPT Plus / Pro plan)",
     "steps": ['Install <a href="https://nodejs.org/" target="_blank" rel="noopener">Node.js</a> (LTS) if you don\'t have it.',
               "In a terminal: <code>npm install -g @openai/codex</code>.",
               "Settings → AI platforms → ChatGPT → <b>Sign in</b> (or run <code>codex login</code>) and choose "
               "<b>Sign in with ChatGPT</b> in the browser."]},
    {"name": "gemini", "title": "Gemini (Antigravity CLI, sign in with Google)",
     "steps": ["Install Antigravity CLI. In Windows PowerShell: "
               "<code>irm https://antigravity.google/cli/install.ps1 | iex</code> (macOS/Linux: "
               "<code>curl -fsSL https://antigravity.google/cli/install.sh | bash</code>).",
               "Settings → AI platforms → Gemini → <b>Sign in</b> (or run <code>agy</code>) and choose "
               "<b>Google OAuth</b>. Gemini CLI no longer works with personal Google accounts."]},
    {"name": "ollama", "title": "Local models (Ollama)",
     "steps": ['Install Ollama from <a href="https://ollama.com/" target="_blank" rel="noopener">ollama.com</a>.',
               "In a terminal: <code>ollama pull llama3.1</code> (or another model that fits your PC).",
               "Settings → AI platforms shows it as ready; pick the model there. Local models write drafts only "
               "(no web search, no app data), with no limits and no cost."]},
]


def setup_checklist() -> list[dict]:
    mods = settings.modules()
    platforms = {p["name"]: p for p in ai.status()}
    items = []
    ready_ai = [p["label"] for p in platforms.values() if p["on"] and p["installed"] and not p["paused"]
                and p["signed_in"] is not False]
    items.append({"title": "An AI platform for drafts", "on": bool(ready_ai), "href": "#ai",
                  "detail": ", ".join(ready_ai) if ready_ai else "none ready yet"})
    for k in KEYS:
        if k["module"]:
            items.append({"title": k["title"], "on": mods.get(k["module"], False), "href": f"#{k['id']}",
                          "detail": "on" if mods.get(k["module"]) else "not set"})
    from app.providers import ai_engines
    engines = [ai_engines.ENGINES[e] for e in ai_engines.available()]
    items.append({"title": "AI visibility engines", "on": bool(engines), "href": "#ai",
                  "detail": ", ".join(engines) if engines else "none ready (uses your AI platforms)"})
    items.append({"title": "Sharing links with clients", "on": not settings.PUBLIC_BASE_URL.startswith(("http://127.", "http://localhost")),
                  "href": "#sharing", "detail": settings.PUBLIC_BASE_URL})
    return items


@router.get("/guide")
def guide(request: Request):
    return render(request, "guide.html", title="Guide", keys=KEYS, ai_steps=AI_STEPS, checklist=setup_checklist(),
                  env_file=settings.ENV_FILE)
