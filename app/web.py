"""Template rendering shared by every module."""
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db, settings

templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
templates.env.filters["loads"] = lambda v, d=None: db.loads(v, d)
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2)


def _localtime(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """UTC ISO string from the database -> this computer's local time for display."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)[:16].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)


templates.env.filters["localtime"] = _localtime


def _safe_url(value) -> str:
    """Only http(s) links from scraped data (business websites, Maps, directory listings); never javascript: etc."""
    v = str(value or "").strip()
    return v if v.lower().startswith(("http://", "https://")) else ""


templates.env.filters["safe_url"] = _safe_url

NAV = [
    ("Prospecting", [
        ("/", "Dashboard"),
        ("/leads/search", "Find leads"),
        ("/rank", "Rank tracker"),
        ("/aivis", "AI visibility"),
        ("/citations", "Citation audit"),
        ("/tools", "Content tools"),
    ]),
    ("CRM", [
        ("/crm/contacts", "Contacts"),
        ("/crm/pipeline", "Pipeline"),
        ("/crm/tasks", "Tasks"),
        ("/crm/outbox", "Outbox"),
        ("/crm/automations", "Automations"),
        ("/crm/forms", "Forms & spam"),
        ("/crm/questionnaires", "Questionnaires"),
    ]),
    ("Clients", [
        ("/clients", "Clients"),
        ("/reports", "Reports"),
        ("/ask", "Ask (AI)"),
    ]),
    ("System", [
        ("/journal", "Journal"),
        ("/guide", "Guide"),
        ("/settings", "Settings"),
    ]),
]


def row_or_404(table: str, row_id: int) -> dict:
    """The row, or a 404 page (a stale page or an old link) instead of a crash."""
    row = db.one(f"SELECT * FROM {table} WHERE id = ?", row_id)
    if not row:
        raise HTTPException(404, "Not found. It may have been deleted.")
    return row


def render(request: Request, name: str, **ctx):
    if "modules" not in ctx:
        from app import ai
        # ai: some platform can draft; ai_web: one can also search the web; ai_mcp: one can read app data (Ask)
        ctx["modules"] = {**settings.modules(), "ai": ai.available(), "ai_web": ai.available(web=True),
                          "ai_mcp": ai.available(mcp=True)}
    ctx.setdefault("brand", brand())
    ctx.setdefault("public_is_local", public_is_local())
    from app import notify
    ctx.setdefault("notices", notify.notices())
    ctx["nav"] = NAV
    ctx["path"] = request.url.path
    return templates.TemplateResponse(request, name, ctx)


def public_is_local() -> bool:
    """True when links we hand to clients/Google (PUBLIC_BASE_URL) point at this computer only."""
    host = settings.PUBLIC_BASE_URL.split("//", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].lower()
    return host in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]", "::1")


def redirect(url: str):
    return RedirectResponse(url, status_code=303)


def safe_back(url: str | None, default: str = "/") -> str:
    from app.security import safe_next
    return safe_next(url, default)


def brand() -> dict:
    return {
        "name": db.get_setting("brand_name", "MapProspector"),
        "color": db.get_setting("brand_color", "#2563eb"),
        "logo": db.get_setting("brand_logo", ""),
        "email": db.get_setting("brand_email", ""),
        "address": db.get_setting("brand_address", ""),
    }
