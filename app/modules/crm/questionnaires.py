"""Client onboarding questionnaires (D6). Answers are stored and, for clients with a brain vault,
added to the vault as source evidence (vendor/local-seo-brain/scripts/ingest_source.py)."""
import secrets

from fastapi import APIRouter, Form, HTTPException, Request

import logging
from urllib.parse import urlencode

from app import brain, db, journal, security, settings
from app.web import redirect, render

router = APIRouter()
log = logging.getLogger("mapprospector.questionnaires")


def link(token: str, contact_id: int | None = None, company_id: int | None = None) -> str:
    """Public link (path + query). Ties answers to a contact or client only when signed, so nobody can
    send answers into another client's vault by editing the link."""
    if not contact_id and not company_id:
        return f"/q/{token}"
    params = {k: v for k, v in (("c", contact_id), ("co", company_id)) if v}
    params["s"] = security.sign("q", token, contact_id or "", company_id or "")
    return f"/q/{token}?{urlencode(params)}"

TYPES = {"text": "Short text", "longtext": "Paragraph", "choice": "Multiple choice (one)",
         "multi": "Checkboxes (many)", "likert": "Scale 1–5"}

# Onboarding questions that map to what local-seo-brain needs (truthful GBP facts, service area, evidence).
STARTER = [
    {"label": "Legal business name exactly as customers see it on your signage", "type": "text"},
    {"label": "Do customers visit your address, or do you travel to them?", "type": "choice",
     "options": ["Customers visit us", "We travel to customers", "Both"]},
    {"label": "Cities / areas you serve", "type": "longtext"},
    {"label": "Main services you offer (one per line)", "type": "longtext"},
    {"label": "Opening hours", "type": "longtext"},
    {"label": "Primary phone number customers should call", "type": "text"},
    {"label": "Website URL", "type": "text"},
    {"label": "Directories you know you are listed on", "type": "longtext"},
    {"label": "Who can approve changes to your Google Business Profile?", "type": "text"},
    {"label": "How satisfied are you with the leads you get from Google today?", "type": "likert"},
]


def _parse(text: str) -> list[dict]:
    """Lines: 'Question label | type | option A, option B'."""
    out = []
    for i, line in enumerate(l for l in text.splitlines() if l.strip()):
        parts = [p.strip() for p in line.split("|")]
        qtype = parts[1].lower() if len(parts) > 1 and parts[1].lower() in TYPES else "text"
        opts = [o.strip() for o in parts[2].split(",")] if len(parts) > 2 else []
        out.append({"id": f"q{i + 1}", "label": parts[0], "type": qtype, "options": opts})
    return out


@router.get("/crm/questionnaires")
def questionnaires(request: Request):
    rows = db.all("SELECT q.*, (SELECT COUNT(*) FROM questionnaire_responses r WHERE r.questionnaire_id = q.id) n "
                  "FROM questionnaires q ORDER BY q.id DESC")
    responses = db.all("SELECT r.*, q.name, co.name company, c.business_name, c.first_name FROM questionnaire_responses r "
                       "JOIN questionnaires q ON q.id = r.questionnaire_id LEFT JOIN companies co ON co.id = r.company_id "
                       "LEFT JOIN contacts c ON c.id = r.contact_id ORDER BY r.id DESC LIMIT 50")
    starter = "\n".join(f"{q['label']} | {q['type']}" + (f" | {', '.join(q['options'])}" if q.get("options") else "")
                        for q in STARTER)
    clients = db.all("SELECT id, name FROM companies WHERE is_client = 1 ORDER BY name")
    return render(request, "crm/questionnaires.html", title="Questionnaires", rows=rows, responses=responses,
                  types=TYPES, starter=starter, base=settings.PUBLIC_BASE_URL, clients=clients, link=link)


@router.post("/crm/questionnaires")
def questionnaire_new(name: str = Form(...), questions: str = Form(...)):
    qs = _parse(questions)
    if not qs:
        raise HTTPException(400, "Add at least one question")
    db.insert("questionnaires", name=name, questions=qs, token=secrets.token_urlsafe(10), created_at=db.now())
    return redirect("/crm/questionnaires")


@router.get("/crm/questionnaires/responses/{rid}")
def response_view(request: Request, rid: int):
    r = db.one("SELECT r.*, q.name, q.questions FROM questionnaire_responses r JOIN questionnaires q "
               "ON q.id = r.questionnaire_id WHERE r.id = ?", rid)
    if not r:
        raise HTTPException(404)
    answers = db.loads(r["answers"], {})
    qa = [(q["label"], answers.get(q["id"])) for q in db.loads(r["questions"], [])]
    return render(request, "crm/questionnaire_response.html", title=r["name"], r=r, qa=qa)


def _q(token: str) -> dict:
    q = db.one("SELECT * FROM questionnaires WHERE token = ?", token)
    if not q:
        raise HTTPException(404, "Questionnaire not found")
    q["questions"] = db.loads(q["questions"], [])
    return q


@router.get("/q/{token}")
def public_q(request: Request, token: str, c: str = "", co: str = "", s: str = "", sent: int = 0):
    q = _q(token)
    return render(request, "crm/questionnaire_public.html", title=q["name"], q=q, c=c, co=co, s=s, sent=sent)


@router.post("/q/{token}")
async def public_q_submit(request: Request, token: str):
    q = _q(token)
    form = await request.form()
    answers = {}
    for qq in q["questions"]:
        answers[qq["id"]] = form.getlist(qq["id"]) if qq["type"] == "multi" else (form.get(qq["id"]) or "")
    contact_id = int(form["c"]) if form.get("c", "").isdigit() else None
    company_id = int(form["co"]) if form.get("co", "").isdigit() else None
    if (contact_id or company_id) and not security.signed(form.get("s", ""), "q", token, contact_id or "", company_id or ""):
        contact_id = company_id = None  # unsigned or edited link: keep the answers, don't attach them to anyone
    if contact_id and not db.one("SELECT 1 FROM contacts WHERE id = ?", contact_id):
        contact_id = None
    if company_id and not db.one("SELECT 1 FROM companies WHERE id = ?", company_id):
        company_id = None
    if contact_id and not company_id:
        contact = db.one("SELECT company_id FROM contacts WHERE id = ?", contact_id)
        company_id = contact["company_id"] if contact else None
    rid = db.insert("questionnaire_responses", questionnaire_id=q["id"], company_id=company_id, contact_id=contact_id,
                    answers=answers, created_at=db.now())
    if contact_id:
        db.insert("activities", contact_id=contact_id, type="form", body=f"Answered questionnaire \"{q['name']}\"",
                  created_at=db.now())
    company = db.one("SELECT * FROM companies WHERE id = ?", company_id) if company_id else None
    if company and company["vault_slug"]:
        path = settings.UPLOAD_DIR / f"questionnaire-{rid}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# Onboarding questionnaire: {q['name']}", "", f"Client: {company['name']}", ""]
        for qq in q["questions"]:
            a = answers.get(qq["id"])
            lines += [f"## {qq['label']}", ", ".join(a) if isinstance(a, list) else (a or "(no answer)"), ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        try:
            brain.ingest("source", company["vault_slug"], path, label=f"Onboarding questionnaire {q['name']}")
        except brain.BrainError as exc:  # the answers are saved; the vault can be updated from the response page
            journal.failure("questionnaires", "add_to_vault", exc, subject_type="questionnaire_response",
                            subject_id=rid, company_id=company["id"])
    return redirect(f"/q/{token}?sent=1")
