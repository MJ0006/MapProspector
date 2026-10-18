"""Prospect audit (G1) and content tools (G2: any Local SEO Brain prompt → Claude draft)."""
import secrets

from fastapi import APIRouter, Form, HTTPException, Request

from app import ai, brain, db, jobs, journal, mailer, places_store
from app.modules.audit import checks
from app.web import redirect, render, row_or_404

router = APIRouter()

# Content tools offered on /tools, grouped. Keys are wiki/prompts/<name>.md in local-seo-brain.
# Every prompt in Local SEO Brain's wiki/prompts, in the order you'd use them (review steps follow their drafts).
TOOLS = {
    "Google Business Profile": ["gbp-description-claude-prompt-3", "gbp-description-claude-prompt-1",
                                "gbp-description-claude-prompt-2", "gbp-categories-prompt", "gbp-services-prompt"],
    "Website": ["prompt-generating-a-title-tag", "prompt-generating-a-meta-description", "ai-homepage-rewrite-prompt",
                "prompt-rewriting-existing-homepage", "prompt-rewriting-existing-service-page",
                "ai-supporting-pages-rewrite-prompt"],
    "Structured data": ["schema-prompt-1", "claude-prompt-1", "claude-prompt-2", "follow-up-prompt"],
    "Content plan": ["prompt-audience-avatar", "keyword-research-prompt", "keyword-variations-for-topical-relevance-prompt",
                     "content-planning-for-topical-relevance-prompt", "content-prioritization-prompt",
                     "prompt-core-30-content-audit", "paa-question-rewording-prompt"],
    "Articles": ["blog-post-outline-prompt", "blog-post-writing-prompt", "follow-up-prompt-1", "follow-up-prompt-2",
                 "ai-detector-test-follow-up-prompt"],
    "Audits": ["technical-audit-prompt", "ctr-audit-prompt", "property-content-with-authority-audit-prompt",
               "backlink-competition-prompt", "claude-deep-research-prompt"],
    "AI search": ["visiblity-prompt", "visibility-follow-up-prompt", "step-1-the-chatgpt-discovery-prompt",
                  "step-2-the-follow-up-qualifying-prompt", "reddit-claude-prompt"],
}
# Which current-doctrine note (references/canon) goes with each group: 001 Business Profile, 002 content and spam,
# 003 local, structured data and AI search.
DOCTRINE = {"Google Business Profile": 1, "Website": 2, "Content plan": 2, "Articles": 2}


def run_audit(audit_id: int) -> None:
    a = db.one("SELECT * FROM audits WHERE id = ?", audit_id)
    if not a or a["issues"] != -1:
        return
    place = places_store.get(a["place_id"])
    try:
        url, results = checks.run(place)
    except Exception as exc:  # never leave the page spinning
        db.update("audits", audit_id, issues=0, checks=[{"key": "error", "label": "Audit could not finish",
                  "group": "Website", "status": "fail", "detail": str(exc)[:300], "source": ""}])
        raise
    db.update("audits", audit_id, url=url, checks=results,
              issues=sum(1 for c in results if c["status"] == "fail"))
    contact = db.one("SELECT id FROM contacts WHERE place_id = ?", a["place_id"])
    if contact:
        db.insert("activities", contact_id=contact["id"], type="audit",
                  body=f"Website audit: {sum(1 for c in results if c['status'] == 'fail')} issues found",
                  created_at=db.now())


@router.post("/audit/run")
def audit_run(place_id: int = Form(...)):
    if not places_store.get(place_id):
        raise HTTPException(404)
    aid = db.insert("audits", place_id=place_id, checks=[], issues=-1, created_at=db.now())
    jobs.submit(run_audit, aid)
    return redirect(f"/audit/{aid}")


def _ctx(aid: int) -> dict:
    a = db.one("SELECT * FROM audits WHERE id = ?", aid)
    if not a:
        raise HTTPException(404)
    results = db.loads(a["checks"], [])
    groups: dict[str, list] = {}
    for c in results:
        groups.setdefault(c["group"], []).append(c)
    return {"a": a, "place": places_store.get(a["place_id"]), "groups": groups,
            "counts": {s: sum(1 for c in results if c["status"] == s) for s in ("pass", "fail", "warn", "na")},
            "contact": db.one("SELECT * FROM contacts WHERE place_id = ?", a["place_id"])}


@router.get("/audit/{aid}")
def audit_view(request: Request, aid: int):
    return render(request, "audit/view.html", title="Website audit", **_ctx(aid))


@router.post("/audit/{aid}/share")
def audit_share(aid: int):
    a = row_or_404("audits", aid)
    if not a["share_token"]:
        db.update("audits", aid, share_token=secrets.token_urlsafe(16))
    return redirect(f"/audit/{aid}#share")


@router.get("/share/audit/{token}")
def audit_shared(request: Request, token: str):
    a = db.one("SELECT id FROM audits WHERE share_token = ?", token)
    if not a:
        raise HTTPException(404)
    return render(request, "audit/share.html", title="Website & listing audit", **_ctx(a["id"]))


def outreach_prompt(place: dict, results: list[dict], sender: str) -> str:
    failed = [f"- {c['label']}: {c['detail']}" for c in results if c["status"] == "fail"][:6]
    warned = [f"- {c['label']}: {c['detail']}" for c in results if c["status"] == "warn"][:4]
    return "\n\n".join([
        f"Write a short, friendly cold email from {sender} (a local SEO consultant) to the owner of "
        f"{place['name']} ({place.get('primary_category') or 'local business'}, {place.get('address') or ''}).",
        "Mention one or two of these observed issues in plain words, offer to share the full audit, and ask for a "
        "short call. Under 120 words. No hype, no guarantees, no fake urgency, no claims you can't back.",
        "Observed failed checks:\n" + ("\n".join(failed) or "- none"),
        "Observed warnings:\n" + ("\n".join(warned) or "- none"),
        brain.GUARDRAILS,
        journal.prompt_block("outbox"),
        'Reply as JSON: {"subject": "...", "body": "..."}',
    ])


@router.post("/audit/{aid}/draft-email")
def audit_draft_email(aid: int):
    ctx = _ctx(aid)
    contact = ctx["contact"]
    if not contact:
        raise HTTPException(400, "Save this business as a lead first.")
    from app.web import brand
    try:
        draft = ai.ask_json(outreach_prompt(ctx["place"], db.loads(ctx["a"]["checks"], []), brand()["name"]),
                            purpose="outbox")
    except (ai.AIUnavailable, ValueError) as exc:
        raise HTTPException(503, f"Could not draft with AI: {exc}")
    oid = mailer.queue("email", contact["id"], contact["email"] or "", draft.get("subject", ""), draft.get("body", ""),
                       origin="audit")
    if oid:
        journal.record("outbox", "outcome", "draft", subject_type="outbox", subject_id=oid,
                       data={"text": db.one("SELECT body FROM outbox WHERE id = ?", oid)["body"],
                             "platform": ai.last_platform()})
    return redirect("/crm/outbox")


# --- content tools (G2) -----------------------------------------------------------------------

@router.get("/tools")
def tools(request: Request, prompt: str = "", company_id: int | None = None):
    notes = {name: brain.prompt_note(name) for group in TOOLS.values() for name in group
             if (brain.WIKI / "prompts" / f"{name}.md").exists()}
    company = db.one("SELECT * FROM companies WHERE id = ?", company_id) if company_id else None
    return render(request, "audit/tools.html", title="Content tools", groups=TOOLS, notes=notes,
                  selected=notes.get(prompt), company=company,
                  companies=db.all("SELECT id, name FROM companies ORDER BY name"))


@router.post("/tools/run")
def tools_run(request: Request, prompt: str = Form(...), business_name: str = Form(""), city: str = Form(""),
              services: str = Form(""), website: str = Form(""), category: str = Form(""),
              audience: str = Form(""), extra: str = Form(""), company_id: str = Form("")):
    facts = {"Business name": business_name, "City / service area": city, "Primary category": category,
             "Services": services, "Website": website, "Audience": audience,
             "Other confirmed facts, or the material to work on": extra}
    lessons = journal.prompt_block("tools", int(company_id) if company_id.isdigit() else None)
    try:
        full_prompt = brain.build_prompt(prompt, facts, doctrine_no=next(
            (DOCTRINE.get(g, 3) for g, names in TOOLS.items() if prompt in names), 3), extra=lessons)
    except brain.BrainError as exc:
        raise HTTPException(400, str(exc))
    error, output = "", ""
    try:
        output = ai.ask(full_prompt, purpose="tools")
    except ai.AIUnavailable as exc:
        error = str(exc)
    return render(request, "audit/tool_result.html", title="Draft", note=brain.prompt_note(prompt), output=output,
                  error=error, prompt_text=full_prompt, company_id=company_id)


@jobs.on_startup
def recover_audits() -> None:
    for a in db.all("SELECT id FROM audits WHERE issues = -1"):
        jobs.submit(run_audit, a["id"])
