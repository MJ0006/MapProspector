"""GBP management pages per client (B1–B8) + Google OAuth + public review-request pages."""
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from app import ai, db, journal, mailer, places_store, settings
from app.modules.clients.routes import company_or_404
from app.modules.gbp import lock, photos, posts, reviews
from app.providers import gbp_api
from app.uploads import read_limited
from app.web import redirect, render

router = APIRouter()


def _page(request, name, co, tab, **ctx):
    return render(request, name, title=co["name"], co=co, tab=tab, connected=gbp_api.connected(), **ctx)


# --- OAuth ------------------------------------------------------------------------------------

@router.get("/gbp/oauth/start")
def oauth_start():
    if not settings.enabled("google_oauth"):
        raise HTTPException(400, "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env first.")
    state = secrets.token_urlsafe(16)
    db.set_setting("oauth_state", state)
    return RedirectResponse(gbp_api.auth_url(state))


@router.get("/gbp/oauth/callback")
def oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error or not code or state != db.get_setting("oauth_state"):
        raise HTTPException(400, f"Google sign-in was not completed. {error}")
    try:
        gbp_api.exchange(code)
    except gbp_api.GBPError as exc:
        raise HTTPException(502, str(exc))
    return redirect("/settings?saved=1")


@router.get("/clients/{company_id}/link")
def link_page(request: Request, company_id: int):
    co = company_or_404(company_id)
    locs, sites, error = [], [], ""
    try:
        locs = gbp_api.list_locations()
    except gbp_api.GBPError as exc:
        error = str(exc)
    try:
        sites = gbp_api.gsc_sites()
    except gbp_api.GBPError as exc:
        error = error or str(exc)
    return _page(request, "gbp/link.html", co, "overview", locations=locs, sites=sites, error=error)


# --- B1 posts --------------------------------------------------------------------------------

@router.get("/clients/{company_id}/posts")
def posts_page(request: Request, company_id: int):
    co = company_or_404(company_id)
    rows = db.all("SELECT * FROM gbp_posts WHERE company_id = ? ORDER BY COALESCE(scheduled_at, created_at) DESC", company_id)
    return _page(request, "gbp/posts.html", co, "posts", rows=rows, ctas=posts.CTAS)


@router.post("/clients/{company_id}/posts/draft")
def posts_draft(company_id: int, topic: str = Form(...), tone: str = Form(""), length: str = Form("short"),
                facts: str = Form(""), count: int = Form(1)):
    company_or_404(company_id)
    try:
        for i in range(max(1, min(count, 8))):
            posts.draft(company_id, topic if count == 1 else f"{topic} (variation {i + 1} of {count}, different angle)",
                        tone, length, facts)
    except ai.AIUnavailable as exc:
        raise HTTPException(503, str(exc))
    return redirect(f"/clients/{company_id}/posts")


@router.post("/clients/{company_id}/posts/manual")
def posts_manual(company_id: int, body: str = Form(...)):
    company_or_404(company_id)
    db.insert("gbp_posts", company_id=company_id, topic="manual", body=body, status="draft", created_at=db.now())
    return redirect(f"/clients/{company_id}/posts")


@router.post("/gbp/posts/{post_id}/{action}")
def post_action(post_id: int, action: str, body: str = Form(None), cta: str = Form(""), cta_url: str = Form(""),
                scheduled_at: str = Form("")):
    p = db.one("SELECT * FROM gbp_posts WHERE id = ?", post_id)
    if not p:
        raise HTTPException(404)
    if body is not None:
        db.update("gbp_posts", post_id, body=body, cta=cta or None, cta_url=cta_url or None)
    ai_text = journal.last_draft("post", post_id)
    if ai_text is not None and p["status"] in ("draft", "failed") and action in ("approve", "schedule", "publish",
                                                                                  "mark_published", "delete"):
        journal.decision("posts", action, draft=ai_text, final=None if action == "delete" else (body or p["body"]),
                         subject_type="post", subject_id=post_id, company_id=p["company_id"])
    if action == "approve":
        db.update("gbp_posts", post_id, status="approved")
    elif action == "schedule":
        if not scheduled_at:
            raise HTTPException(400, "Pick a date and time")
        try:
            when = datetime.fromisoformat(scheduled_at).astimezone(timezone.utc)
        except ValueError:
            raise HTTPException(400, "Pick a valid date and time for the post.")
        db.update("gbp_posts", post_id, status="scheduled", scheduled_at=when.isoformat(timespec="seconds"))
    elif action == "publish":
        db.update("gbp_posts", post_id, status="approved")
        try:
            posts.publish(post_id)
        except gbp_api.GBPError as exc:
            raise HTTPException(502, str(exc))
    elif action == "mark_published":
        db.update("gbp_posts", post_id, status="published", published_at=db.now())
    elif action == "delete":
        db.run("DELETE FROM gbp_posts WHERE id = ?", post_id)
    return redirect(f"/clients/{p['company_id']}/posts")


# --- B2/B3 reviews ----------------------------------------------------------------------------

@router.get("/clients/{company_id}/reviews")
def reviews_page(request: Request, company_id: int, show: str = "todo"):
    co = company_or_404(company_id)
    where = "reply_status IN ('none','drafting','draft','approved')" if show == "todo" else "1 = 1"
    rows = db.all(f"SELECT * FROM reviews WHERE company_id = ? AND {where} ORDER BY review_date DESC LIMIT 300", company_id)
    reqs = db.all("SELECT r.*, c.business_name, c.first_name FROM review_requests r LEFT JOIN contacts c "
                  "ON c.id = r.contact_id WHERE r.company_id = ? ORDER BY r.id DESC LIMIT 50", company_id)
    fb = db.all("SELECT * FROM feedback WHERE company_id = ? ORDER BY id DESC LIMIT 20", company_id)
    return _page(request, "gbp/reviews.html", co, "reviews", rows=rows, show=show, stats=reviews.stats(company_id),
                 requests=reqs, feedback=fb, base=settings.PUBLIC_BASE_URL,
                 contacts=db.all("SELECT id, first_name, last_name, business_name, email, phone FROM contacts "
                                 "WHERE company_id = ? AND is_spam = 0", company_id))


@router.post("/clients/{company_id}/reviews/sync")
def reviews_sync(company_id: int):
    try:
        reviews.sync_from_google(company_id)
    except gbp_api.GBPError as exc:
        raise HTTPException(400, str(exc))
    return redirect(f"/clients/{company_id}/reviews")


@router.post("/clients/{company_id}/reviews/draft-all")
def reviews_draft_all(company_id: int):
    ids = [r["id"] for r in db.all("SELECT id FROM reviews WHERE company_id = ? AND reply_status = 'none' LIMIT 15", company_id)]
    try:
        reviews.queue_drafts(ids)
    except ai.AIUnavailable as exc:
        raise HTTPException(503, str(exc))
    return redirect(f"/clients/{company_id}/reviews")


@router.post("/gbp/reviews/{review_id}/{action}")
def review_action(review_id: int, action: str, text: str = Form("")):
    rv = db.one("SELECT * FROM reviews WHERE id = ?", review_id)
    if not rv:
        raise HTTPException(404)
    if action == "draft":
        try:
            reviews.queue_drafts([review_id])
        except ai.AIUnavailable as exc:
            raise HTTPException(503, str(exc))
    elif action in ("approve", "mark_replied"):
        final = text or rv["reply_draft"] or ""
        if rv["reply_draft"]:
            journal.decision("reviews", action, draft=rv["reply_draft"], final=final, subject_type="review",
                             subject_id=review_id, company_id=rv["company_id"])
        if action == "approve":
            try:
                reviews.publish_reply(review_id, final)
            except gbp_api.GBPError as exc:
                journal.failure("reviews", "publish_reply", exc, subject_type="review", subject_id=review_id,
                                company_id=rv["company_id"])
                raise HTTPException(502, str(exc))
        else:
            db.update("reviews", review_id, reply_status="published", reply_text=final, replied_at=db.now())
    return redirect(f"/clients/{rv['company_id']}/reviews")


@router.post("/clients/{company_id}/review-requests")
def review_request_new(company_id: int, contact_id: str = Form(""), message: str = Form("")):
    co = company_or_404(company_id)
    if not co["review_link"]:
        raise HTTPException(400, "Add the client's Google review link first (client settings).")
    cid = int(contact_id) if contact_id else None
    contact = db.one("SELECT * FROM contacts WHERE id = ?", cid) if cid else None
    channel = "email" if contact and contact["email"] else "sms" if contact and contact["phone"] else "link"
    link = reviews.create_request(company_id, cid, channel)
    if contact and channel != "link":
        from app.modules.crm.automations import render_vars
        body = render_vars(message or "Hi {{first_name}},\n\nThanks for choosing {{company_name}}! Would you share "
                           "a quick review of your experience? {{review_link}}", {**contact, "custom": {}, "company_id": company_id},
                           {"review_link": link})
        # A customer the client already served: still goes through the outbox for your approval.
        mailer.queue(channel, cid, contact["email"] or contact["phone"], "How did we do?", body, origin="review_request")
    return redirect(f"/clients/{company_id}/reviews#requests")


@router.get("/r/{token}")
def review_landing(request: Request, token: str):
    rr = db.one("SELECT * FROM review_requests WHERE token = ?", token)
    if not rr or rr["status"] == "stopped":
        raise HTTPException(404)
    if not rr["opened_at"]:
        db.run("UPDATE review_requests SET opened_at = ?, status = 'opened' WHERE id = ?", db.now(), rr["id"])
    co = db.one("SELECT * FROM companies WHERE id = ?", rr["company_id"])
    return render(request, "gbp/review_public.html", title=f"Review {co['name']}", co=co, token=token,
                  sent=request.query_params.get("sent"))


@router.get("/r/{token}/go")
def review_go(token: str):
    rr = db.one("SELECT * FROM review_requests WHERE token = ?", token)
    if not rr or rr["status"] == "stopped":
        raise HTTPException(404)
    co = db.one("SELECT review_link FROM companies WHERE id = ?", rr["company_id"])
    if not co or not (co["review_link"] or "").startswith("https://"):
        raise HTTPException(404, "This review link isn't set up yet. Please contact the business directly.")
    db.run("UPDATE review_requests SET clicked_at = COALESCE(clicked_at, ?), status = 'clicked' WHERE id = ?",
           db.now(), rr["id"])
    return RedirectResponse(co["review_link"], status_code=302)


@router.post("/r/{token}/feedback")
def review_feedback(token: str, message: str = Form(...)):
    rr = db.one("SELECT * FROM review_requests WHERE token = ?", token)
    if not rr or rr["status"] == "stopped":
        raise HTTPException(404)
    db.insert("feedback", company_id=rr["company_id"], request_id=rr["id"], message=message[:3000], created_at=db.now())
    return redirect(f"/r/{token}?sent=1")


# --- B4 photos -----------------------------------------------------------------------------------

@router.get("/clients/{company_id}/photos")
def photos_page(request: Request, company_id: int):
    co = company_or_404(company_id)
    rows = db.all("SELECT * FROM photos WHERE company_id = ? ORDER BY COALESCE(scheduled_at, created_at)", company_id)
    place = places_store.get(co["place_id"]) if co["place_id"] else None
    return _page(request, "gbp/photos.html", co, "photos", rows=rows, categories=photos.CATEGORIES, place=place)


@router.post("/clients/{company_id}/photos")
async def photos_upload(company_id: int, files: list[UploadFile] = File(...), caption: str = Form(""),
                        category: str = Form("ADDITIONAL"), stamp_gps: str = Form(""), lat: str = Form(""),
                        lng: str = Form("")):
    co = company_or_404(company_id)
    gps = None
    if stamp_gps and (lat.strip() or lng.strip()):
        try:
            gps = (float(lat), float(lng))
        except ValueError:
            gps = None
        if not gps or not (-90 <= gps[0] <= 90 and -180 <= gps[1] <= 180):
            raise HTTPException(400, "Latitude and longitude must be numbers like 39.7392 and -104.9903.")
    folder = photos.photo_dir(company_id)
    for f in files:
        if Path(f.filename or "").suffix.lower() not in photos.ALLOWED:
            continue
        name = photos.unique_name(folder, co["name"], caption, f.filename)
        path = folder / name
        path.write_bytes(await read_limited(f, "photo"))
        glat = glng = None
        if gps and photos.stamp_gps(path, *gps):
            glat, glng = gps
        db.insert("photos", company_id=company_id, filename=name, original_name=f.filename, caption=caption,
                  category=category if category in photos.CATEGORIES else "ADDITIONAL", gps_lat=glat, gps_lng=glng,
                  status="library", created_at=db.now())
    return redirect(f"/clients/{company_id}/photos")


@router.post("/clients/{company_id}/photos/schedule")
async def photos_schedule(request: Request, company_id: int):
    form = await request.form()
    try:
        ids = [int(x) for x in form.getlist("photo_id")]
        start = datetime.fromisoformat(form.get("start") or datetime.now().isoformat(timespec="minutes")).astimezone(timezone.utc)
        every = max(0, min(365, int(form.get("every_days") or 0)))
    except ValueError:
        raise HTTPException(400, "Pick a valid start date/time and a whole number of days.")
    photos.schedule_drip(company_id, ids, start, every)
    return redirect(f"/clients/{company_id}/photos")


@router.post("/gbp/photos/{photo_id}/{action}")
def photo_action(photo_id: int, action: str):
    p = db.one("SELECT * FROM photos WHERE id = ?", photo_id)
    if not p:
        raise HTTPException(404)
    if action == "publish":
        try:
            if photos.publish(photo_id) == "manual":
                raise HTTPException(400, "Connect Google and link a location to publish; or upload it in Google yourself.")
        except gbp_api.GBPError as exc:
            raise HTTPException(502, str(exc))
    elif action == "mark_published":
        db.update("photos", photo_id, status="published", published_at=db.now())
    elif action == "unschedule":
        db.update("photos", photo_id, status="library", scheduled_at=None)
    elif action == "delete":
        (photos.photo_dir(p["company_id"]) / p["filename"]).unlink(missing_ok=True)
        db.run("DELETE FROM photos WHERE id = ?", photo_id)
    return redirect(f"/clients/{p['company_id']}/photos")


@router.get("/media/photos/{company_id}/{filename}")
def photo_file(company_id: int, filename: str):
    p = db.one("SELECT * FROM photos WHERE company_id = ? AND filename = ?", company_id, filename)
    if not p:
        raise HTTPException(404)
    return FileResponse(photos.photo_dir(company_id) / p["filename"])


# --- B5 profile lock -----------------------------------------------------------------------------

@router.get("/clients/{company_id}/lock")
def lock_page(request: Request, company_id: int):
    co = company_or_404(company_id)
    changes = db.all("SELECT * FROM profile_changes WHERE company_id = ? ORDER BY status = 'open' DESC, id DESC LIMIT 100",
                     company_id)
    return _page(request, "gbp/lock.html", co, "lock", snapshot=lock.approved_snapshot(company_id), changes=changes,
                 fields=lock.FIELDS, revertible=lock.REVERTIBLE)


@router.post("/clients/{company_id}/lock/{action}")
def lock_action(company_id: int, action: str):
    company_or_404(company_id)
    try:
        if action == "approve":
            lock.approve(company_id)
        elif action == "check":
            lock.check(company_id)
    except (ValueError, Exception) as exc:  # Places errors surface to the user
        raise HTTPException(400, str(exc))
    return redirect(f"/clients/{company_id}/lock")


@router.post("/gbp/changes/{change_id}/{action}")
def change_action(change_id: int, action: str):
    ch = db.one("SELECT * FROM profile_changes WHERE id = ?", change_id)
    if not ch:
        raise HTTPException(404)
    if action == "revert":
        try:
            lock.revert(change_id)
        except gbp_api.GBPError as exc:
            raise HTTPException(502, str(exc))
    elif action == "fixed":
        db.update("profile_changes", change_id, status="reverted", resolved_at=db.now())
    elif action == "accept":
        snap = lock.approved_snapshot(ch["company_id"]) or {}
        snap[ch["field"]] = ch["live_value"]
        db.update("profile_changes", change_id, status="accepted", resolved_at=db.now())
        db.run("UPDATE profile_snapshots SET approved = 0 WHERE company_id = ?", ch["company_id"])
        db.insert("profile_snapshots", company_id=ch["company_id"], data=snap, approved=1, created_at=db.now())
    elif action == "dismiss":
        db.update("profile_changes", change_id, status="dismissed", resolved_at=db.now())
    return redirect(f"/clients/{ch['company_id']}/lock")


# --- B6/B7 insights + Search Console ------------------------------------------------------------

def perf_summary(series: dict, start: date, end: date) -> dict:
    """Totals per metric for the range plus the equal-length prior range, with % change."""
    def total(names, lo, hi):
        return sum(v for n in names for d, v in series.get(n, {}).items() if lo.isoformat() <= d <= hi.isoformat())
    span = end - start
    prior_end, prior_start = start - timedelta(days=1), start - timedelta(days=1) - span
    groups = {"Appearances (Maps)": ["BUSINESS_IMPRESSIONS_DESKTOP_MAPS", "BUSINESS_IMPRESSIONS_MOBILE_MAPS"],
              "Appearances (Search)": ["BUSINESS_IMPRESSIONS_DESKTOP_SEARCH", "BUSINESS_IMPRESSIONS_MOBILE_SEARCH"],
              "Calls": ["CALL_CLICKS"], "Website clicks": ["WEBSITE_CLICKS"], "Direction requests": ["BUSINESS_DIRECTION_REQUESTS"]}
    out = {}
    for label, names in groups.items():
        cur, prev = total(names, start, end), total(names, prior_start, prior_end)
        out[label] = {"current": cur, "prior": prev, "change_pct": round(100 * (cur - prev) / prev, 1) if prev else None}
    return out


@router.get("/clients/{company_id}/insights")
def insights_page(request: Request, company_id: int, days: int = 28):
    days = max(1, min(days, 540))  # Google keeps about 18 months of performance data
    co = company_or_404(company_id)
    end = date.today() - timedelta(days=3)  # Google data lags a few days
    start = end - timedelta(days=days - 1)
    perf, queries, error = None, [], ""
    if gbp_api.connected() and co["gbp_location"]:
        try:
            series = gbp_api.performance(co["gbp_location"], (start - timedelta(days=days)).isoformat(), end.isoformat())
            perf = perf_summary(series, start, end)
        except gbp_api.GBPError as exc:
            error = str(exc)
    if gbp_api.connected() and co["gsc_site"]:
        try:
            queries = gbp_api.gsc_queries(co["gsc_site"], start.isoformat(), end.isoformat())
        except gbp_api.GBPError as exc:
            error = error or str(exc)
    return _page(request, "gbp/insights.html", co, "insights", perf=perf, queries=queries, error=error, days=days,
                 start=start, end=end)


# --- B8 optimisation ------------------------------------------------------------------------------

@router.get("/clients/{company_id}/optimize")
def optimize_page(request: Request, company_id: int):
    co = company_or_404(company_id)
    place = places_store.get(co["place_id"]) if co["place_id"] else None
    items = []
    if place:
        items = [
            ("Primary category", bool(place.get("primary_category")), place.get("primary_category")),
            ("Website", bool(place.get("website")), place.get("website")),
            ("Phone", bool(place.get("phone")), place.get("phone")),
            ("Opening hours", bool(place.get("hours")), "; ".join(place.get("hours") or [])),
            ("10+ photos", (place.get("photos_count") or 0) >= 10,
             "10 or more photos" if (place.get("photos_count") or 0) >= 10 else f"{place.get('photos_count') or 0} photos"),
            ("Reviews", (place.get("review_count") or 0) >= 25, f"{place.get('review_count') or 0} reviews, {place.get('rating') or '—'}★"),
        ]
    return _page(request, "gbp/optimize.html", co, "optimize", place=place, items=items)
