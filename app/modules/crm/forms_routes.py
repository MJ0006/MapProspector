"""Lead-capture forms + spam filters (D4). Public form: /f/{token} (HTML page) and POST /f/{token}
(also accepts posts from your own website form; see the embed snippet on the forms page)."""
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import db, settings
from app.modules.crm import automations, spam
from app.web import redirect, render, row_or_404

router = APIRouter()

DEFAULT_FIELDS = [
    {"name": "name", "label": "Your name", "type": "text", "required": True},
    {"name": "email", "label": "Email", "type": "email", "required": True},
    {"name": "phone", "label": "Phone", "type": "tel", "required": False},
    {"name": "message", "label": "How can we help?", "type": "textarea", "required": False},
]


@router.get("/crm/forms")
def forms_page(request: Request):
    forms = db.all("SELECT f.*, (SELECT COUNT(*) FROM form_submissions s WHERE s.form_id = f.id AND s.is_spam = 0) good, "
                   "(SELECT COUNT(*) FROM form_submissions s WHERE s.form_id = f.id AND s.is_spam = 1) spam "
                   "FROM forms f ORDER BY f.id DESC")
    subs = db.all("SELECT s.*, f.name form FROM form_submissions s JOIN forms f ON f.id = s.form_id ORDER BY s.id DESC LIMIT 100")
    rules = db.all("SELECT * FROM spam_rules ORDER BY kind, value")
    stats = db.all("SELECT spam_reason, COUNT(*) n FROM form_submissions WHERE is_spam = 1 GROUP BY spam_reason ORDER BY n DESC")
    return render(request, "crm/forms.html", title="Forms & spam", forms=forms, subs=subs, rules=rules, stats=stats,
                  kinds=spam.KINDS, base=settings.PUBLIC_BASE_URL,
                  companies=db.all("SELECT id, name FROM companies ORDER BY name"))


@router.post("/crm/forms")
def form_new(name: str = Form(...), company_id: str = Form(""), fields: str = Form("")):
    parsed = DEFAULT_FIELDS
    if fields.strip():
        parsed = []
        for line in fields.splitlines():  # "name|Label|type|required"
            parts = [p.strip() for p in line.split("|")]
            if parts[0]:
                parsed.append({"name": parts[0], "label": parts[1] if len(parts) > 1 else parts[0],
                               "type": parts[2] if len(parts) > 2 else "text",
                               "required": len(parts) > 3 and parts[3].lower() in ("1", "yes", "required", "true")})
    db.insert("forms", name=name, company_id=int(company_id) if company_id else None, fields=parsed,
              token=secrets.token_urlsafe(10), created_at=db.now())
    return redirect("/crm/forms")


@router.post("/crm/forms/{fid}/delete")
def form_delete(fid: int):
    db.run("DELETE FROM forms WHERE id = ?", fid)
    return redirect("/crm/forms")


@router.post("/crm/spam-rules")
def rule_new(kind: str = Form(...), value: str = Form("")):
    if kind not in spam.KINDS:
        raise HTTPException(400)
    db.insert("spam_rules", kind=kind, value=value.strip(), active=1)
    return redirect("/crm/forms")


@router.post("/crm/spam-rules/{rid}/delete")
def rule_delete(rid: int):
    db.run("DELETE FROM spam_rules WHERE id = ?", rid)
    return redirect("/crm/forms")


@router.post("/crm/submissions/{sid}/not-spam")
def submission_not_spam(sid: int):
    s = row_or_404("form_submissions", sid)
    db.update("form_submissions", sid, is_spam=0, spam_reason=None)
    if s["contact_id"]:
        db.update("contacts", s["contact_id"], is_spam=0, spam_reason=None)
        automations.fire("form_submitted", s["contact_id"], form_id=s["form_id"])
    return redirect("/crm/forms")


def _form(token: str) -> dict:
    f = db.one("SELECT * FROM forms WHERE token = ?", token)
    if not f:
        raise HTTPException(404, "Form not found")
    f["fields"] = db.loads(f["fields"], [])
    return f


@router.get("/f/{token}")
def public_form(request: Request, token: str, sent: int = 0):
    return render(request, "crm/form_public.html", title=_form(token)["name"], f=_form(token), sent=sent)


MAX_PER_IP_HOUR = 5  # more than this from one address in an hour is treated as spam (stops automations)
EMAIL_RE = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")


def ingest_submission(form: dict, data: dict, ip: str | None, country: str | None) -> dict:
    # One address only: "a@x.com, b@y.com" must not make automations email strangers.
    m = EMAIL_RE.search(data.get("email") or "")
    data["email"] = m.group(0) if m else ""
    is_spam, reason = spam.check(data, ip, country)
    if not is_spam and ip and db.one("SELECT COUNT(*) n FROM form_submissions WHERE ip = ? AND created_at >= ?", ip,
                                     (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"))["n"] >= MAX_PER_IP_HOUR:
        is_spam, reason = True, f"more than {MAX_PER_IP_HOUR} submissions from this address in an hour"
    name = (data.get("name") or "").strip()
    first, _, last = name.partition(" ")
    custom = {k: v for k, v in data.items() if k not in ("name", "email", "phone", "message", "website")}
    if data.get("message"):
        custom["message"] = data["message"]
    cid = db.insert("contacts", company_id=form["company_id"], first_name=first or data.get("first_name"),
                    last_name=last or data.get("last_name"), email=data.get("email"), phone=data.get("phone"),
                    website=data.get("website"), business_name=data.get("business_name") or data.get("company"),
                    source="form", status="new", tags=[], custom=custom, is_spam=1 if is_spam else 0,
                    spam_reason=reason, created_at=db.now(), last_activity_at=db.now(), last_activity_type="form")
    sid = db.insert("form_submissions", form_id=form["id"], contact_id=cid, data=data, ip=ip, country=country,
                    is_spam=1 if is_spam else 0, spam_reason=reason, created_at=db.now())
    db.insert("activities", contact_id=cid, type="form", body=f"Submitted form \"{form['name']}\"",
              created_at=db.now())
    if not is_spam:
        automations.fire("contact_created", cid)
        automations.fire("form_submitted", cid, form_id=form["id"])
    return {"submission_id": sid, "contact_id": cid, "spam": is_spam}


@router.post("/f/{token}")
async def public_form_submit(request: Request, token: str):
    form = _form(token)
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            data = await request.json()
        else:
            data = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    except ValueError:
        raise HTTPException(400, "Could not read the submission.")
    if not isinstance(data, dict) or len(data) > 40:
        raise HTTPException(400, "Unexpected submission format.")
    if data.pop("_hp", ""):  # honeypot field filled = bot
        return HTMLResponse("Thanks!")
    data = {str(k)[:60]: str(v)[:5000] for k, v in data.items() if v is not None}
    for f in form["fields"]:
        if f.get("required") and not data.get(f["name"]):
            raise HTTPException(400, f"{f['label']} is required")
    peer = request.client.host if request.client else ""
    # X-Forwarded-For only counts when it comes from a tunnel on this computer; otherwise a bot could fake any IP.
    ip = request.headers.get("x-forwarded-for", peer).split(",")[0].strip() if peer in ("127.0.0.1", "::1") else peer
    ingest_submission(form, data, ip or None, request.headers.get("cf-ipcountry"))
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True})
    return redirect(f"/f/{token}?sent=1")
