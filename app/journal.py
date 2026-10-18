"""Journal: the app's memory of what it did, what you decided, and what went wrong.

record()   — called from jobs, AI calls, approvals/edits/rejections and failures. Never raises.
decision() — records how you treated an AI draft (unchanged / edited / rejected) with both texts.
lessons_for() — approved lessons for an area (+ client) to add to future prompts.
propose_lessons() — asks Claude to turn recent edits and failures into short lessons; they stay
                    'proposed' until you approve them on the Journal page.

This is learning by feedback, not self-modifying code: drafts get better because prompts carry
your approved lessons, and failure history is visible to you and to agents (MCP get_journal).
"""
import difflib
import logging

from app import db

log = logging.getLogger("mapprospector.journal")
DRAFT_AREAS = {"posts": "Google posts", "reviews": "Review replies", "outbox": "Outreach emails",
               "tools": "Content tools"}


def record(area: str, kind: str, action: str, outcome: str = "ok", *, subject_type: str | None = None,
           subject_id: int | None = None, company_id: int | None = None, detail: str = "",
           data: dict | None = None) -> int | None:
    try:
        return db.insert("events", created_at=db.now(), area=area, kind=kind, action=action, outcome=outcome,
                         subject_type=subject_type, subject_id=subject_id, company_id=company_id,
                         detail=(detail or "")[:1000], data=data or {})
    except Exception:  # the journal must never break the work it observes
        log.exception("journal write failed")
        return None


def failure(area: str, action: str, error: object, **kw) -> int | None:
    return record(area, "failure", action, "failed", detail=str(error)[:1000], **kw)


def similarity(a: str, b: str) -> float:
    return round(difflib.SequenceMatcher(None, a or "", b or "").ratio(), 3)


def decision(area: str, action: str, *, draft: str | None, final: str | None, subject_type: str,
             subject_id: int, company_id: int | None = None) -> int | None:
    """How you treated an AI draft. outcome: unchanged | edited | rejected."""
    if final is None:
        outcome = "rejected"
    elif (draft or "").strip() == (final or "").strip():
        outcome = "unchanged"
    else:
        outcome = "edited"
    return record(area, "decision", action, outcome, subject_type=subject_type, subject_id=subject_id,
                  company_id=company_id, data={"draft": (draft or "")[:4000], "final": (final or "")[:4000],
                                               "similarity": similarity(draft or "", final or "")})


def last_draft(subject_type: str, subject_id: int) -> str | None:
    """Text the AI produced for this subject (from its 'draft' ai_call event)."""
    row = db.one("SELECT data FROM events WHERE subject_type = ? AND subject_id = ? AND action = 'draft' "
                 "AND outcome = 'ok' ORDER BY id DESC LIMIT 1", subject_type, subject_id)
    return (db.loads(row["data"], {}) or {}).get("text") if row else None


# --- lessons -------------------------------------------------------------------------------------

def lessons_for(area: str, company_id: int | None = None) -> list[str]:
    rows = db.all("SELECT id, text FROM lessons WHERE status = 'active' AND area = ? AND "
                  "(company_id IS NULL OR company_id = ?) ORDER BY company_id IS NULL, id", area, company_id)
    if rows:
        db.run(f"UPDATE lessons SET uses = uses + 1 WHERE id IN ({','.join(str(r['id']) for r in rows)})")
    return [r["text"] for r in rows]


def prompt_block(area: str, company_id: int | None = None) -> str:
    """Text appended to AI prompts: the owner's approved lessons for this kind of draft."""
    items = lessons_for(area, company_id)
    if not items:
        return ""
    return ("Lessons from how the owner edited earlier drafts (follow them unless they conflict with the rules above):\n"
            + "\n".join(f"- {t}" for t in items))


def stats(days: int = 30) -> dict:
    since = f"-{int(days)} days"
    by_area = db.all("SELECT area, outcome, COUNT(*) n FROM events WHERE kind = 'decision' AND "
                     "created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?) GROUP BY area, outcome", since)
    acceptance: dict = {}
    for r in by_area:
        acceptance.setdefault(r["area"], {"unchanged": 0, "edited": 0, "rejected": 0})[r["outcome"]] = r["n"]
    failures = db.all("SELECT area, action, COUNT(*) n, MAX(created_at) last, MAX(detail) sample FROM events "
                      "WHERE kind = 'failure' AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?) "
                      "GROUP BY area, action ORDER BY n DESC", since)
    ai = db.one("SELECT COUNT(*) n, SUM(outcome = 'failed') failed, AVG(json_extract(data, '$.ms')) avg_ms FROM events "
                "WHERE kind = 'ai_call' AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)", since)
    return {"acceptance": acceptance, "failures": failures, "ai": ai}


def evidence(area: str, company_id: int | None, limit: int = 25) -> list[dict]:
    """Recent edited/rejected drafts (the learning signal) for an area."""
    rows = db.all("SELECT id, outcome, data FROM events WHERE kind = 'decision' AND area = ? AND outcome IN "
                  "('edited','rejected') AND (? IS NULL OR company_id = ?) ORDER BY id DESC LIMIT ?",
                  area, company_id, company_id, limit)
    return [{"id": r["id"], "outcome": r["outcome"], **(db.loads(r["data"], {}) or {})} for r in rows]


def lesson_prompt(area: str, examples: list[dict], existing: list[str]) -> str:
    pairs = []
    for e in examples:
        if e["outcome"] == "rejected":
            pairs.append(f"REJECTED DRAFT:\n{e.get('draft', '')[:1200]}")
        else:
            pairs.append(f"AI DRAFT:\n{e.get('draft', '')[:1200]}\nOWNER'S FINAL VERSION:\n{e.get('final', '')[:1200]}")
    return "\n\n".join([
        f"These are {DRAFT_AREAS.get(area, area)} an AI drafted for a local business, and what the owner did with them.",
        "Infer up to 5 short, concrete, reusable writing lessons from the owner's edits (tone, length, words they "
        "remove or add, structure). Only include a lesson if at least two examples support it. Never include a "
        "lesson that would break Google's rules or invent facts, prices or claims.",
        "Already known lessons (do not repeat):\n" + ("\n".join(f"- {t}" for t in existing) or "- none"),
        "Examples:\n\n" + "\n\n---\n\n".join(pairs),
        'Reply as JSON: {"lessons": [{"text": "...", "evidence": [<example numbers starting at 1>]}]}',
    ])


def propose_lessons(area: str, company_id: int | None = None) -> int:
    """Ask Claude for lessons from recent edits. Returns how many were proposed (status 'proposed')."""
    from app import ai
    examples = evidence(area, company_id)
    if len(examples) < 2:
        return 0
    existing = [r["text"] for r in db.all("SELECT text FROM lessons WHERE area = ? AND status != 'retired' AND "
                                          "(company_id IS ? OR company_id IS NULL)", area, company_id)]
    out = ai.ask_json(lesson_prompt(area, examples, existing), purpose="lessons")
    n = 0
    for item in (out.get("lessons", []) if isinstance(out, dict) else [])[:5]:
        text = str(item.get("text", "")).strip()
        if not text or text in existing:
            continue
        ev = [examples[i - 1]["id"] for i in item.get("evidence", []) if isinstance(i, int) and 0 < i <= len(examples)]
        db.insert("lessons", area=area, company_id=company_id, text=text[:400], evidence=ev, status="proposed",
                  created_at=db.now(), updated_at=db.now())
        n += 1
    record("journal", "outcome", "propose_lessons", detail=f"{n} lessons proposed for {area}", company_id=company_id)
    return n
