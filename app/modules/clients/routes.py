"""Clients: convert a won lead into a client with its own Local SEO Brain vault, upload evidence,
and read the brain's Draft scorecard / roadmap / weekly report."""
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app import brain, db, markdown, places_store, settings
from app.uploads import read_limited
from app.web import redirect, render

router = APIRouter()

EVIDENCE = {
    "gbp": "Business Profile performance CSV (Date, Impressions, Search Views, Direction Requests, Phone Calls, Website Clicks)",
    "reviews": "Reviews export (JSON or CSV with rating, date, text)",
    "citations": "Citation export CSV (Whitespark, BrightLocal, Moz Local or directory,url,status)",
    "geogrid": "Geo-grid CSV (keyword, rank, lat, lng); MapProspector scans are added automatically",
    "source": "Any other document (notes, crawl export, strategy doc)",
}


def company_or_404(company_id: int) -> dict:
    c = db.one("SELECT * FROM companies WHERE id = ?", company_id)
    if not c:
        raise HTTPException(404, "Client not found")
    return c


def convert(contact_id: int) -> int:
    contact = db.one("SELECT * FROM contacts WHERE id = ?", contact_id)
    if not contact:
        raise HTTPException(404)
    if contact["company_id"]:
        co = db.one("SELECT * FROM companies WHERE id = ?", contact["company_id"])
        if co and co["is_client"]:
            return co["id"]
    place = places_store.get(contact["place_id"]) if contact["place_id"] else None
    name = contact["business_name"] or f"{contact['first_name'] or ''} {contact['last_name'] or ''}".strip() or "Client"
    slug = brain.slugify(name)
    n = 1
    # Also skip names whose vault folder exists: deleting a client keeps its vault, and a new client with the same
    # name must not inherit that evidence and plan.
    while db.one("SELECT 1 FROM companies WHERE vault_slug = ?", slug) or brain.vault_path(slug).exists():
        n += 1
        slug = f"{brain.slugify(name)}-{n}"
    cid = db.insert("companies", name=name, place_id=contact["place_id"], website=contact["website"] or (place or {}).get("website"),
                    phone=contact["phone"] or (place or {}).get("phone"), address=contact["address"] or (place or {}).get("address"),
                    is_client=1, vault_slug=slug, created_at=db.now(),
                    review_link=(f"https://search.google.com/local/writereview?placeid={place['google_place_id']}"
                                 if place and place.get("google_place_id") and not place["google_place_id"].startswith("demo-") else None))
    from app.web import brand
    brain.create_vault(slug, brand()["name"])
    db.update("contacts", contact_id, company_id=cid, status="client")
    db.insert("activities", contact_id=contact_id, type="stage", body=f"Converted to client \"{name}\"", created_at=db.now())
    return cid


@router.post("/clients/convert")
def clients_convert(contact_id: int = Form(...)):
    return redirect(f"/clients/{convert(contact_id)}")


@router.get("/clients")
def clients_list(request: Request):
    rows = db.all("SELECT c.*, p.rating, p.review_count, "
                  "(SELECT COUNT(*) FROM profile_changes x WHERE x.company_id = c.id AND x.status = 'open') changes, "
                  "(SELECT COUNT(*) FROM reviews r WHERE r.company_id = c.id AND r.reply_status IN ('none','draft')) unreplied "
                  "FROM companies c LEFT JOIN places p ON p.id = c.place_id WHERE c.is_client = 1 ORDER BY c.name")
    return render(request, "clients/list.html", title="Clients", rows=rows)


@router.post("/clients")
def clients_new(name: str = Form(...), website: str = Form(""), phone: str = Form("")):
    cid = db.insert("contacts", business_name=name, website=website, phone=phone, source="manual", status="client",
                    tags=[], created_at=db.now())
    return redirect(f"/clients/{convert(cid)}")


@router.get("/clients/{company_id}")
def client_overview(request: Request, company_id: int):
    co = company_or_404(company_id)
    slug = co["vault_slug"]
    scorecard = brain.deliverable(slug, "Health Scorecard") if slug else None
    roadmap = brain.deliverable(slug, "Action Roadmap") if slug else None
    report_path = brain.vault_path(slug) / f"{slug}-weekly-report.html" if slug else None
    from app.providers import gbp_api
    return render(request, "clients/overview.html", title=co["name"], co=co, tab="overview",
                  connected=gbp_api.connected(),
                  place=places_store.get(co["place_id"]) if co["place_id"] else None,
                  scorecard=markdown.render(scorecard) if scorecard else None,
                  roadmap=markdown.render(roadmap) if roadmap else None,
                  has_report=bool(report_path and report_path.exists()),
                  sources=brain.vault_sources(slug) if slug else [], evidence=EVIDENCE,
                  vault=str(brain.vault_path(slug)) if slug else None,
                  contacts=db.all("SELECT * FROM contacts WHERE company_id = ?", company_id),
                  scans=db.all("SELECT * FROM scans WHERE place_id = ? ORDER BY id DESC LIMIT 8", co["place_id"]) if co["place_id"] else [])


@router.post("/clients/{company_id}/settings")
def client_settings(company_id: int, name: str = Form(...), website: str = Form(""), phone: str = Form(""),
                    address: str = Form(""), review_link: str = Form(""), brand_voice: str = Form(""),
                    gsc_site: str = Form(""), gbp_location: str = Form("")):
    company_or_404(company_id)
    db.update("companies", company_id, name=name, website=website or None, phone=phone or None, address=address or None,
              review_link=review_link or None, brand_voice=brand_voice or None, gsc_site=gsc_site or None,
              gbp_location=gbp_location or None)
    return redirect(f"/clients/{company_id}")


@router.post("/clients/{company_id}/evidence")
async def client_evidence(company_id: int, kind: str = Form(...), label: str = Form(""), file: UploadFile = File(...)):
    co = company_or_404(company_id)
    if kind not in EVIDENCE:
        raise HTTPException(400)
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = brain.slugify(file.filename.rsplit(".", 1)[0]) + "." + (file.filename.rsplit(".", 1)[-1].lower()[:5] or "txt")
    path = settings.UPLOAD_DIR / f"{co['vault_slug']}-{kind}-{safe}"
    path.write_bytes(await read_limited(file, "evidence"))
    try:
        brain.ingest(kind, co["vault_slug"], path, label=label or file.filename)
        if kind == "reviews":
            from app.modules.gbp import reviews
            reviews.import_file(company_id, path)
    except brain.BrainError as exc:
        raise HTTPException(400, f"Local SEO Brain could not read that file: {exc}")
    return redirect(f"/clients/{company_id}")


@router.post("/clients/{company_id}/delete")
def client_delete(company_id: int):
    """Remove the client from the app. Its Local SEO Brain vault folder stays on disk."""
    co = company_or_404(company_id)
    db.run("UPDATE contacts SET company_id = NULL WHERE company_id = ?", company_id)
    db.run("DELETE FROM companies WHERE id = ?", company_id)
    from app import journal
    journal.record("clients", "decision", "delete_client", detail=f"{co['name']} (vault kept: {co['vault_slug']})")
    return redirect("/clients")


@router.post("/clients/{company_id}/synthesize")
def client_synthesize(company_id: int):
    co = company_or_404(company_id)
    try:
        brain.synthesize(co["vault_slug"])
        brain.render_report(co["vault_slug"])
    except brain.BrainError as exc:
        from app import journal
        journal.failure("clients", "update_plan", exc, company_id=company_id)
        raise HTTPException(502, f"Local SEO Brain could not update the plan: {exc}")
    return redirect(f"/clients/{company_id}")


@router.get("/clients/{company_id}/brain")
def client_brain(request: Request, company_id: int):
    """The Local SEO Brain's own checks for this client's vault: next action, vault health, evidence age, map."""
    co = company_or_404(company_id)
    if not co["vault_slug"]:
        raise HTTPException(404, "This business has no Local SEO Brain vault.")
    slug, error, action, health = co["vault_slug"], "", {"steps": [], "due": []}, None
    try:
        action = brain.next_action(slug)
        health = brain.lint(slug)
    except brain.BrainError as exc:
        error = str(exc)
    # Past-review notes are the Brain's reference copies (Google policy, prompts), grouped by folder.
    due: dict[str, list[str]] = {}
    for item in action["due"]:
        folder = item.split("/")[1] if item.startswith("wiki/") and item.count("/") >= 2 else "Google sources"
        due.setdefault(folder, []).append(item.rsplit("/", 1)[-1].removesuffix(".md"))
    from app.modules.audit.routes import TOOLS
    from app.providers import gbp_api
    return render(request, "clients/brain.html", title=f"{co['name']}: Brain", co=co, tab="brain",
                  connected=gbp_api.connected(), action=action, due=due, health=health, error=error,
                  evidence=brain.evidence_age(slug), max_days=brain.EVIDENCE_MAX_DAYS,
                  prompt_count=sum(len(v) for v in TOOLS.values()), vault=str(brain.vault_path(slug)))


@router.get("/clients/{company_id}/brain/map.svg")
def client_brain_map(company_id: int):
    co = company_or_404(company_id)
    if not co["vault_slug"]:
        raise HTTPException(404)
    try:
        path = brain.relationship_map(co["vault_slug"])
    except brain.BrainError as exc:
        raise HTTPException(502, str(exc))
    return FileResponse(path, media_type="image/svg+xml")


@router.get("/clients/{company_id}/weekly-report")
def client_weekly_report(company_id: int):
    co = company_or_404(company_id)
    path = brain.vault_path(co["vault_slug"]) / f"{co['vault_slug']}-weekly-report.html"
    if not path.exists():
        raise HTTPException(404, "Run 'Update plan' first.")
    return FileResponse(path, media_type="text/html")
