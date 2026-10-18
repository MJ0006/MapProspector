"""Client reports (F1): branded HTML built from the client's data, scheduled weekly/monthly,
held for your approval, then emailed as a link (and downloadable as a file)."""
import secrets
from datetime import datetime, timedelta, timezone

from app import brain, db, jobs, journal, mailer, markdown, settings
from app.modules.gbp import reviews
from app.modules.rank import engine as rank
from app.web import brand, templates

SECTIONS = {
    "rankings": "Rankings (latest heat map per keyword + change)",
    "reviews": "Reviews (rating, new reviews, replies)",
    "activity": "Work done (posts, photos, review replies, tasks)",
    "lock": "Profile changes caught",
    "citations": "Citation score",
    "ai_visibility": "AI visibility",
    "plan": "Local SEO Brain scorecard & roadmap (Draft)",
}
PERIOD_DAYS = {"weekly": 7, "monthly": 30, "none": 30}


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def gather(company_id: int, sections: list[str], days: int) -> dict:
    co = db.one("SELECT * FROM companies WHERE id = ?", company_id)
    since = _since(days)
    data: dict = {"co": co, "days": days, "since": since[:10], "today": datetime.now(timezone.utc).date().isoformat()}
    if "rankings" in sections and co["place_id"]:
        latest = db.all("SELECT * FROM scans WHERE id IN (SELECT MAX(id) FROM scans WHERE place_id = ? AND status IN "
                        "('completed','incomplete') GROUP BY keyword, surface) ORDER BY keyword", co["place_id"])
        rows = []
        for s in latest:
            st = db.loads(s["stats"], {}) or {}
            prev = rank.previous_scan(s)
            pst = db.loads(prev["stats"], {}) if prev else None
            pts = rank.points(s["id"])
            rows.append({"scan": s, "stats": st, "prev": pst,
                         "change": round(pst["avg_rank_all"] - st["avg_rank_all"], 2) if pst and st.get("avg_rank_all") and pst.get("avg_rank_all") else None,
                         "grid": [p["rank"] for p in pts], "size": s["grid_size"], "shape": s["shape"],
                         "cells": [(p["rank"], p["results"] is not None) for p in pts]})
        data["rankings"] = rows
    if "reviews" in sections:
        data["reviews"] = {**reviews.stats(company_id),
                           "new": db.all("SELECT rating, text, review_date FROM reviews WHERE company_id = ? AND COALESCE(review_date, created_at) >= ? "
                                         "ORDER BY review_date DESC LIMIT 10", company_id, since)}
    if "activity" in sections:
        data["activity"] = {
            "posts": db.all("SELECT body, published_at FROM gbp_posts WHERE company_id = ? AND status = 'published' AND published_at >= ?", company_id, since),
            "photos": db.one("SELECT COUNT(*) n FROM photos WHERE company_id = ? AND status = 'published' AND published_at >= ?", company_id, since)["n"],
            "replies": db.one("SELECT COUNT(*) n FROM reviews WHERE company_id = ? AND reply_status = 'published' AND replied_at >= ?", company_id, since)["n"],
            "tasks": db.all("SELECT title FROM tasks WHERE company_id = ? AND done = 1 AND created_at >= ?", company_id, since),
        }
    if "lock" in sections:
        data["lock"] = db.all("SELECT field, live_value, approved_value, status, detected_at FROM profile_changes "
                              "WHERE company_id = ? AND detected_at >= ?", company_id, since)
    if "citations" in sections:
        data["citations"] = db.one("SELECT score, grade, completed_at FROM citation_audits WHERE company_id = ? AND "
                                   "score IS NOT NULL ORDER BY id DESC LIMIT 1", company_id)
    if "ai_visibility" in sections and co["place_id"]:
        r = db.one("SELECT report, completed_at FROM aivis_runs WHERE place_id = ? AND status = 'completed' ORDER BY id DESC LIMIT 1", co["place_id"])
        data["ai_visibility"] = {**db.loads(r["report"], {}), "date": (r["completed_at"] or "")[:10]} if r else None
    if "plan" in sections and co["vault_slug"]:
        sc = brain.deliverable(co["vault_slug"], "Health Scorecard")
        rm = brain.deliverable(co["vault_slug"], "Action Roadmap")
        data["plan"] = {"scorecard": markdown.render(sc) if sc else None, "roadmap": markdown.render(rm) if rm else None}
    return data


def render_html(report: dict, note: str = "") -> str:
    data = gather(report["company_id"], db.loads(report["sections"], []), PERIOD_DAYS.get(report["schedule"] or "none", 30))
    return templates.get_template("reports/client_report.html").render(r=report, d=data, brand=brand(), note=note)


def build_run(report_id: int, note: str = "") -> int:
    rep = db.one("SELECT * FROM reports WHERE id = ?", report_id)
    html = render_html(rep, note)
    status = "pending_approval" if rep["require_approval"] else "approved"
    run_id = db.insert("report_runs", report_id=report_id, html=html, token=secrets.token_urlsafe(16), status=status,
                       created_at=db.now())
    if status == "approved":
        send_run(run_id)
    return run_id


def send_run(run_id: int) -> None:
    run = db.one("SELECT * FROM report_runs WHERE id = ?", run_id)
    rep = db.one("SELECT * FROM reports WHERE id = ?", run["report_id"])
    if not (rep["recipients"] or "").strip():
        return  # nobody to email: stays approved (download / share link only)
    if not db.claim("report_runs", run_id, ("approved",), "sending"):
        return
    co = db.one("SELECT name FROM companies WHERE id = ?", rep["company_id"])
    link = f"{settings.PUBLIC_BASE_URL}/share/report/{run['token']}"
    for to in [e.strip() for e in (rep["recipients"] or "").split(",") if e.strip()]:
        mailer.queue("email", None, to, f"{rep['name']}: {co['name']}",
                     f"Hi,\n\nYour latest report for {co['name']} is ready:\n{link}\n\nThanks,\n{brand()['name']}",
                     origin="report", approved=True)
    db.update("report_runs", run_id, status="sent", sent_at=db.now())


@jobs.on_startup
def interrupted_report_sends() -> None:
    db.run("UPDATE report_runs SET status = 'approved' WHERE status = 'sending'")  # only queues emails: safe to redo


def next_run_at(schedule: str, after: datetime) -> str | None:
    if schedule == "weekly":
        nxt = rank.next_run("weekly", 0, after)  # Mondays 06:00 UTC
    elif schedule == "monthly":
        nxt = rank.next_run("monthly_day", 1, after)
    else:
        return None
    return nxt.isoformat(timespec="seconds")


@jobs.on_tick
def run_due_reports() -> None:
    now = datetime.now(timezone.utc)
    for r in db.all("SELECT * FROM reports WHERE next_run_at IS NOT NULL AND next_run_at <= ?", now.isoformat(timespec="seconds")):
        # Advance first so a failing report is retried next period, not every minute.
        db.update("reports", r["id"], next_run_at=next_run_at(r["schedule"], now))
        try:
            build_run(r["id"])
            db.update("reports", r["id"], last_error=None)
        except Exception as exc:
            db.update("reports", r["id"], last_error=str(exc)[:300])
            journal.failure("report", "scheduled_build", exc, subject_type="report", subject_id=r["id"],
                            company_id=r["company_id"])
