"""Journal page: what the app did, what you decided, what failed, and the lessons learned from it."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, HTTPException, Request

from app import ai, db, jobs, journal
from app.web import redirect, render

router = APIRouter()


@router.get("/journal")
def journal_page(request: Request, area: str = "", kind: str = "", outcome: str = "", days: int = 30):
    sql, args = "SELECT * FROM events WHERE created_at >= ?", [
        (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")]
    for col, val in (("area", area), ("kind", kind), ("outcome", outcome)):
        if val:
            sql += f" AND {col} = ?"
            args.append(val)
    events = db.all(sql + " ORDER BY id DESC LIMIT 300", *args)
    lessons = db.all("SELECT l.*, c.name company FROM lessons l LEFT JOIN companies c ON c.id = l.company_id "
                     "WHERE l.status != 'retired' ORDER BY l.status = 'proposed' DESC, l.id DESC")
    areas = [r["area"] for r in db.all("SELECT DISTINCT area FROM events ORDER BY area")]
    return render(request, "journal.html", title="Journal", events=events, lessons=lessons, stats=journal.stats(days),
                  draft_areas=journal.DRAFT_AREAS, areas=areas, f={"area": area, "kind": kind, "outcome": outcome,
                                                                   "days": days},
                  companies=db.all("SELECT id, name FROM companies WHERE is_client = 1 ORDER BY name"))


@router.post("/journal/learn")
def journal_learn(area: str = Form(...), company_id: str = Form("")):
    if area not in journal.DRAFT_AREAS:
        raise HTTPException(400, "Unknown area")
    if not ai.available():
        raise HTTPException(503, "Needs the claude CLI.")
    jobs.submit(journal.propose_lessons, area, int(company_id) if company_id else None)
    return redirect("/journal?learning=1")


@router.post("/journal/lessons/{lesson_id}/{action}")
def lesson_action(lesson_id: int, action: str, text: str = Form("")):
    lesson = db.one("SELECT * FROM lessons WHERE id = ?", lesson_id)
    if not lesson:
        raise HTTPException(404)
    if action == "approve":
        db.update("lessons", lesson_id, status="active", text=(text.strip() or lesson["text"])[:400], updated_at=db.now())
    elif action == "retire":
        db.update("lessons", lesson_id, status="retired", updated_at=db.now())
    journal.record("journal", "decision", f"lesson_{action}", subject_type="lesson", subject_id=lesson_id,
                   company_id=lesson["company_id"], detail=lesson["text"][:200])
    return redirect("/journal")


@router.post("/journal/lessons")
def lesson_add(area: str = Form(...), text: str = Form(...), company_id: str = Form("")):
    """Write a lesson yourself (active immediately)."""
    if area not in journal.DRAFT_AREAS:
        raise HTTPException(400, "Unknown area")
    db.insert("lessons", area=area, company_id=int(company_id) if company_id else None, text=text.strip()[:400],
              evidence=[], status="active", created_at=db.now(), updated_at=db.now())
    return redirect("/journal")


@jobs.on_tick
def weekly_reflection() -> None:
    """Once a week, propose lessons for areas with enough new edits. Proposals still need your approval."""
    if not ai.available():
        return
    last = db.get_setting("last_reflection")
    now = datetime.now(timezone.utc)
    if last and datetime.fromisoformat(last) > now - timedelta(days=7):
        return
    db.set_setting("last_reflection", now.isoformat(timespec="seconds"))
    for area in journal.DRAFT_AREAS:
        new = db.one("SELECT COUNT(*) n FROM events WHERE kind = 'decision' AND area = ? AND outcome IN ('edited','rejected') "
                     "AND created_at >= ?", area, last or "0")["n"]
        if new >= 3:
            journal.propose_lessons(area)
