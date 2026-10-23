"""CRM (D1–D6). Sub-routers: contacts/tasks/outbox here; pipeline, automations, forms, questionnaires."""
import csv
import io
from datetime import datetime, timedelta

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app import db, journal, mailer
from app.modules.crm import automations, emails, forms_routes, pipeline, questionnaires
from app.modules.crm import automations_routes
from app.uploads import read_limited
from app.web import _localtime, redirect, render, safe_back, row_or_404

router = APIRouter()
router.include_router(pipeline.router)
router.include_router(automations_routes.router)
router.include_router(forms_routes.router)
router.include_router(questionnaires.router)

STATUSES = ["new", "contacted", "replied", "meeting", "client", "lost"]


def log_activity(contact_id: int, type_: str, body: str) -> None:
    db.insert("activities", contact_id=contact_id, type=type_, body=body, created_at=db.now())
    db.update("contacts", contact_id, last_activity_at=db.now(), last_activity_type=type_)


def create_contact(**fields) -> int:
    fields.setdefault("status", "new")
    fields.setdefault("tags", [])
    cid = db.insert("contacts", created_at=db.now(), last_activity_at=db.now(), last_activity_type="created", **fields)
    automations.fire("contact_created", cid)
    return cid


# --- contacts -------------------------------------------------------------------------

@router.get("/crm/contacts")
def contacts(request: Request, q: str = "", status: str = "", source: str = "", spam: int = 0):
    sql = "SELECT c.*, co.name company FROM contacts c LEFT JOIN companies co ON co.id = c.company_id WHERE c.is_spam = ?"
    args: list = [spam]
    if q:
        sql += " AND (c.business_name LIKE ? OR c.first_name LIKE ? OR c.last_name LIKE ? OR c.email LIKE ? OR c.phone LIKE ?)"
        args += [f"%{q}%"] * 5
    if status:
        sql += " AND c.status = ?"
        args.append(status)
    if source:
        sql += " AND c.source = ?"
        args.append(source)
    rows = db.all(sql + " ORDER BY c.last_activity_at DESC LIMIT 500", *args)
    return render(request, "crm/contacts.html", title="Contacts", rows=rows, statuses=STATUSES,
                  f={"q": q, "status": status, "source": source, "spam": spam})


@router.post("/crm/contacts")
def contact_new(business_name: str = Form(""), first_name: str = Form(""), last_name: str = Form(""),
                email: str = Form(""), phone: str = Form(""), website: str = Form("")):
    cid = create_contact(business_name=business_name, first_name=first_name, last_name=last_name, email=email,
                         phone=phone, website=website, source="manual")
    return redirect(f"/crm/contacts/{cid}")


@router.post("/crm/contacts/import")
async def contacts_import(file: UploadFile = File(...)):
    """CSV with any of: business_name, first_name, last_name, email, phone, website, address, tags."""
    text = (await read_limited(file, "csv")).decode("utf-8-sig", errors="replace")
    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if not any(row.get(k) for k in ("business_name", "email", "phone", "first_name")):
            continue
        create_contact(**{k: row.get(k, "") for k in ("business_name", "first_name", "last_name", "email",
                                                        "phone", "website", "address")},
                       tags=[t.strip() for t in row.get("tags", "").split(",") if t.strip()], source="import")
        n += 1
    return redirect(f"/crm/contacts?imported={n}")


def _contact_or_404(cid: int) -> dict:
    c = db.one("SELECT * FROM contacts WHERE id = ?", cid)
    if not c:
        raise HTTPException(404, "Contact not found")
    return c


@router.get("/crm/contacts/{cid}")
def contact_detail(request: Request, cid: int):
    c = _contact_or_404(cid)
    place = db.one("SELECT * FROM places WHERE id = ?", c["place_id"]) if c["place_id"] else None
    return render(request, "crm/contact.html", title=c["business_name"] or c["first_name"] or "Contact", c=c,
                  place=place, statuses=STATUSES,
                  activities=db.all("SELECT * FROM activities WHERE contact_id = ? ORDER BY id DESC", cid),
                  tasks=db.all("SELECT * FROM tasks WHERE contact_id = ? ORDER BY done, due_at", cid),
                  deals=db.all("SELECT d.*, s.name stage, p.name pipeline FROM deals d JOIN pipeline_stages s "
                               "ON s.id = d.stage_id JOIN pipelines p ON p.id = d.pipeline_id WHERE d.contact_id = ?", cid),
                  outbox=db.all("SELECT * FROM outbox WHERE contact_id = ? ORDER BY id DESC", cid),
                  audits=db.all("SELECT * FROM audits WHERE place_id = ? ORDER BY id DESC", c["place_id"]) if c["place_id"] else [],
                  scans=db.all("SELECT * FROM scans WHERE place_id = ? ORDER BY id DESC LIMIT 10", c["place_id"]) if c["place_id"] else [],
                  runs=db.all("SELECT r.*, a.name FROM automation_runs r JOIN automations a ON a.id = r.automation_id "
                              "WHERE r.contact_id = ? ORDER BY r.id DESC", cid),
                  autos=db.all("SELECT id, name FROM automations WHERE active = 1"),
                  pipelines=pipeline.pipelines_with_stages(),
                  companies=db.all("SELECT id, name FROM companies ORDER BY name"))


@router.post("/crm/contacts/{cid}")
async def contact_update(request: Request, cid: int):
    old = _contact_or_404(cid)
    form = dict(await request.form())
    fields = {k: form.get(k, "").strip() for k in automations.CONTACT_FIELDS if k in form}
    if "company_id" in form:
        fields["company_id"] = int(form["company_id"]) if form["company_id"] else None
    fields["tags"] = [t.strip() for t in form.get("tags", "").split(",") if t.strip()]
    fields["do_not_contact"] = 1 if form.get("do_not_contact") else 0
    db.update("contacts", cid, **fields)
    for k in automations.CONTACT_FIELDS:
        if k in fields and (old.get(k) or "") != fields[k]:
            if k == "status":
                log_activity(cid, "stage", f"Status: {old['status']} → {fields[k]}")
            automations.fire("field_updated", cid, field=k, value=fields[k])
    return redirect(f"/crm/contacts/{cid}")


@router.post("/crm/contacts/{cid}/note")
def contact_note(cid: int, body: str = Form(...), type: str = Form("note")):
    _contact_or_404(cid)
    log_activity(cid, type if type in ("note", "call", "email", "sms") else "note", body)
    return redirect(f"/crm/contacts/{cid}")


@router.post("/crm/contacts/{cid}/find-email")
def contact_find_email(cid: int):
    """Read the contact's own website for a published email; fills the Email field only if it's empty."""
    c = _contact_or_404(cid)
    if not c["website"]:
        raise HTTPException(400, "Contact has no website.")
    found = emails.find(c["website"])
    if found["emails"]:
        if not c["email"]:
            db.update("contacts", cid, email=found["emails"][0])
            automations.fire("field_updated", cid, field="email", value=found["emails"][0])
        log_activity(cid, "note", f"Emails on website: {', '.join(found['emails'])}")
    else:
        if found["errors"] and not found["pages"]:
            journal.failure("crm", "find_email", "; ".join(found["errors"]), subject_type="contact", subject_id=cid)
        log_activity(cid, "note", f"No email found on {len(found['pages'])} website page(s).")
    return redirect(f"/crm/contacts/{cid}")


@router.post("/crm/contacts/{cid}/message")
def contact_message(cid: int, channel: str = Form("email"), subject: str = Form(""), body: str = Form(...)):
    c = _contact_or_404(cid)
    to = c["email"] if channel == "email" else c["phone"]
    if not to:
        raise HTTPException(400, f"Contact has no {'email' if channel == 'email' else 'phone'}.")
    mailer.queue(channel, cid, to, subject, body, origin="manual")
    return redirect("/crm/outbox")


@router.post("/crm/contacts/{cid}/automation")
def contact_start_automation(cid: int, automation_id: int = Form(...)):
    _contact_or_404(cid)
    automations.start(automation_id, cid, {"manual": True})
    return redirect(f"/crm/contacts/{cid}")


@router.post("/crm/contacts/{cid}/spam")
def contact_spam(cid: int, is_spam: int = Form(1)):
    db.update("contacts", cid, is_spam=is_spam, spam_reason="marked manually" if is_spam else None)
    return redirect(f"/crm/contacts/{cid}")


@router.post("/crm/contacts/{cid}/delete")
def contact_delete(cid: int):
    db.run("DELETE FROM contacts WHERE id = ?", cid)
    return redirect("/crm/contacts")


# --- tasks ----------------------------------------------------------------------------

@router.get("/crm/tasks")
def tasks(request: Request, show: str = "open"):
    where = "t.done = 0" if show == "open" else "1 = 1"
    rows = db.all(f"SELECT t.*, c.business_name, c.first_name FROM tasks t LEFT JOIN contacts c ON c.id = t.contact_id "
                  f"WHERE {where} ORDER BY t.done, t.due_at IS NULL, t.due_at LIMIT 500")
    # calendar: next 14 days
    today = datetime.now().date()  # calendar in local time
    days = []
    for i in range(14):
        d = (today + timedelta(days=i)).isoformat()
        days.append({"date": d, "tasks": [t for t in rows if _localtime(t["due_at"], "%Y-%m-%d") == d]})
    return render(request, "crm/tasks.html", title="Tasks", rows=rows, days=days, show=show,
                  contacts=db.all("SELECT id, business_name, first_name FROM contacts WHERE is_spam = 0 ORDER BY business_name"))


@router.post("/crm/tasks")
def task_new(title: str = Form(...), due_at: str = Form(""), contact_id: str = Form(""), notes: str = Form(""),
             back: str = Form("/crm/tasks")):
    db.insert("tasks", title=title, notes=notes, due_at=db.to_utc(due_at),
              contact_id=int(contact_id) if contact_id else None, created_at=db.now())
    return redirect(safe_back(back, "/crm/tasks"))


@router.post("/crm/tasks/{tid}/done")
def task_done(tid: int, back: str = Form("/crm/tasks")):
    t = row_or_404("tasks", tid)
    db.update("tasks", tid, done=0 if t["done"] else 1, done_at=None if t["done"] else db.now())
    if t["contact_id"] and not t["done"]:
        log_activity(t["contact_id"], "task", f"Done: {t['title']}")
    return redirect(safe_back(back, "/crm/tasks"))


# --- outbox ---------------------------------------------------------------------------

@router.get("/crm/outbox")
def outbox(request: Request, status: str = "draft"):
    rows = db.all("SELECT o.*, c.business_name, c.first_name FROM outbox o LEFT JOIN contacts c ON c.id = o.contact_id "
                  "WHERE o.status = ? ORDER BY o.id DESC LIMIT 300", status)
    counts = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) n FROM outbox GROUP BY status")}
    return render(request, "crm/outbox.html", title="Outbox", rows=rows, status=status, counts=counts)


@router.post("/crm/outbox/{oid}/{action}")
def outbox_action(oid: int, action: str, body: str = Form(None), subject: str = Form(None),
                  to_addr: str = Form(None)):
    m = db.one("SELECT * FROM outbox WHERE id = ?", oid)
    if not m:
        raise HTTPException(404)
    ai_text = journal.last_draft("outbox", oid)
    if ai_text is not None and m["status"] == "draft" and action in ("approve", "cancel"):
        journal.decision("outbox", action, draft=ai_text, final=None if action == "cancel" else (body if body is not None else m["body"]),
                         subject_type="outbox", subject_id=oid)
    if m["status"] in ("sending", "sent", "cancelled") and action != "retry":
        # being sent right now, or done: editing, cancelling or marking it sent would go wrong
        return redirect(f"/crm/outbox?status={m['status']}")
    if body is not None:
        db.update("outbox", oid, body=body, subject=subject if subject is not None else m["subject"],
                  to_addr=(to_addr.strip() if to_addr is not None else m["to_addr"]))
        m = db.one("SELECT * FROM outbox WHERE id = ?", oid)
    if action == "approve":
        if m["status"] != "draft":  # back button / double click: never send again
            return redirect(f"/crm/outbox?status={m['status']}")
        problem = mailer.approval_problem(m)
        if problem:
            raise HTTPException(400, problem)
        cols = {"status": "approved"}
        if m["channel"] == "email" and m["origin"] != "report":
            cols["body"] = mailer.with_current_footer(m["body"] or "")
        db.update("outbox", oid, **cols)
        mailer.send(oid)
    elif action == "retry":  # a failed email goes back to drafts, to be approved again
        if m["status"] == "failed":
            db.update("outbox", oid, status="draft", error=None)
    elif action == "cancel":
        db.update("outbox", oid, status="cancelled")
    elif action == "mark_sent":  # SMS or emails you sent yourself
        db.update("outbox", oid, status="sent", sent_at=db.now())
        if m["contact_id"]:
            log_activity(m["contact_id"], m["channel"], f"Sent: {(m['subject'] or m['body'] or '')[:80]}")
    return redirect(f"/crm/outbox?status={m['status'] if action == 'save' else 'draft'}")
