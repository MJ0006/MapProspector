"""MapProspector: personal local lead-finding and local SEO app.

Run:  uvicorn app.main:app --reload
"""
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.staticfiles import StaticFiles

from app import db, jobs, notify, security, settings  # notify registers its background ticks
from app.web import redirect, render, safe_back

logging.basicConfig(level=logging.INFO)

INTEGRATIONS = [
    ("places", "Google Places API (New)", "Lead search, rank scans (Places mode), profile-lock monitoring"),
    ("dataforseo", "DataForSEO", "True per-point Maps SERP scans, keyword volume / CPC / difficulty"),
    ("google_oauth", "Google OAuth (GBP + Search Console)", "Publish posts, replies, photos; GBP insights; Search Console data"),
    ("claude", "Claude Code CLI (your subscription)", "Claude engine in AI visibility (drafts: see AI platforms below)"),
    ("openai", "OpenAI", "ChatGPT engine in AI visibility"),
    ("perplexity", "Perplexity", "Perplexity engine in AI visibility"),
    ("gemini", "Google Gemini", "Gemini engine in AI visibility"),
    ("grok", "xAI Grok", "Grok engine in AI visibility"),
    ("smtp", "SMTP email", "Sending approved emails and scheduled reports"),
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    if os.environ.get("MAPPROSPECTOR_NO_JOBS") != "1":
        jobs.start()
    jobs.recover()  # after start, so recovered work runs in the background
    yield
    jobs.stop()


app = FastAPI(title="MapProspector", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).with_name("static"))), name="static")
app.middleware("http")(security.middleware)


@app.get("/login")
def login_form(next: str = "/"):
    return security.login_page(next)


@app.post("/login")
def login_submit(password: str = Form(""), next: str = Form("/")):
    return security.login(password, next)


@app.get("/")
def dashboard(request: Request):
    counts = {
        "leads": db.one("SELECT COUNT(*) n FROM contacts WHERE is_spam = 0")["n"],
        "clients": db.one("SELECT COUNT(*) n FROM companies WHERE is_client = 1")["n"],
        "scans": db.one("SELECT COUNT(*) n FROM scans WHERE status = 'completed'")["n"],
        "tasks_due": db.one("SELECT COUNT(*) n FROM tasks WHERE done = 0 AND due_at <= ?",
                            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds"))["n"],
        "outbox": db.one("SELECT COUNT(*) n FROM outbox WHERE status = 'draft'")["n"],
        "changes": db.one("SELECT COUNT(*) n FROM profile_changes WHERE status = 'open'")["n"],
        "replies": db.one("SELECT COUNT(*) n FROM reviews WHERE reply_status = 'draft'")["n"],
    }
    recent_scans = db.all("SELECT s.*, p.name business FROM scans s LEFT JOIN places p ON p.id = s.place_id "
                          "ORDER BY s.id DESC LIMIT 6")
    tasks = db.all("SELECT * FROM tasks WHERE done = 0 ORDER BY due_at IS NULL, due_at LIMIT 6")
    return render(request, "dashboard.html", title="Dashboard", counts=counts,
                  recent_scans=recent_scans, tasks=tasks)


@app.post("/notices/seen")
def notices_seen(request: Request):
    notify.mark_seen()
    return redirect(safe_back(request.headers.get("referer", "/").split(request.headers.get("host", ""), 1)[-1]))


@app.get("/settings")
def settings_page(request: Request, saved: int = 0, signin: str = "", opened: int = 1):
    from app.modules.ai_platforms.routes import card_context
    google_connected = db.one("SELECT 1 FROM oauth_tokens WHERE provider = 'google'") is not None
    return render(request, "settings.html", title="Settings", integrations=INTEGRATIONS,
                  saved=saved, google_connected=google_connected, env_file=settings.ENV_FILE,
                  signin=signin, signin_opened=opened, **card_context())


@app.post("/settings")
def settings_save(brand_name: str = Form(""), brand_color: str = Form(""), brand_logo: str = Form(""),
                  brand_email: str = Form(""), brand_address: str = Form("")):
    for key, value in [("brand_name", brand_name or "MapProspector"), ("brand_color", brand_color or "#2563eb"),
                       ("brand_logo", brand_logo), ("brand_email", brand_email), ("brand_address", brand_address)]:
        db.set_setting(key, value.strip())
    return redirect("/settings?saved=1")


def _mount_modules():
    from app.modules import routers
    for router in routers():
        app.include_router(router)


_mount_modules()
