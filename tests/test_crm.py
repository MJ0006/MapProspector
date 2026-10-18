import json

from app import db
from app.modules.crm import automations, pipeline, spam


def _auto(trigger, actions, **kw):
    return db.insert("automations", name="t", trigger=trigger, trigger_config=kw.get("cfg", {}),
                     filters=kw.get("filters", []), actions=actions, active=1, created_at=db.now())


def test_form_submission_runs_automation_with_wait(client):
    _auto("form_submitted", [
        {"type": "send_email", "subject": "Thanks {{first_name}}", "body": "Hi {{first_name}}, got it."},
        {"type": "wait", "minutes": "5"},
        {"type": "create_task", "title": "Call {{first_name}}", "due_hours": "2"},
    ])
    client.post("/crm/forms", data={"name": "Website"})
    token = db.one("SELECT token FROM forms")["token"]
    r = client.post(f"/f/{token}", data={"name": "Ana Lopez", "email": "ana@example.com", "message": "Need a roof"},
                    follow_redirects=False)
    assert r.status_code == 303
    c = db.one("SELECT * FROM contacts")
    assert c["first_name"] == "Ana" and c["source"] == "form" and json.loads(c["custom"])["message"] == "Need a roof"
    m = db.one("SELECT * FROM outbox")
    assert m["subject"] == "Thanks Ana" and m["status"] == "approved"  # inbound lead: auto-approved
    assert "unsubscribe" in m["body"]
    run = db.one("SELECT * FROM automation_runs")
    assert run["status"] == "waiting" and db.one("SELECT COUNT(*) n FROM tasks")["n"] == 0
    db.run("UPDATE automation_runs SET resume_at = '2000-01-01T00:00:00+00:00'")
    automations.resume_waiting()
    assert db.one("SELECT status FROM automation_runs")["status"] == "done"
    assert db.one("SELECT title FROM tasks")["title"] == "Call Ana"


def test_cold_contacts_always_need_approval():
    _auto("contact_created", [{"type": "send_email", "subject": "Hello", "body": "Pitch"}])
    from app.modules.crm.routes import create_contact
    create_contact(business_name="Peak Plumbing", email="owner@peak.example", source="prospecting")
    assert db.one("SELECT status FROM outbox")["status"] == "draft"


def test_do_not_contact_blocks_messages():
    from app import mailer
    cid = db.insert("contacts", business_name="X", email="x@x.com", source="form", status="new", do_not_contact=1)
    assert mailer.queue("email", cid, "x@x.com", "s", "b", origin="t", approved=True) is None


def test_filters_and_condition_stop():
    aid = _auto("manual", [{"type": "condition", "field": "status", "op": "eq", "value": "client"},
                           {"type": "add_tag", "tag": "vip"}])
    cid = db.insert("contacts", business_name="X", source="manual", status="new", tags=[])
    automations.start(aid, cid)
    assert db.one("SELECT status FROM automation_runs")["status"] == "stopped"
    assert json.loads(db.one("SELECT tags FROM contacts")["tags"]) == []
    assert automations.matches({"custom": {}, "email": "a@gmail.com"}, {"field": "email", "op": "contains", "value": "yahoo, gmail"})


def test_spam_rules(client):
    for kind, value in [("keyword", "seo services"), ("url_in_message", ""), ("phone_format", ""), ("domain", "spam.ru")]:
        db.insert("spam_rules", kind=kind, value=value, active=1)
    assert spam.check({"message": "We offer SEO services!"})[0]
    assert spam.check({"message": "visit https://x.co"}) == (True, "link in message")
    assert spam.check({"phone": "12"}) == (True, "invalid phone number")
    assert spam.check({"email": "bot@spam.ru"})[0]
    assert spam.check({"name": "Real Person", "phone": "(303) 555-0100", "message": "Leaking roof"}) == (False, None)
    client.post("/crm/forms", data={"name": "W"})
    token = db.one("SELECT token FROM forms")["token"]
    client.post(f"/f/{token}", data={"name": "Bot", "email": "a@b.com", "message": "cheap seo services"})
    sub = db.one("SELECT * FROM form_submissions")
    assert sub["is_spam"] == 1 and "keyword" in sub["spam_reason"]
    # honeypot
    client.post(f"/f/{token}", data={"name": "Bot2", "email": "a@b.com", "_hp": "x"})
    assert db.one("SELECT COUNT(*) n FROM form_submissions")["n"] == 1


def test_pipeline_move_fires_stage_trigger(client):
    pls = pipeline.pipelines_with_stages()
    stages = pls[0]["stages"]
    _auto("deal_stage", [{"type": "add_tag", "tag": "meeting-booked"}], cfg={"stage_id": stages[4]["id"]})
    cid = db.insert("contacts", business_name="Acme", source="manual", status="new", tags=[])
    did = pipeline.create_deal(cid, "Retainer", value=500)
    r = client.post(f"/crm/deals/{did}/move", data={"stage_id": stages[4]["id"]})
    assert r.status_code == 200
    assert json.loads(db.one("SELECT tags FROM contacts")["tags"]) == ["meeting-booked"]
    client.post(f"/crm/deals/{did}/move", data={"stage_id": stages[6]["id"]})  # "Won" = 100 %
    assert db.one("SELECT status FROM deals")["status"] == "won"


def test_questionnaire_flow(client):
    client.post("/crm/questionnaires", data={"name": "Onboarding",
                                             "questions": "Business name | text\nServices | multi | Roofing, Gutters\nHappy? | likert"})
    q = db.one("SELECT * FROM questionnaires")
    assert len(json.loads(q["questions"])) == 3
    client.post(f"/q/{q['token']}", data={"q1": "Acme", "q2": ["Roofing", "Gutters"], "q3": "4"})
    ans = json.loads(db.one("SELECT answers FROM questionnaire_responses")["answers"])
    assert ans == {"q1": "Acme", "q2": ["Roofing", "Gutters"], "q3": "4"}


def test_crm_pages_render(client):
    cid = db.insert("contacts", business_name="Acme", source="manual", status="new", tags=[], created_at=db.now())
    for url in ("/crm/contacts", f"/crm/contacts/{cid}", "/crm/pipeline", "/crm/tasks", "/crm/outbox",
                "/crm/automations", "/crm/automations/0", "/crm/forms", "/crm/questionnaires"):
        r = client.get(url)
        assert r.status_code == 200, url
    client.post("/crm/automations/0", data={"name": "A", "trigger": "manual", "actions": '[{"type":"wait","minutes":"1"}]'})
    aid = db.one("SELECT id FROM automations")["id"]
    assert client.get(f"/crm/automations/{aid}").status_code == 200
    # outbox approve without SMTP keeps it approved (waiting for SMTP / manual send)
    oid = db.insert("outbox", channel="email", contact_id=cid, to_addr="a@b.c", subject="s", body="b", status="draft", origin="t")
    r = client.post(f"/crm/outbox/{oid}/approve", data={"body": "edited", "subject": "s2", "to_addr": "a@b.c"})
    assert r.status_code == 400 and "postal address" in r.text  # cold email, no address yet
    db.set_setting("brand_address", "1 Main St, Austin TX 78701")
    client.post(f"/crm/outbox/{oid}/approve", data={"body": "edited", "subject": "s2", "to_addr": "a@b.c"})
    m = db.one("SELECT * FROM outbox WHERE id = ?", oid)
    assert m["status"] == "approved" and m["body"].startswith("edited") and "1 Main St" in m["body"] and m["subject"] == "s2"
    oid2 = db.insert("outbox", channel="email", contact_id=cid, to_addr="", subject="s", body="b", status="draft", origin="t")
    assert client.post(f"/crm/outbox/{oid2}/approve", data={"body": "b", "to_addr": ""}).status_code == 400
    r = client.post("/crm/contacts/import", files={"file": ("c.csv", b"business_name,email,tags\nBeta Co,b@b.co,hot\n", "text/csv")})
    assert db.one("SELECT source FROM contacts WHERE business_name = 'Beta Co'")["source"] == "import"
