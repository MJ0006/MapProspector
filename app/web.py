"""Template rendering shared by every module."""
import json
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db, settings

templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
templates.env.filters["loads"] = lambda v, d=None: db.loads(v, d)
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2)

NAV = [
    ("Prospecting", [
        ("/", "Dashboard"),
        ("/leads/search", "Find leads"),
        ("/rank", "Rank tracker"),
        ("/aivis", "AI visibility"),
        ("/citations", "Citation audit"),
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
    ]),
    ("System", [
        ("/settings", "Settings"),
    ]),
]


def render(request: Request, name: str, **ctx):
    ctx.setdefault("modules", settings.modules())
    ctx.setdefault("brand", brand())
    ctx["nav"] = NAV
    ctx["path"] = request.url.path
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str):
    return RedirectResponse(url, status_code=303)


def brand() -> dict:
    return {
        "name": db.get_setting("brand_name", "MapProspector"),
        "color": db.get_setting("brand_color", "#2563eb"),
        "logo": db.get_setting("brand_logo", ""),
        "email": db.get_setting("brand_email", ""),
        "address": db.get_setting("brand_address", ""),
    }
