"""AI business posts (B1): prime with company facts → choose topic/tone/length → Claude drafts →
you review/approve → publish now or on a schedule (connected mode) or copy into Google yourself."""
from datetime import datetime, timezone

from app import ai, brain, db, jobs
from app.providers import gbp_api

CTAS = {"": "No button", "CALL": "Call now", "LEARN_MORE": "Learn more", "BOOK": "Book", "ORDER": "Order online",
        "SHOP": "Shop", "SIGN_UP": "Sign up"}


def post_prompt(company: dict, topic: str, tone: str, length: str, facts: str) -> str:
    return "\n\n".join([
        f"Write one Google Business Profile update post for {company['name']}.",
        f"Topic: {topic}. Tone: {tone or company.get('brand_voice') or 'friendly and plain'}. "
        f"Length: {length or 'short'} (Google allows up to 1,500 characters; aim for 80–250 words).",
        "Company facts you may use (nothing else):\n" + (facts or "- name: " + company["name"]),
        "Write for customers, not search engines: no keyword lists, no hashtags spam, no phone numbers in the text "
        "(Google rejects posts containing phone numbers), no prices or offers unless given above.",
        "Brain chapter on posts:\n" + brain.chapter("gbp/gbp-posts", 1200),
        brain.GUARDRAILS,
        "Reply with only the post text.",
    ])


def company_facts(company: dict) -> str:
    lines = [f"- name: {company['name']}"]
    for k in ("website", "address", "brand_voice"):
        if company.get(k):
            lines.append(f"- {k}: {company[k]}")
    return "\n".join(lines)


def draft(company_id: int, topic: str, tone: str = "", length: str = "", extra_facts: str = "") -> int:
    co = db.one("SELECT * FROM companies WHERE id = ?", company_id)
    facts = company_facts(co) + ("\n" + extra_facts if extra_facts else "")
    text = ai.ask(post_prompt(co, topic, tone, length, facts))
    return db.insert("gbp_posts", company_id=company_id, topic=topic, body=text.strip(), status="draft", created_at=db.now())


def publish(post_id: int) -> str:
    """Publish an approved post. Returns 'published', 'manual' (no API) or raises."""
    p = db.one("SELECT * FROM gbp_posts WHERE id = ?", post_id)
    co = db.one("SELECT * FROM companies WHERE id = ?", p["company_id"])
    if not (gbp_api.connected() and co["gbp_location"]):
        return "manual"
    try:
        gbp_api.create_post(co["gbp_location"], p["body"], p["cta"] or None, p["cta_url"] or None)
    except gbp_api.GBPError as exc:
        db.update("gbp_posts", post_id, status="failed", error=str(exc)[:300])
        raise
    db.update("gbp_posts", post_id, status="published", published_at=db.now(), error=None)
    return "published"


@jobs.on_tick
def publish_scheduled() -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for p in db.all("SELECT id FROM gbp_posts WHERE status = 'scheduled' AND scheduled_at <= ?", now):
        try:
            if publish(p["id"]) == "manual":
                db.update("gbp_posts", p["id"], status="approved", error="Due now: post it in Google (not connected).")
        except gbp_api.GBPError:
            pass
