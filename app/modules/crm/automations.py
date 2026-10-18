"""Automation engine (D3): trigger → filters → ordered actions, with waits and a run log.

Triggers: form_submitted, contact_created, deal_created, deal_stage {stage_id},
          field_updated {field, value}, manual
Filters:  [{field, op: eq|neq|contains|empty|not_empty, value}] on contact fields or custom.<name>
Actions:  send_email {subject, body} · send_sms {body} · wait {minutes} · create_task {title, due_hours}
          update_field {field, value} · add_tag {tag} · move_deal {stage_id}
          condition {field, op, value} (stop when false) · end_if_review_clicked
          send_questionnaire {questionnaire_id} · send_review_request {company_id}
Variables in messages: {{first_name}} {{last_name}} {{business_name}} {{email}} {{phone}}
          {{company_name}} {{review_link}} {{questionnaire_link}} {{custom.<field>}}
Outgoing messages go through the outbox, so cold contacts always need your approval.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from app import db, jobs, mailer, settings

TRIGGERS = {
    "form_submitted": "New form submission",
    "contact_created": "New contact created",
    "deal_created": "New deal in the pipeline",
    "deal_stage": "Deal moved to a stage",
    "field_updated": "Contact field updated",
    "manual": "Started manually",
}
ACTIONS = {
    "send_email": "Send email", "send_sms": "Send SMS", "wait": "Wait", "create_task": "Create task",
    "update_field": "Update field", "add_tag": "Add tag", "move_deal": "Move deal to stage",
    "condition": "Continue only if…", "end_if_review_clicked": "Stop if review link was clicked",
    "send_questionnaire": "Send questionnaire", "send_review_request": "Send review request",
}
CONTACT_FIELDS = ["first_name", "last_name", "business_name", "email", "phone", "website", "address",
                  "source", "status"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _contact(cid: int) -> dict | None:
    c = db.one("SELECT * FROM contacts WHERE id = ?", cid)
    if c:
        c["custom"] = db.loads(c["custom"], {}) or {}
        c["tags"] = db.loads(c["tags"], []) or []
    return c


def field_value(contact: dict, field: str):
    if field.startswith("custom."):
        return contact["custom"].get(field[7:])
    return contact.get(field)


def matches(contact: dict, rule: dict) -> bool:
    v = field_value(contact, rule.get("field", ""))
    v = "" if v is None else str(v).lower()
    target = str(rule.get("value", "")).lower()
    op = rule.get("op", "eq")
    if op == "eq":
        return v == target
    if op == "neq":
        return v != target
    if op == "contains":
        # comma separated list means "any of"
        return any(t.strip() and t.strip() in v for t in target.split(","))
    if op == "empty":
        return v == ""
    if op == "not_empty":
        return v != ""
    return False


def render_vars(text: str, contact: dict, ctx: dict) -> str:
    company = db.one("SELECT * FROM companies WHERE id = ?", contact.get("company_id")) if contact.get("company_id") else None
    values = {k: contact.get(k) or "" for k in CONTACT_FIELDS}
    values["company_name"] = company["name"] if company else ""
    values["review_link"] = ctx.get("review_link", "")
    values["questionnaire_link"] = ctx.get("questionnaire_link", "")

    def sub(m):
        key = m.group(1).strip()
        if key.startswith("custom."):
            return str(contact["custom"].get(key[7:], ""))
        return str(values.get(key, ""))
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", sub, text or "")


def fire(trigger: str, contact_id: int, **context) -> list[int]:
    """Start every active automation for this trigger whose config and filters match."""
    contact = _contact(contact_id)
    if not contact or contact["is_spam"]:
        return []
    started = []
    for a in db.all("SELECT * FROM automations WHERE active = 1 AND trigger = ?", trigger):
        cfg = db.loads(a["trigger_config"], {}) or {}
        if trigger == "deal_stage" and cfg.get("stage_id") and int(cfg["stage_id"]) != int(context.get("stage_id", 0)):
            continue
        if trigger == "field_updated":
            if cfg.get("field") and cfg["field"] != context.get("field"):
                continue
            if cfg.get("value") and str(cfg["value"]).lower() != str(context.get("value", "")).lower():
                continue
        if not all(matches(contact, f) for f in (db.loads(a["filters"], []) or [])):
            continue
        started.append(start(a["id"], contact_id, context))
    return started


def start(automation_id: int, contact_id: int, context: dict | None = None) -> int:
    run_id = db.insert("automation_runs", automation_id=automation_id, contact_id=contact_id, step=0,
                       status="running", context=context or {}, log=[], created_at=db.now(), updated_at=db.now())
    jobs.submit(advance, run_id)
    return run_id


def _log(run: dict, line: str) -> list:
    entries = db.loads(run["log"], []) or []
    entries.append(f"{db.now()} {line}")
    return entries


def advance(run_id: int) -> None:
    """Execute actions from the current step until a wait, the end, or a stop."""
    run = db.one("SELECT * FROM automation_runs WHERE id = ?", run_id)
    if not run or run["status"] not in ("running", "waiting"):
        return
    auto = db.one("SELECT * FROM automations WHERE id = ?", run["automation_id"])
    actions = db.loads(auto["actions"], []) if auto else []
    ctx = db.loads(run["context"], {}) or {}
    step = run["step"]
    while step < len(actions):
        contact = _contact(run["contact_id"])
        if not contact:
            break
        action = actions[step]
        step += 1
        try:
            outcome = execute(action, contact, ctx)
        except Exception as exc:
            db.update("automation_runs", run_id, status="failed", step=step,
                      log=_log(run, f"step {step} {action.get('type')} failed: {exc}"), updated_at=db.now())
            return
        run["log"] = json.dumps(_log(run, f"step {step} {action.get('type')}: {outcome['note']}"))
        if outcome.get("stop"):
            db.update("automation_runs", run_id, status="stopped", step=step, log=db.loads(run["log"]),
                      context=ctx, updated_at=db.now())
            return
        if outcome.get("wait_until"):
            db.update("automation_runs", run_id, status="waiting", step=step, resume_at=outcome["wait_until"],
                      log=db.loads(run["log"]), context=ctx, updated_at=db.now())
            return
    db.update("automation_runs", run_id, status="done", step=step, log=db.loads(run["log"]), context=ctx,
              updated_at=db.now())


def execute(action: dict, contact: dict, ctx: dict) -> dict:
    t = action.get("type")
    if t == "send_email":
        if not contact.get("email"):
            return {"note": "skipped, no email"}
        oid = mailer.queue("email", contact["id"], contact["email"], render_vars(action.get("subject", ""), contact, ctx),
                           render_vars(action.get("body", ""), contact, ctx), origin="automation", approved=True)
        return {"note": f"email queued (outbox #{oid})" if oid else "skipped, do not contact"}
    if t == "send_sms":
        if not contact.get("phone"):
            return {"note": "skipped, no phone"}
        oid = mailer.queue("sms", contact["id"], contact["phone"], "", render_vars(action.get("body", ""), contact, ctx),
                           origin="automation", approved=True)
        return {"note": f"sms queued (outbox #{oid})" if oid else "skipped, do not contact"}
    if t == "wait":
        until = _now() + timedelta(minutes=float(action.get("minutes", 0)))
        return {"note": f"waiting until {until.isoformat(timespec='minutes')}",
                "wait_until": until.isoformat(timespec="seconds")}
    if t == "create_task":
        due = _now() + timedelta(hours=float(action.get("due_hours", 24)))
        db.insert("tasks", contact_id=contact["id"], title=render_vars(action.get("title", "Follow up"), contact, ctx),
                  due_at=due.isoformat(timespec="seconds"), created_at=db.now())
        return {"note": "task created"}
    if t == "update_field":
        set_field(contact["id"], action["field"], action.get("value", ""), fire_trigger=False)
        return {"note": f"{action['field']} = {action.get('value', '')}"}
    if t == "add_tag":
        tags = sorted(set(contact["tags"] + [action.get("tag", "")]) - {""})
        db.update("contacts", contact["id"], tags=tags)
        return {"note": f"tag {action.get('tag')}"}
    if t == "move_deal":
        deal = db.one("SELECT * FROM deals WHERE contact_id = ? AND status = 'open' ORDER BY id DESC", contact["id"])
        if not deal:
            return {"note": "no open deal"}
        db.update("deals", deal["id"], stage_id=int(action["stage_id"]), updated_at=db.now())
        return {"note": f"deal moved to stage {action['stage_id']}"}
    if t == "condition":
        ok = matches(contact, action)
        return {"note": "condition met" if ok else "condition not met, stopping", "stop": not ok}
    if t == "end_if_review_clicked":
        clicked = db.one("SELECT 1 FROM review_requests WHERE contact_id = ? AND clicked_at IS NOT NULL", contact["id"])
        return {"note": "review link clicked, stopping" if clicked else "no click yet", "stop": bool(clicked)}
    if t == "send_questionnaire":
        q = db.one("SELECT * FROM questionnaires WHERE id = ?", int(action["questionnaire_id"]))
        if not q or not contact.get("email"):
            return {"note": "skipped"}
        ctx["questionnaire_link"] = f"{settings.PUBLIC_BASE_URL}/q/{q['token']}?c={contact['id']}"
        body = action.get("body") or "Hi {{first_name}},\n\nPlease fill in this short questionnaire: {{questionnaire_link}}"
        mailer.queue("email", contact["id"], contact["email"], action.get("subject") or q["name"],
                     render_vars(body, contact, ctx), origin="automation", approved=True)
        return {"note": "questionnaire sent"}
    if t == "send_review_request":
        from app.modules.gbp import reviews
        company_id = int(action.get("company_id") or contact.get("company_id") or 0)
        if not company_id:
            return {"note": "skipped, no company"}
        link = reviews.create_request(company_id, contact["id"], channel="email" if contact.get("email") else "sms")
        ctx["review_link"] = link
        body = action.get("body") or "Hi {{first_name}},\n\nThanks for choosing {{company_name}}. Would you share a quick review? {{review_link}}"
        channel = "email" if contact.get("email") else "sms"
        mailer.queue(channel, contact["id"], contact.get("email") or contact.get("phone"),
                     action.get("subject") or "How did we do?", render_vars(body, contact, ctx),
                     origin="automation", approved=True)
        return {"note": "review request sent"}
    raise ValueError(f"Unknown action {t}")


def set_field(contact_id: int, field: str, value, *, fire_trigger: bool = True) -> None:
    contact = _contact(contact_id)
    if field.startswith("custom."):
        custom = contact["custom"]
        custom[field[7:]] = value
        db.update("contacts", contact_id, custom=custom)
    elif field in CONTACT_FIELDS:
        db.update("contacts", contact_id, **{field: value})
    else:
        raise ValueError(f"Unknown field {field}")
    if fire_trigger:
        fire("field_updated", contact_id, field=field, value=value)


@jobs.on_tick
def resume_waiting() -> None:
    now = _now().isoformat(timespec="seconds")
    for r in db.all("SELECT id FROM automation_runs WHERE status = 'waiting' AND resume_at <= ?", now):
        db.update("automation_runs", r["id"], status="running")
        advance(r["id"])
