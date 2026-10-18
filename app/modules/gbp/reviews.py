"""Reviews (B2) and review requests (B3).

Review requests send EVERY customer to the same Google review link. There is no "funnel" that
routes unhappy customers away: review gating breaks Google's policy and local-seo-brain refuses it.
An optional private feedback box sits next to the review button, never instead of it.
"""
import csv
import hashlib
import json
import secrets
from pathlib import Path

from app import ai, brain, db, settings
from app.providers import gbp_api


def _hash(name: str | None) -> str | None:
    return hashlib.sha256(name.encode()).hexdigest()[:16] if name else None


def import_file(company_id: int, path: Path) -> int:
    """Google-style JSON list or CSV with rating/date/text (+ optional id/author)."""
    text = path.read_text(errors="replace")
    rows = json.loads(text) if path.suffix.lower() == ".json" else list(csv.DictReader(text.splitlines()))
    n = 0
    for i, r in enumerate(rows):
        rating = r.get("rating") or r.get("stars") or r.get("starRating")
        try:
            rating = int(float(rating)) if rating not in (None, "") else None
        except ValueError:
            rating = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}.get(str(rating).upper())
        ext = str(r.get("id") or r.get("review_id") or hashlib.md5(f"{r.get('date')}{r.get('text')}".encode()).hexdigest()[:12])
        if db.one("SELECT 1 FROM reviews WHERE company_id = ? AND external_id = ?", company_id, ext):
            continue
        db.insert("reviews", company_id=company_id, external_id=ext, author_hash=_hash(r.get("author") or r.get("name")),
                  rating=rating, text=r.get("text") or r.get("comment") or "", review_date=r.get("date") or r.get("createTime"),
                  reply_status="published" if r.get("reply") else "none", reply_text=r.get("reply"), created_at=db.now())
        n += 1
    return n


def sync_from_google(company_id: int) -> int:
    """Pull reviews through the Business Profile API (connected mode). Returns new review count."""
    co = db.one("SELECT * FROM companies WHERE id = ?", company_id)
    if not co["gbp_location"]:
        raise gbp_api.GBPError("Link this client to a Google location first.")
    stars = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
    n = 0
    for r in gbp_api.list_reviews(co["gbp_location"]):
        ext = r.get("reviewId") or r.get("name", "").rsplit("/", 1)[-1]
        if db.one("SELECT 1 FROM reviews WHERE company_id = ? AND external_id = ?", company_id, ext):
            continue
        reply = (r.get("reviewReply") or {}).get("comment")
        db.insert("reviews", company_id=company_id, external_id=ext,
                  author_hash=_hash((r.get("reviewer") or {}).get("displayName")), rating=stars.get(r.get("starRating")),
                  text=r.get("comment") or "", review_date=(r.get("createTime") or "")[:10],
                  reply_status="published" if reply else "none", reply_text=reply, created_at=db.now())
        n += 1
    return n


def reply_prompt(company: dict, review: dict) -> str:
    voice = company.get("brand_voice") or "warm, professional, concise"
    return "\n\n".join([
        f"Draft a reply from {company['name']} to this Google review. Tone: {voice}.",
        f"Rating: {review['rating'] or 'unknown'} stars\nReview text: \"{review['text'] or '(no text)'}\"",
        "Refer to what the customer actually said. Thank them. For a negative review, apologise without admitting "
        "liability, offer to make it right offline, and give no private details. 2–4 sentences. Do not mention "
        "the reviewer's name. Do not ask them to change their rating. Do not offer incentives.",
        brain.GUARDRAILS,
        "Reply with only the reply text.",
    ])


def draft_reply(review_id: int) -> str:
    rv = db.one("SELECT * FROM reviews WHERE id = ?", review_id)
    co = db.one("SELECT * FROM companies WHERE id = ?", rv["company_id"])
    text = ai.ask(reply_prompt(co, rv))
    db.update("reviews", review_id, reply_draft=text, reply_status="draft")
    return text


def publish_reply(review_id: int, text: str) -> bool:
    """Returns True when posted to Google; False when saved for you to paste manually."""
    rv = db.one("SELECT * FROM reviews WHERE id = ?", review_id)
    co = db.one("SELECT * FROM companies WHERE id = ?", rv["company_id"])
    if gbp_api.connected() and co["gbp_location"]:
        gbp_api.reply_review(co["gbp_location"], rv["external_id"], text)
        db.update("reviews", review_id, reply_text=text, reply_status="published", replied_at=db.now())
        return True
    db.update("reviews", review_id, reply_text=text, reply_status="approved")
    return False


def stats(company_id: int) -> dict:
    rows = db.all("SELECT rating, reply_status FROM reviews WHERE company_id = ?", company_id)
    rated = [r["rating"] for r in rows if r["rating"]]
    reqs = db.all("SELECT status, opened_at, clicked_at FROM review_requests WHERE company_id = ?", company_id)
    return {
        "count": len(rows), "avg": round(sum(rated) / len(rated), 2) if rated else None,
        "dist": {s: sum(1 for r in rated if r == s) for s in range(5, 0, -1)},
        "unreplied": sum(1 for r in rows if r["reply_status"] in ("none", "draft")),
        "requests": len(reqs), "opened": sum(1 for r in reqs if r["opened_at"]),
        "clicked": sum(1 for r in reqs if r["clicked_at"]),
    }


def create_request(company_id: int, contact_id: int | None, channel: str = "link") -> str:
    token = secrets.token_urlsafe(9)
    db.insert("review_requests", company_id=company_id, contact_id=contact_id, token=token, channel=channel,
              status="sent" if channel in ("email", "sms") else "created", created_at=db.now())
    return f"{settings.PUBLIC_BASE_URL}/r/{token}"
