import csv
import io
import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import Response

from app import brain, db, jobs, places_store, settings
from app.modules.citations import engine
from app.modules.clients.routes import company_or_404
from app.providers import gbp_api
from app.web import redirect, render

router = APIRouter()


@router.get("/citations")
def citations_home(request: Request, place_id: int | None = None):
    place = places_store.get(place_id) if place_id else None
    audits = db.all("SELECT * FROM citation_audits ORDER BY id DESC LIMIT 50")
    for a in audits:
        a["business"] = db.loads(a["business"], {})
    nap = engine.profile_from(place["name"], place.get("address"), place.get("phone"), place.get("website")) if place else None
    return render(request, "citations/home.html", title="Citation audit", audits=audits, nap=nap, place=place,
                  verticals=engine.VERTICALS, saved=db.all("SELECT id, name FROM places ORDER BY name LIMIT 300"))


@router.post("/citations")
def citations_start(request: Request, name: str = Form(...), street: str = Form(""), city: str = Form(""),
                    state: str = Form(""), zip: str = Form(""), phone: str = Form(""), website: str = Form(""),
                    vertical: str = Form(""), place_id: str = Form(""), company_id: str = Form("")):
    nap = {"name": name, "street": street, "city": city, "state": state, "zip": zip, "phone": phone, "website": website}
    aid = engine.create_audit(nap, company_id=int(company_id) if company_id else None,
                              place_id=int(place_id) if place_id else None, vertical=vertical or None)
    from app import ai
    if ai.available():
        jobs.submit(engine.run_audit, aid)
    else:
        db.update("citation_audits", aid, status="manual")
    return redirect(f"/citations/{aid}")


def _audit_ctx(aid: int) -> dict:
    a = db.one("SELECT * FROM citation_audits WHERE id = ?", aid)
    if not a:
        raise HTTPException(404)
    a["business"] = db.loads(a["business"], {})
    listings = db.all("SELECT * FROM citation_listings WHERE audit_id = ? ORDER BY CASE status WHEN 'wrong' THEN 0 "
                      "WHEN 'missing' THEN 1 WHEN 'live' THEN 2 ELSE 3 END, directory", aid)
    for l in listings:
        l["fields_d"] = db.loads(l["fields"], {}) or {}
    return {"a": a, "listings": listings, "sum": engine.summary(listings), "field_names": engine.FIELDS}


@router.get("/citations/{aid}")
def citations_view(request: Request, aid: int):
    return render(request, "citations/audit.html", title="Citation audit", **_audit_ctx(aid))


@router.post("/citations/{aid}/rerun")
def citations_rerun(aid: int):
    db.run("UPDATE citation_listings SET status = 'pending' WHERE audit_id = ? AND status IN ('unavailable','missing')", aid)
    jobs.submit(engine.run_audit, aid)
    return redirect(f"/citations/{aid}")


@router.post("/citations/listings/{lid}")
def listing_update(lid: int, status: str = Form(...), listing_url: str = Form(""), name: str = Form(""),
                   address: str = Form(""), phone: str = Form(""), website: str = Form(""),
                   submission_status: str = Form("")):
    l = db.one("SELECT * FROM citation_listings WHERE id = ?", lid)
    if not l:
        raise HTTPException(404)
    cols = {"listing_url": listing_url or l["listing_url"], "checked_by": "manual", "updated_at": db.now()}
    if submission_status:
        cols["submission_status"] = submission_status
    if any([name, address, phone, website]) and l["audit_id"]:
        nap = db.loads(db.one("SELECT business FROM citation_audits WHERE id = ?", l["audit_id"])["business"], {})
        fields = engine.compare(nap, {"name": name, "address": address, "phone": phone, "website": website})
        cols.update(fields=fields, status=engine.listing_status(fields))
    else:
        cols["status"] = status
    db.update("citation_listings", lid, **cols)
    if l["audit_id"]:
        engine.finish(l["audit_id"])
    back = f"/clients/{l['company_id']}/citations" if l["company_id"] else f"/citations/{l['audit_id']}"
    return redirect(back)


@router.post("/citations/{aid}/share")
def citations_share(aid: int):
    a = db.one("SELECT * FROM citation_audits WHERE id = ?", aid)
    if not a:
        raise HTTPException(404)
    if not a["share_token"]:
        db.update("citation_audits", aid, share_token=secrets.token_urlsafe(16))
    return redirect(f"/citations/{aid}#share")


@router.get("/share/citations/{token}")
def citations_shared(request: Request, token: str):
    a = db.one("SELECT id FROM citation_audits WHERE share_token = ?", token)
    if not a:
        raise HTTPException(404)
    return render(request, "citations/share.html", title="Citation audit", **_audit_ctx(a["id"]))


# --- per-client profile + tracker (C2) -------------------------------------------------------------

@router.get("/clients/{company_id}/citations")
def client_citations(request: Request, company_id: int):
    co = company_or_404(company_id)
    audit = db.one("SELECT * FROM citation_audits WHERE company_id = ? ORDER BY id DESC LIMIT 1", company_id)
    ctx = _audit_ctx(audit["id"]) if audit else {"a": None, "listings": [], "sum": None, "field_names": engine.FIELDS}
    return render(request, "citations/client.html", title=co["name"], co=co, tab="citations",
                  connected=gbp_api.connected(), nap=engine.profile(company_id), keys=engine.PROFILE_KEYS,
                  verticals=engine.VERTICALS, **ctx)


@router.post("/clients/{company_id}/citations/profile")
async def client_citation_profile(request: Request, company_id: int):
    company_or_404(company_id)
    engine.save_profile(company_id, dict(await request.form()))
    return redirect(f"/clients/{company_id}/citations")


@router.post("/clients/{company_id}/citations/audit")
def client_citation_audit(company_id: int, vertical: str = Form("")):
    co = company_or_404(company_id)
    aid = engine.create_audit(engine.profile(company_id), company_id=company_id, place_id=co["place_id"],
                              vertical=vertical or None)
    from app import ai
    if ai.available():
        jobs.submit(engine.run_audit, aid)
    return redirect(f"/clients/{company_id}/citations")


@router.get("/clients/{company_id}/citations/nap.csv")
def client_nap_csv(company_id: int):
    """One-row NAP sheet for manual directory submissions."""
    nap = engine.profile(company_id)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=engine.PROFILE_KEYS)
    w.writeheader()
    w.writerow(nap)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="nap-{company_id}.csv"'})


@router.get("/clients/{company_id}/citations/export.csv")
def client_citations_export(company_id: int):
    """Directory,url,status: the format vendor/local-seo-brain/scripts/ingest_citations.py reads."""
    audit = db.one("SELECT id FROM citation_audits WHERE company_id = ? ORDER BY id DESC LIMIT 1", company_id)
    rows = db.all("SELECT directory, listing_url, status FROM citation_listings WHERE audit_id = ?", audit["id"]) if audit else []
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["directory", "url", "status"])
    for r in rows:
        w.writerow([r["directory"], r["listing_url"] or "", {"live": "verified", "wrong": "inconsistent"}.get(r["status"], r["status"])])
    return Response(buf.getvalue(), media_type="text/csv")


@router.post("/clients/{company_id}/citations/to-brain")
def client_citations_to_brain(company_id: int):
    co = company_or_404(company_id)
    path = settings.UPLOAD_DIR / f"citations-{company_id}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(client_citations_export(company_id).body.decode())
    brain.ingest("citations", co["vault_slug"], path)
    return redirect(f"/clients/{company_id}/citations")
